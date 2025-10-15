from typing import Dict
from google.cloud import firestore


def fetch_inventory(db: firestore.Client) -> Dict[str, dict]:
    inv: Dict[str, dict] = {}
    for doc in db.collection("inventory").stream():
        d = doc.to_dict() or {}
        inv[doc.id] = {
            "id": doc.id,
            "name": d.get("name", doc.id),
            "unit": d.get("unit", "g"),
            "capacity": float(d.get("capacity", 0)),
            "current": float(d.get("current", 0)),
            "updated_at": d.get("updated_at"),
        }
    return inv

