from typing import Dict
from google.cloud import firestore


def fetch_recipes(db: firestore.Client) -> Dict[str, dict]:
    rec: Dict[str, dict] = {}
    for doc in db.collection("recipes").stream():
        d = doc.to_dict() or {}
        rec[doc.id] = {
            "id": doc.id,
            "base_volume_ml": float(d.get("base_volume_ml", 200)),
            "ingredients": d.get("ingredients", []),  # [{ingredient_id, qty, unit}]
        }
    return rec


def fetch_products(db: firestore.Client) -> Dict[str, dict]:
    prods: Dict[str, dict] = {}
    for doc in db.collection("products").where("is_active", "==", True).stream():
        d = doc.to_dict() or {}
        prods[doc.id] = {
            "id": doc.id,
            "name": d.get("name", doc.id),
            "category": d.get("category", "Прочее"),
            "volumes": d.get("volumes", [200]),
            "base_price": int(d.get("base_price", 0)),
            "addons": d.get("addons", []),  # [{id,name,price_delta,ingredients:{}}]
            "recipe_ref": d.get("recipe_ref", None),  # 'recipes/xxx' или reference
        }
    return prods
