from __future__ import annotations
from typing import Dict, Any, List
from google.cloud import firestore
from app.logic.calc import compute_item_consumption, aggregate_consumption, price_of_item

# cart_item = {
#   product_id, product_name, volume_ml:int, qty:int,
#   base_price:int (копейки),  addons:[addonId]
#   product_doc: full product document (для расчётов)
# }

def check_and_commit_sale(db: firestore.Client, cart: List[Dict[str,Any]]) -> Dict[str,Any]:
    if not cart:
        return {"ok": False, "error": "Корзина пуста"}

    # Предварительный расчёт потребления по корзине
    item_consumptions = []
    total_amount = 0
    for it in cart:
        cons = compute_item_consumption(db, it["product_doc"], it["volume_ml"], it.get("addons", []))
        item_consumptions.append(cons)
        total_amount += price_of_item(it)

    need = aggregate_consumption(item_consumptions)  # {ingredientId: qty}

    def _tx(transaction: firestore.Transaction):
        # читаем все нужные inventory
        inv_refs = {ing: db.collection("inventory").document(ing) for ing in need.keys()}
        inv_snaps = {k: r.get(transaction=transaction) for k, r in inv_refs.items()}

        # проверяем дефицит
        shortages = []
        for ing, qty_need in need.items():
            snap = inv_snaps[ing]
            if not snap.exists:
                shortages.append({"ingredient": ing, "need": float(qty_need), "have": 0.0})
                continue
            cur = float((snap.to_dict() or {}).get("current", 0))
            if cur < qty_need:
                shortages.append({"ingredient": ing, "need": float(qty_need), "have": float(cur)})
        if shortages:
            raise RuntimeError(jsonify_shortages(shortages))

        # списываем
        for ing, qty_need in need.items():
            snap = inv_snaps[ing]
            cur = float((snap.to_dict() or {}).get("current", 0))
            new_val = cur - float(qty_need)
            transaction.update(inv_refs[ing], {"current": new_val, "updated_at": firestore.SERVER_TIMESTAMP})

        # пишем продажу + лог
        sale_ref = db.collection("sales").document()
        transaction.set(sale_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "items": [
                {
                    "product_id": it["product_id"],
                    "product_name": it["product_name"],
                    "volume_ml": it["volume_ml"],
                    "qty": it["qty"],
                    "addons": it.get("addons", []),
                    "price_total_item": price_of_item(it),
                } for it in cart
            ],
            "total_amount": int(total_amount),
            "inventory_delta": {k: -float(v) for k,v in need.items()},
        })

        log_ref = db.collection("inventory_log").document()
        transaction.set(log_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "type": "sale",
            "delta": {k: -float(v) for k,v in need.items()},
            "meta": {"sale_id": sale_ref.id}
        })

    db.transaction()(_tx)
    return {"ok": True, "total_amount": int(total_amount)}

def jsonify_shortages(shortages: List[Dict[str,Any]]) -> str:
    # компактный текст для UI
    parts = []
    for s in shortages:
        need = s["need"]; have = s["have"]
        parts.append(f"{s['ingredient']}: нужно {need}, есть {have}, нехватка {round(need-have,2)}")
    return "Недостаточно запасов → " + "; ".join(parts)

