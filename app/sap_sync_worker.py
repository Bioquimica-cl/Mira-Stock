import logging
import os
import threading
import unicodedata
from datetime import datetime

import requests

from app.database import get_db, init_db

logger = logging.getLogger("MiraStockSync")

sync_status = {
    "is_running":     False,
    "progress":       0,
    "last_sync":      None,
    "total_products": 0,
    "message":        "Sin sincronizar. Presione el botón para iniciar.",
}


def _normalize(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text)) if unicodedata.category(c) != "Mn"
    ).lower()


def run_sync():
    sync_status.update({"is_running": True, "progress": 5, "message": "Conectando con API-Planillas-1..."})

    try:
        api_url  = os.getenv("API_PLANILLAS_URL", "").rstrip("/")
        endpoint = f"{api_url}/api/v1/stock/catalog"

        if not api_url:
            raise ValueError("API_PLANILLAS_URL no está configurada en el .env")

        headers = {}
        api_key = os.getenv("API_PLANILLAS_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key

        sync_status.update({"progress": 10, "message": "Obteniendo catálogo desde Integraciones-BQ (puede tardar varios minutos)..."})
        logger.info(f"[Sync] Llamando a {endpoint}")

        # Timeout generoso: SAP puede tardar varios minutos en responder
        response = requests.get(endpoint, headers=headers, timeout=600)
        if not response.ok:
            try:
                detail = response.json().get("detail", response.text[:500])
            except Exception:
                detail = response.text[:500]
            raise RuntimeError(f"Integraciones-BQ respondió {response.status_code}: {detail}")
        data  = response.json()
        items = data.get("items", [])

        logger.info(f"[Sync] {len(items)} ítems recibidos desde Integraciones-BQ.")
        sync_status.update({"progress": 65, "message": f"Procesando {len(items)} ítems..."})

        # ── Enriquecimiento con WooCommerce (imágenes + categorías, opcional) ────
        image_map:      dict = {}
        category_map:   dict = {}
        all_categories: dict = {}
        desc_map:       dict = {}
        try:
            from app.woo_client import WooImageClient
            woo = WooImageClient()
            if woo.is_configured():
                sync_status.update({"progress": 70, "message": "Obteniendo imágenes, categorías y descripciones desde WooCommerce..."})
                image_map, category_map, all_categories, desc_map = woo.get_enrichment()
                logger.info(f"[Sync] {len(image_map)} imágenes, {len(all_categories)} categorías, {len(desc_map)} descripciones de WooCommerce.")
        except Exception as e:
            logger.warning(f"[Sync] WooCommerce no disponible (no crítico): {e}")

        # ── Preparar filas para SQLite ──────────────────────────────────────────
        sync_status.update({"progress": 80, "message": "Guardando en base de datos local..."})

        product_rows = []
        stock_rows   = []

        for p in items:
            sku = (p.get("sku") or "").strip()
            if not sku:
                continue
            name      = (p.get("name") or "").strip()
            name_norm = _normalize(name)
            image_url = image_map.get(sku, "")  # URL de Woo; se sobreescribe con IDs de Drive al exportar
            # Descripción: SAP ForeignName primero, WooCommerce short_description como fallback
            sap_desc = (p.get("description") or "").strip()
            description = sap_desc or desc_map.get(sku, "")

            product_rows.append((
                sku,
                name,
                name_norm,
                p.get("item_type", "Producto"),
                float(p.get("price") or 0),
                image_url,
                description,
                1 if p.get("sell_item", True) else 0,
                category_map.get(sku, ""),
                "",  # images: se llena con el script de Drive, no se toca en el sync
            ))

            for wh in ["01", "11", "15", "30"]:
                stock_rows.append((sku, wh, float(p.get(f"stock_{wh}") or 0)))

        # ── Persistir ───────────────────────────────────────────────────────────
        init_db()
        conn = get_db()

        # Preservar datos locales que el sync no trae (ubicaciones e imágenes de Drive)
        preserved = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT sku, location, images FROM products WHERE location != '' OR images != ''"
            ).fetchall()
        }

        with conn:
            conn.execute("DELETE FROM stock")
            conn.execute("DELETE FROM products")
            conn.execute("DELETE FROM categories")
            conn.executemany(
                "INSERT INTO products (sku, name, name_norm, item_type, price, image_url, description, sell_item, categories, images) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                product_rows,
            )
            conn.executemany(
                "INSERT INTO stock (sku, warehouse_code, on_hand) VALUES (?, ?, ?)",
                stock_rows,
            )
            if all_categories:
                conn.executemany(
                    "INSERT INTO categories (slug, name) VALUES (?, ?)",
                    [(slug, name) for slug, name in all_categories.items()],
                )
            # Restaurar ubicaciones e imágenes de Drive que el sync sobreescribiría con ''
            for sku, (location, images) in preserved.items():
                if location:
                    conn.execute(
                        "UPDATE products SET location=? WHERE sku=?", (location, sku)
                    )
                if images:
                    conn.execute(
                        "UPDATE products SET images=? WHERE sku=?", (images, sku)
                    )
        conn.close()

        woo_msg = f", {len(image_map)} imágenes, {len(all_categories)} categorías" if image_map else ""
        sync_status.update({
            "is_running":     False,
            "progress":       100,
            "total_products": len(product_rows),
            "last_sync":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "message":        f"Completado: {len(product_rows)} productos{woo_msg}.",
        })
        logger.info(f"[Sync] Completado: {len(product_rows)} productos{woo_msg}.")

    except Exception as e:
        sync_status.update({
            "is_running": False,
            "progress":   0,
            "message":    f"Error en sincronización: {str(e)}",
        })
        logger.error(f"[Sync] Error: {e}", exc_info=True)


def start_async_sync():
    if sync_status["is_running"]:
        logger.info("Sync ya en curso, omitiendo.")
        return
    threading.Thread(target=run_sync, daemon=True).start()
