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


def _fetch_woo_images() -> dict[str, str]:
    """{SKU_UPPER: image_url} directo desde WooCommerce — sin pasar por Drive.
    La imagen se sirve siempre desde el servidor de WooCommerce, en vivo."""
    woo_url = os.getenv("WOO_URL", "").rstrip("/")
    key     = os.getenv("WOO_KEY", "")
    secret  = os.getenv("WOO_SECRET", "")
    if not (woo_url and key and secret):
        logger.warning("[Sync] WOO_URL/WOO_KEY/WOO_SECRET no configurados — sin imágenes.")
        return {}

    # Basic Auth (no OAuth1): WooCommerce solo exige firma OAuth1 para sitios sin
    # SSL — bioquimica.cl es HTTPS, así que key/secret van directo como usuario/clave.
    auth = (key, secret)
    base = f"{woo_url}/wp-json/wc/v3"
    image_map: dict[str, str] = {}
    variable_ids: list[int] = []
    page = 1

    while True:
        try:
            r = requests.get(
                f"{base}/products",
                auth=auth,
                params={"page": page, "per_page": 100, "_fields": "id,sku,type,images"},
                timeout=30,
            )
            r.raise_for_status()
            products = r.json()
            if not products:
                break
            for p in products:
                sku = str(p.get("sku") or "").strip().upper()
                imgs = p.get("images") or []
                if sku and imgs and imgs[0].get("src"):
                    image_map[sku] = imgs[0]["src"]
                if p.get("type") == "variable":
                    variable_ids.append(p["id"])
            page += 1
        except Exception as e:
            logger.warning(f"[Sync] WooCommerce (productos, página {page}) falló: {e}")
            break

    for pid in variable_ids:
        vpage = 1
        while True:
            try:
                r = requests.get(
                    f"{base}/products/{pid}/variations",
                    auth=auth,
                    params={"per_page": 100, "page": vpage, "_fields": "sku,image"},
                    timeout=30,
                )
                r.raise_for_status()
                variations = r.json()
                if not variations:
                    break
                for v in variations:
                    vsku = str(v.get("sku") or "").strip().upper()
                    vimg = (v.get("image") or {}).get("src", "")
                    if vsku and vimg and vsku not in image_map:
                        image_map[vsku] = vimg
                vpage += 1
            except Exception as e:
                logger.warning(f"[Sync] WooCommerce (variaciones producto {pid}) falló: {e}")
                break

    return image_map


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
        sync_status.update({"progress": 60, "message": f"Procesando {len(items)} ítems..."})

        # ── Imágenes directo de WooCommerce (sin Drive) ─────────────────────────
        sync_status.update({"progress": 65, "message": "Obteniendo imágenes desde WooCommerce..."})
        image_map = _fetch_woo_images()
        logger.info(f"[Sync] {len(image_map)} imágenes obtenidas de WooCommerce.")

        # ── Preparar filas para SQLite ──────────────────────────────────────────
        # Categorías y descripción web ya vienen incluidas en la respuesta de
        # Stock-Service (campos `categories` y `woo_description`) — no hace falta
        # pedírselas a WooCommerce de nuevo.
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
            image_url = image_map.get(sku.upper(), "")

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
                image_url,
                description,
                1 if p.get("sell_item", True) else 0,
                categories_str,
            ))

            for wh in ["01", "11", "15", "30"]:
                stock_rows.append((sku, wh, float(p.get(f"stock_{wh}") or 0)))

        # ── Persistir ───────────────────────────────────────────────────────────
        init_db()
        conn = get_db()

        # Preservar la ubicación física (lo único que el sync no trae de ninguna API)
        preserved_locations = dict(
            conn.execute("SELECT sku, location FROM products WHERE location != ''").fetchall()
        )

        with conn:
            conn.execute("DELETE FROM stock")
            conn.execute("DELETE FROM products")
            conn.execute("DELETE FROM categories")
            conn.executemany(
                "INSERT INTO products (sku, name, name_norm, item_type, price, image_url, description, sell_item, categories) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            if preserved_locations:
                conn.executemany(
                    "UPDATE products SET location=? WHERE sku=?",
                    [(location, sku) for sku, location in preserved_locations.items()],
                )
        conn.close()

        sync_status.update({
            "is_running":     False,
            "progress":       100,
            "total_products": len(product_rows),
            "last_sync":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "message":        f"Completado: {len(product_rows)} productos, {len(all_categories)} categorías, {len(image_map)} imágenes.",
        })
        logger.info(f"[Sync] Completado: {len(product_rows)} productos, {len(all_categories)} categorías, {len(image_map)} imágenes.")

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
