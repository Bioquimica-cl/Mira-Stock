import csv
import os
import unicodedata
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.database import get_db, DB_PATH
from app.sap_sync_worker import sync_status, start_async_sync

router = APIRouter(prefix="/api", tags=["Catalog"])

# App de solo consulta: un único set de columnas. El único stock que se muestra
# en toda la app es el de tienda (bodega 15) — no hay vista "web" ni canales.
# El precio mostrado es siempre SAP neto (el +IVA se calcula en el frontend).
_PRODUCT_COLS = """
    p.sku,
    p.name,
    p.description,
    p.item_type,
    p.sell_item,
    p.image_url,
    p.price,
    COALESCE(s15.on_hand, 0) AS stock_tienda,
    p.location
"""

_JOINS = """
    FROM products p
    LEFT JOIN stock s15 ON p.sku = s15.sku AND s15.warehouse_code = '15'
"""


def _expand_images(row: dict) -> dict:
    # Imagen servida directo desde WooCommerce (sin Drive de por medio).
    urls = [row["image_url"]] if row.get("image_url") else []
    row["image_urls"] = urls
    row["image_count"] = len(urls)
    return row


def _normalize(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text)) if unicodedata.category(c) != "Mn"
    ).lower()


def _build_conditions(search: str, item_type: str, stock_filter: str, category: str):
    conditions, args = [], []

    if search:
        search_terms = _normalize(search).split()
        for term in search_terms:
            like_term = f"%{term}%"
            conditions.append("(p.name_norm LIKE ? OR LOWER(p.sku) LIKE ? OR LOWER(p.description) LIKE ?)")
            args.extend([like_term, like_term, like_term])

    if item_type and item_type != "all":
        conditions.append("p.item_type = ?")
        args.append(item_type)

    if stock_filter == "instock":
        conditions.append("COALESCE(s15.on_hand, 0) > 0")
    elif stock_filter == "outofstock":
        conditions.append("COALESCE(s15.on_hand, 0) = 0")

    # Solo ítems vendibles — comportamiento fijo, sin toggle en la UI.
    conditions.append("p.sell_item = 1")

    if category:
        conditions.append("(',' || p.categories || ',') LIKE ?")
        args.append(f"%,{category},%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, args


def migrate_csv_locations() -> None:
    """Importa ubicaciones del CSV al DB una sola vez (si la DB no tiene ubicaciones aún)."""
    csv_path = Path(__file__).parent.parent / "Ubicaciones Tienda SKU - Hoja 1.csv"
    if not csv_path.exists() or not os.path.exists(DB_PATH):
        return
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM products WHERE location != ''").fetchone()[0]
        conn.close()
        if count > 0:
            return
        locations: dict[str, str] = {}
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sku = (row.get("SKU") or "").strip().upper()
                if not sku:
                    continue
                locs = [
                    (row.get("UBICACION-1") or "").strip(),
                    (row.get("UBICACION-2") or "").strip(),
                    (row.get("UBICACION-3") or "").strip(),
                ]
                locs = [l for l in locs if l]
                if locs and sku not in locations:
                    locations[sku] = ", ".join(locs)
        if not locations:
            return
        conn = get_db()
        with conn:
            for sku, loc in locations.items():
                conn.execute(
                    "UPDATE products SET location=? WHERE UPPER(sku)=UPPER(?)",
                    (loc, sku),
                )
        conn.close()
    except Exception:
        pass


@router.get("/categories")
async def get_categories():
    if not os.path.exists(DB_PATH):
        return []
    conn = get_db()
    rows = conn.execute("SELECT slug, name FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/products")
async def get_products(
    search: str = "",
    item_type: str = "all",
    stock_filter: str = "all",
    category: str = "",
    page: int = 1,
    page_size: int = 24,
):
    if not os.path.exists(DB_PATH):
        return {
            "products": [],
            "pagination": {"current_page": 1, "total_pages": 0, "total_items": 0, "page_size": page_size},
        }

    where, args = _build_conditions(search, item_type, stock_filter, category)

    conn = get_db()
    total_items = conn.execute(f"SELECT COUNT(*) {_JOINS} {where}", args).fetchone()[0]
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size

    order_clause = "p.name"
    order_args: list = []
    if search:
        norm = _normalize(search.strip())
        order_clause = """
            CASE
                WHEN LOWER(p.sku) = LOWER(?)   THEN 0
                WHEN p.name_norm LIKE ?         THEN 1
                ELSE                                 2
            END, p.name
        """
        order_args = [search.strip(), f"%{norm}%"]

    rows = conn.execute(
        f"SELECT {_PRODUCT_COLS} {_JOINS} {where} ORDER BY {order_clause} LIMIT ? OFFSET ?",
        args + order_args + [page_size, offset],
    ).fetchall()
    conn.close()

    return {
        "products": [_expand_images(dict(r)) for r in rows],
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "page_size": page_size,
        },
    }


@router.get("/product/{sku}")
async def get_product(sku: str):
    if not os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=503,
            detail="Base de datos no inicializada. Ejecute una sincronización primero.",
        )

    conn = get_db()
    row = conn.execute(
        f"SELECT {_PRODUCT_COLS} {_JOINS} WHERE UPPER(p.sku) = UPPER(?)",
        (sku.strip(),),
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Producto con SKU '{sku}' no encontrado.")

    return {"status": "success", "product": _expand_images(dict(row))}


@router.get("/sync-status")
async def get_sync_status():
    return sync_status


@router.post("/trigger-sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    if sync_status["is_running"]:
        return {"message": "Sincronización ya en curso.", "status": sync_status}
    background_tasks.add_task(start_async_sync)
    return {"message": "Sincronización iniciada.", "status": sync_status}


@router.get("/sync-schedule")
async def get_sync_schedule():
    from app.main import scheduler, SYNC_INTERVAL_MINUTES
    job = scheduler.get_job("auto_sync")
    next_run = (
        job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job and job.next_run_time else None
    )
    return {"interval_minutes": SYNC_INTERVAL_MINUTES, "next_sync": next_run}
