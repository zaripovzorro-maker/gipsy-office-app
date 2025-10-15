from __future__ import annotations
import streamlit as st

from app.services.firestore_client import get_db, secrets_diag
from app.ui_sale import render_sale
from app.ui_inventory import render_inventory
from app.ui_reports import render_reports

st.set_page_config(page_title="Gipsy Office — учёт", page_icon="☕", layout="wide")

def main():
    with st.sidebar:
        st.header("Gipsy Office")
        secrets_diag()

    db = get_db()  # инициализация Firestore (через Secrets)

    tabs = st.tabs(["Продажа", "Остатки", "Отчёты"])
    with tabs[0]:
        render_sale(db)
    with tabs[1]:
        render_inventory(db)
    with tabs[2]:
        render_reports(db)

if __name__ == "__main__":
    main()
