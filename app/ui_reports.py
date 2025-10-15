from __future__ import annotations
import streamlit as st
from google.cloud import firestore

def render_reports(db: firestore.Client):
    st.subheader("Отчёты (MVP)")

    st.markdown("**Скоро закончится** (🟠/🔴)")
    low = []
    for d in db.collection("inventory").stream():
        v = d.to_dict(); cap = float(v.get("capacity") or 0) or 1.0
        cur = float(v.get("current") or 0)
        ratio = cur / cap if cap > 0 else 0
        if ratio < 0.5:
            low.append((v.get("name", d.id), ratio))
    if low:
        for name, r in sorted(low, key=lambda x:x[1]):
            st.write(f"• {name}: {int(r*100)}%")
    else:
        st.write("Нет позиций в зоне риска.")

    st.markdown("---")
    st.markdown("**Последние продажи**")
    sales = db.collection("sales").order_by("created_at", direction=firestore.Query.DESCENDING).limit(20).stream()
    for s in sales:
        v = s.to_dict()
        st.write(f"- {v.get('created_at')} — {v.get('total_amount',0)/100:.0f} ₽, позиций: {len(v.get('items',[]))}")

