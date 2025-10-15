from __future__ import annotations
from typing import Dict, Any, List, Tuple
from google.cloud import firestore

# products/{productId}
# {
#   name: str, category: str, volumes: [200, 400],
#   base_price: int (копейки),
#   addons: [{id, name, price_delta:int, ingredients:{ingredientId: qty}}],
#   recipe_ref: "recipes/{id}",
#   is_active: bool
# }

def list_categories(db: firestore.Client) -> List[str]:
    docs = db.collection("products").where("is_active", "==", True).stream()
    return sorted({ (d.to_dict().get("category") or "Без категории") for d in docs })

def list_products_by_category(db: firestore.Client, category: str) -> List[Tuple[str, Dict[str,Any]]]:
    q = db.collection("products").where("is_active","==",True).where("category","==",category)
    return [(d.id, d.to_dict()) for d in q.stream()]

def load_recipe(db: firestore.Client, recipe_ref: str) -> Dict[str,Any] | None:
    # recipe_ref вида "recipes/xxx"
    parts = recipe_ref.split("/")
    if len(parts) != 2:
        return None
    d = db.collection(parts[0]).document(parts[1]).get()
    return d.to_dict() if d.exists else None

