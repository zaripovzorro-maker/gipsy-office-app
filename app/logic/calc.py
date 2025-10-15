from __future__ import annotations
from typing import Dict, Any, List
from google.cloud import firestore
from app.services.products import load_recipe

def compute_item_consumption(db: firestore.Client, product: Dict[str,Any], volume_ml: int, addon_ids: List[str]) -> Dict[str,float]:
    # product.recipe_ref -> recipes/{id}
    r = load_recipe(db, product.get("recipe_ref","") or "")
    if not r:
        return {}  # без рецепта не списываем (или можно жёстко блокировать)

    base_vol = float(r.get("base_volume_ml") or 0) or 200.0
    mult = float(volume_ml) / base_vol

    cons: Dict[str,float] = {}
    for ing in r.get("ingredients", []):
        iid = ing["ingredient_id"]; qty = float(ing["qty"])
        cons[iid] = cons.get(iid, 0.0) + qty * mult

    # добавки (каждая добавка: ingredients:{id: qty})
    addons_map = {a["id"]: a for a in (product.get("addons") or [])}
    for aid in addon_ids:
        a = addons_map.get(aid)
        if not a: continue
        for iid, qty in (a.get("ingredients") or {}).items():
            cons[iid] = cons.get(iid, 0.0) + float(qty)

    return cons

def aggregate_consumption(item_consumptions: List[Dict[str,float]]) -> Dict[str,float]:
    agg: Dict[str,float] = {}
    for c in item_consumptions:
        for k,v in c.items():
            agg[k] = agg.get(k, 0.0) + float(v)
    return agg

def price_of_item(item: Dict[str,Any]) -> int:
    # price = base_price*qty + sum(addon.price_delta)*qty
    base = int(item.get("base_price",0))
    addons = item.get("addons", [])
    product_addons = {a["id"]: a for a in (item["product_doc"].get("addons") or [])}
    addons_sum = sum(int(product_addons[a]["price_delta"]) for a in addons if a in product_addons)
    return (base + addons_sum) * int(item.get("qty",1))

