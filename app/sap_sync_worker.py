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
    sync_status.update({"is_running": True, "progress": 5, "message": "Conectando con Stock-Service..."})

    try:
        api_url  = os.getenv("API_PLANILLAS_URL", "").rstrip("/")
        # enabled=true: solo SKU vigentes/vendibles en SAP ahora mismo (SalesItem='Y' y
        # Valid='Y') — sin esto, Stock-Service trae también los que SAP dejó de traer
        # (descontinuados/de baja), inflando el catálogo con ítems que no deberían
        # mostrarse en una tienda/escáner de uso real.
        endpoint = f"{api_url}/api/v1/stock/catalog?enabled=true"

        if not api_url:
            raise ValueError("API_PLANILLAS_URL no está configurada en el .env")

        headers = {}
        api_key = os.getenv("API_PLANILLAS_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key

        sync_status.update({"progress": 10, "message": "Obteniendo catálogo desde Stock-Service (puede tardar varios minutos)..."})
        logger.info(f"[Sync] Llamando a {endpoint}")

        # Timeout generoso: SAP puede tardar varios minutos en responder
        response = requests.get(endpoint, headers=headers, timeout=600)
        if not response.ok:
            try:
                detail = response.json().get("detail", response.text[:500])
            except Exception:
                detail = response.text[:500]
            raise RuntimeError(f"Stock-Service respondió {response.status_code}: {detail}")
        data  = response.json()
        items = data.get("items", [])

        logger.info(f"[Sync] {len(items)} ítems recibidos desde Stock-Service.")
        sync_status.update({"progress": 70, "message": f"Procesando {len(items)} ítems..."})

        # ── Preparar filas para SQLite ──────────────────────────────────────────
        # Categorías y descripción web ya vienen incluidas en la respuesta de
        # Stock-Service (campos `categories` y `woo_description`) — no hace falta
        # una segunda llamada a WooCommerce para esto (esa llamada aparte existía
        # antes y fallaba en silencio, dejando categorías/descripción vacías).
        sync_status.update({"progress": 80, "message": "Guardando en base de datos local..."})

        product_rows = []
        stock_rows   = []
        all_categories: dict[str, str] = {}

        for p in items:
            sku = (p.get("sku") or "").strip()
            if not sku:
                continue
            name      = (p.get("name") or "").strip()
            name_norm = _normalize(name)
            # Descripción: SAP ForeignName primero, descripción de WooCommerce como fallback
            sap_desc = (p.get("description") or "").strip()
            description = sap_desc or (p.get("woo_description") or "").strip()

            cats = p.get("categories") or []
            for c in cats:
                if c.get("slug"):
                    all_categories[c["slug"]] = c.get("name", c["slug"])
            categories_str = ",".join(c["slug"] for c in cats if c.get("slug"))

            product_rows.append((
                sku,
                name,
                name_norm,
                p.get("item_type", "Producto"),
                float(p.get("price") or 0),
                "",  # image_url: sin fuente propia — se usa solo "images" (Drive)
                description,
                1 if p.get("sell_item", True) else 0,
                categories_str,
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

        sync_status.update({
            "is_running":     False,
            "progress":       100,
            "total_products": len(product_rows),
            "last_sync":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "message":        f"Completado: {len(product_rows)} productos, {len(all_categories)} categorías.",
        })
        logger.info(f"[Sync] Completado: {len(product_rows)} productos, {len(all_categories)} categorías.")

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
