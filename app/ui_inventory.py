from __future__ import annotations
import streamlit as st
from typing import Dict, Any
from google.cloud import firestore
from app.logic.thresholds import status_color

def render_inventory(db: firestore.Client):
    st.subheader("Остатки (inventory)")
    docs = list(db.collection("inventory").stream())
    if not docs:
        st.info("Коллекция `inventory` пуста. Запустите сид-скрипт или добавьте вручную.")
        return

    items = []
    for d in docs:
        v = d.to_dict(); v["id"] = d.id
        cap = float(v.get("capacity") or 0) or 1.0
        cur = float(v.get("current") or 0)
        ratio = max(0.0, min(1.0, cur / cap)) if cap > 0 else 0
        items.append((v, ratio))

    # сортировка по % остатка (возрастание)
    items.sort(key=lambda x: x[1])

    for v, ratio in items:
        icon = status_color(ratio)
        name = v.get("name", v["id"])
        unit = v.get("unit","")
        st.markdown(
            f"**{name}** — {int(v.get('current',0))}/{int(v.get('capacity',0))} {unit}  {icon} ({int(ratio*100)}%)"
        )

    st.write("---")
    st.subheader("Пополнение / корректировка")
    ids = [d.id for d in docs]
    ing_id = st.selectbox("Ингредиент", ids)
    delta = st.number_input("Изменение (плюс к current)", value=0.0, step=10.0)
    if st.button("Применить"):
        ref = db.collection("inventory").document(ing_id)
        def _tx(tx: firestore.Transaction):
            snap = ref.get(transaction=tx)
            cur = float((snap.to_dict() or {}).get("current", 0))
            tx.update(ref, {"current": cur + float(delta), "updated_at": firestore.SERVER_TIMESTAMP})
            db.collection("inventory_log").add({
                "created_at": firestore.SERVER_TIMESTAMP,
                "type": "adjust",
                "delta": {ing_id: float(delta)},
                "meta": {"ui": "manual_adjust"}
            })
        db.transaction()(_tx)
        st.success("Остаток изменён.")

