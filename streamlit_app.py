import json
import streamlit as st

# --- Локальные модули ---
from app.services.firestore_client import get_db
from app.ui_sale import render_sale
from app.ui_inventory import render_inventory
from app.ui_reports import render_reports


# ============================================================
# Проверка и отладка секретов (показывает при необходимости)
# ============================================================
def sidebar_secrets_check():
    with st.sidebar.expander("🔍 Secrets check", expanded=False):
        svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
        st.write("PROJECT_ID present:", bool(st.secrets.get("PROJECT_ID")))
        st.write("FIREBASE_SERVICE_ACCOUNT type:", type(svc).__name__)
        if isinstance(svc, str):
            st.write("contains \\n literal:", "\\n" in svc)
            try:
                j = json.loads(svc)
                st.write("json ok:", True)
                st.write(
                    "pk begins with BEGIN:",
                    str(j.get("private_key", "")).strip().startswith("-----BEGIN"),
                )
            except Exception as e:
                st.write("json ok:", False, str(e))
        elif isinstance(svc, dict):
            st.write(
                "pk begins with BEGIN:",
                str(svc.get("private_key", "")).strip().startswith("-----BEGIN"),
            )


# ============================================================
# Главная функция приложения
# ============================================================
def main():
    st.set_page_config(
        page_title="Gipsy Office — учёт",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("gipsy office — учёт")

    # Инициализация Firestore
    db = get_db()

    # Боковая панель навигации
    st.sidebar.header("Навигация")
    page = st.sidebar.radio(
        "Раздел",
        ["Продажи", "Склад", "Рецепты • Отчёты"],
        index=0,
        label_visibility="collapsed",
    )

    # Проверка секретов (можно свернуть)
    sidebar_secrets_check()

    st.divider()

    # --- Навигация между страницами ---
    if page == "Продажи":
        render_sale(db)
    elif page == "Склад":
        render_inventory(db)
    else:
        render_reports(db)


# ============================================================
# Точка входа
# ============================================================
if __name__ == "__main__":
    main()
