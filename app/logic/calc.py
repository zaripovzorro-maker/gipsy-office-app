from typing import Dict, List


def compute_base_consumption(recipe: dict, volume_ml: float) -> Dict[str, float]:
    base_ml = float(recipe.get("base_volume_ml", 200)) or 200.0
    k = volume_ml / base_ml
    out: Dict[str, float] = {}
    for item in recipe.get("ingredients", []):
        iid = item["ingredient_id"]
        qty = float(item["qty"])
        out[iid] = out.get(iid, 0.0) + qty * k
    return out


def consumption_for_item(product: dict, volume_ml: float, addon_ids: List[str], recipes: Dict[str, dict]) -> Dict[str, float]:
    total: Dict[str, float] = {}

    rkey = None
    if product.get("recipe_ref"):
        if isinstance(product["recipe_ref"], str):
            rkey = product["recipe_ref"].split("/")[-1]
        elif isinstance(product["recipe_ref"], dict) and "path" in product["recipe_ref"]:
            rkey = str(product["recipe_ref"]["path"]).split("/")[-1]

    if rkey and rkey in recipes:
        total = sum_maps(total, compute_base_consumption(recipes[rkey], volume_ml))

    addons = {a["id"]: a for a in product.get("addons", [])}
    for add_id in addon_ids:
        ad = addons.get(add_id)
        if not ad:
            continue
        for iid, q in (ad.get("ingredients") or {}).items():
            total[iid] = total.get(iid, 0.0) + float(q)

    return total


def sum_maps(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def total_cart_consumption(cart: List[dict], products: Dict[str, dict], recipes: Dict[str, dict]) -> Dict[str, float]:
    need: Dict[str, float] = {}
    for item in cart:
        prod = products.get(item["product_id"])
        if not prod:
            continue
        cons = consumption_for_item(prod, item["volume_ml"], item.get("addons", []), recipes)
        for k, v in cons.items():
            need[k] = need.get(k, 0.0) + v * int(item["qty"])
    return need


def find_shortages(need: Dict[str, float], inventory: Dict[str, dict]):
    shortages = []
    for iid, req in need.items():
        have = float(inventory.get(iid, {}).get("current", 0.0))
        if have + 1e-9 < req:
            shortages.append({"ingredient_id": iid, "need": req, "have": have, "deficit": req - have})
    return shortages
