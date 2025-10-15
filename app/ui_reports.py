import streamlit as st
from google.cloud import firestore

from app.services.inventory import fetch_inventory
from app.logic.thresholds import inv_status
from app.utils.format import fmt_money_kop


def render_reports(db: firestore.Client):
    st.subheader("Рецепты • Отчёты (MVP)")

    col1, col2 = st.columns(2)
    with col1:
        st.caption("Последние 30 продаж:")
        sales = list(
            db.collection("sales")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(30)
            .stream()
        )
        if not sales:
            st.info("Пока нет продаж.")
        else:
            for s in sales:
                d = s.to_dict()
                st.write(f"- **{fmt_money_kop(int(d.get('total_amount',0)))}**, позиций: {len(d.get('items',[]))}")

    with col2:
        st.caption("Ингредиенты на исходе (🟠/🔴):")
        inv = fetch_inventory(db)
        danger = []
        for x in inv.values():
            icon, ratio = inv_status(x["capacity"], x["current"])
            if icon in ("🟠", "🔴"):
                danger.append(f"{icon} {x['name']} — {x['current']}/{x['capacity']} {x['unit']}")
        if not danger:
            st.success("Критичных остатков нет.")
        else:
            for line in danger:
                st.write("• " + line)
