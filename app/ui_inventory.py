import streamlit as st
from google.cloud import firestore

from app.services.inventory import fetch_inventory
from app.logic.thresholds import inv_status


def render_inventory(db: firestore.Client):
    st.subheader("Склад")

    inv = fetch_inventory(db)
    if not inv:
        st.info("Пока нет записей в `inventory`.")
        return

    rows = []
    for iid, d in inv.items():
        icon, ratio = inv_status(d["capacity"], d["current"])
        rows.append({
            "Ингредиент": d["name"],
            "Текущее": d["current"],
            "Ед.": d["unit"],
            "Макс.": d["capacity"],
            "Статус": icon,
            "Заполненность %": round(ratio * 100, 1),
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("➕ Пополнение / корректировка"):
        choice = st.selectbox("Ингредиент", options=list(inv.keys()), format_func=lambda k: inv[k]["name"])
        delta = st.number_input("Изменение (плюс к текущему)", value=0.0, step=10.0)
        if st.button("Сохранить"):
            ref = db.collection("inventory").document(choice)
            ref.update({"current": firestore.Increment(float(delta)), "updated_at": firestore.SERVER_TIMESTAMP})
            db.collection("inventory_log").add({
                "created_at": firestore.SERVER_TIMESTAMP,
                "type": "restock" if delta >= 0 else "adjust",
                "delta": {choice: float(delta)}
            })
            st.success("Обновлено.")
            st.experimental_rerun()
