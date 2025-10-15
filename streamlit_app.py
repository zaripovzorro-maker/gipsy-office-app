# streamlit_app.py
import os
import json
import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account

# ======================================================
# Настройка пути (чтобы Streamlit видел пакет app/)
# ======================================================
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app.ui_sale import render_sale
from app.ui_inventory import render_inventory
from app.ui_reports import render_reports


# ======================================================
# Инициализация Firestore
# ======================================================
@st.cache_resource
def init_firestore():
    """Подключаемся к Firestore через Streamlit Secrets"""
    project_id = st.secrets.get("PROJECT_ID")
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")

    if not svc:
        st.error("❌ В секрете отсутствует FIREBASE_SERVICE_ACCOUNT")
        st.stop()

    try:
        # Проверим, TOML или JSON
        if isinstance(svc, str):
            data = json.loads(svc)
        else:
            data = dict(svc)

        creds = service_account.Credentials.from_service_account_info(data)
        db = firestore.Client(credentials=creds, project=project_id)
        return db

    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        st.stop()


# ======================================================
# Основной UI
# ======================================================
def main():
    st.set_page_config(
        page_title="Gipsy Office — учёт",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("gipsy office — учёт")

    db = init_firestore()

    # Боковое меню
    st.sidebar.header("Навигация")
    page = st.sidebar.radio(
        "",
        ["Продажи", "Склад", "Рецепты • Отчёты"],
        index=0,
        horizontal=False,
    )

    # Отладка секретов (можно выключить после проверки)
    with st.sidebar.expander("🔍 Secrets check"):
        st.write("• PROJECT_ID present:", bool(st.secrets.get("PROJECT_ID")))
        st.write("• FIREBASE_SERVICE_ACCOUNT type:", type(st.secrets.get("FIREBASE_SERVICE_ACCOUNT")).__name__)
        svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", "")
        if isinstance(svc, str):
            st.write("• contains \\n literal:", "\\n" in svc)
        if isinstance(svc, dict) and "private_key" in svc:
            pk = svc["private_key"]
            st.write("• private_key length:", len(pk))
            st.write("• starts with BEGIN:", pk.strip().startswith("-----BEGIN"))


    # Основная логика навигации
    st.divider()
    if page == "Продажи":
        render_sale(db)
    elif page == "Склад":
        render_inventory(db)
    else:
        render_reports(db)


# ======================================================
# Точка входа
# ======================================================
if __name__ == "__main__":
    main()
