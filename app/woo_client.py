"""
Cliente ligero de WooCommerce para MiraStock-Total.
Obtiene SKU + imagen + categorías + descripción en una sola pasada paginada.
No trae precios — el precio que muestra la app es siempre SAP neto + IVA (precio
de tienda), no el de la tienda online.
"""
import os
import logging
import requests
from requests_oauthlib import OAuth1

logger = logging.getLogger("WooClient")


class WooImageClient:
    def __init__(self):
        self.url    = os.getenv("WOO_URL", "").rstrip("/")
        self.key    = os.getenv("WOO_KEY", "")
        self.secret = os.getenv("WOO_SECRET", "")
        self.auth   = OAuth1(self.key, self.secret) if self.key and self.secret else None
        self.base   = f"{self.url}/wp-json/wc/v3"

    def is_configured(self) -> bool:
        return bool(self.url and self.key and self.secret)

    def get_enrichment(self) -> tuple[dict, dict, dict, dict]:
        """
        Pasada completa: devuelve (image_map, category_map, all_categories, desc_map).
        - image_map:      {SKU_UPPER: imagen_url}  — incluye variaciones
        - category_map:   {SKU_UPPER: "slug1,slug2"}
        - all_categories: {slug: nombre_display}
        - desc_map:       {SKU_UPPER: short_description}
        """
        if not self.is_configured():
            logger.warning("[Woo] No configurado — se omite enriquecimiento.")
            return {}, {}, {}, {}

        image_map:      dict[str, str]  = {}
        category_map:   dict[str, str]  = {}
        all_categories: dict[str, str]  = {}
        desc_map:       dict[str, str]  = {}
        variable_products: list[dict]   = []
        page = 1

        logger.info("[Woo] Descargando productos WooCommerce...")
        while True:
            try:
                r = requests.get(
                    f"{self.base}/products",
                    auth=self.auth,
                    params={
                        "page": page, "per_page": 100,
                        "_fields": "id,sku,type,images,categories,short_description",
                    },
                    timeout=30,
                )
                r.raise_for_status()
                products = r.json()
                if not products:
                    break
                for p in products:
                    sku    = str(p.get("sku") or "").strip().upper()
                    images = p.get("images") or []
                    cats   = [c for c in (p.get("categories") or []) if c.get("slug")]
                    desc   = (p.get("short_description") or "").strip()

                    for c in cats:
                        all_categories[c["slug"]] = c.get("name", c["slug"])

                    parent_img = images[0].get("src", "") if images else ""
                    cat_str    = ",".join(c["slug"] for c in cats)

                    if sku:
                        if parent_img:
                            image_map[sku] = parent_img
                        if cat_str:
                            category_map[sku] = cat_str
                        if desc:
                            desc_map[sku] = desc

                    if p.get("type") == "variable":
                        variable_products.append({
                            "id":    p["id"],
                            "img":   parent_img,
                            "cats":  cat_str,
                        })

                logger.info(f"[Woo] Productos — página {page} ({len(products)})")
                page += 1
            except Exception as e:
                logger.error(f"[Woo] Error en página {page}: {e}")
                break

        # Variaciones: SKUs propios con imagen y categorías del padre
        logger.info(f"[Woo] Obteniendo variaciones de {len(variable_products)} productos variables...")
        for vp in variable_products:
            vpage = 1
            while True:
                try:
                    r = requests.get(
                        f"{self.base}/products/{vp['id']}/variations",
                        auth=self.auth,
                        params={"per_page": 100, "page": vpage, "_fields": "sku,image"},
                        timeout=30,
                    )
                    r.raise_for_status()
                    variations = r.json()
                    if not variations:
                        break
                    for v in variations:
                        vsku = str(v.get("sku") or "").strip().upper()
                        if not vsku:
                            continue
                        vimg = (v.get("image") or {}).get("src", "") or vp["img"]
                        if vimg and vsku not in image_map:
                            image_map[vsku] = vimg
                        if vp["cats"] and vsku not in category_map:
                            category_map[vsku] = vp["cats"]
                    vpage += 1
                except Exception as e:
                    logger.error(f"[Woo] Error variaciones producto {vp['id']}: {e}")
                    break

        logger.info(
            f"[Woo] Completado: {len(image_map)} imágenes, "
            f"{len(all_categories)} categorías, {len(desc_map)} descripciones."
        )
        return image_map, category_map, all_categories, desc_map

    def get_sku_image_map(self) -> dict[str, str]:
        image_map, _, _, _ = self.get_enrichment()
        return image_map
