from typing import List, Tuple, Dict
from google.cloud import firestore
from app.logic.calc import total_cart_consumption


def commit_sale(
    db: firestore.Client,
    cart: List[dict],
    products: Dict[str, dict],
    recipes: Dict[str, dict],
) -> Tuple[bool, str]:

    need = total_cart_consumption(cart, products, recipes)

    @firestore.transactional
    def _txn(transaction: firestore.Transaction):
        inv_refs = {iid: db.collection("inventory").document(iid) for iid in need.keys()}
        snaps = {iid: inv_refs[iid].get(transaction=transaction) for iid in inv_refs}

        # check
        for iid, req in need.items():
            cur = float(snaps[iid].to_dict().get("current", 0.0) if snaps[iid].exists else 0.0)
            if cur + 1e-9 < req:
                raise RuntimeError(f"Недостаточно '{iid}': нужно {req}, есть {cur}")

        # update
        for iid, req in need.items():
            cur = float(snaps[iid].to_dict().get("current", 0.0))
            transaction.update(inv_refs[iid], {"current": cur - req, "updated_at": firestore.SERVER_TIMESTAMP})

        total_amount = sum(int(i["price_total"]) for i in cart)
        sale_ref = db.collection("sales").document()
        transaction.set(sale_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "items": cart,
            "total_amount": total_amount,
            "inventory_delta": {k: -v for k, v in need.items()},
        })
        log_ref = db.collection("inventory_log").document()
        transaction.set(log_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "type": "sale",
            "delta": {k: -v for k, v in need.items()},
            "sale_id": sale_ref.id
        })
        return sale_ref.id

    try:
        sid = _txn(db.transaction())
        return True, sid
    except Exception as e:
        return False, str(e)
