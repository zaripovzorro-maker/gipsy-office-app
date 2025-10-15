import os
import sys
import json
import streamlit as st

# гарантируем, что корень репозитория в пути поиска модулей
ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ===== Пытаемся импортнуть модульные экранчики =====
USE_FALLBACK = False
try:
    from app.services.firestore_client import get_db as _get_db
    from app.ui_sale import render_sale as _render_sale
    from app.ui_inventory import render_inventory as _render_inventory
    from app.ui_reports import render_reports as _render_reports
except Exception as e:
    USE_FALLBACK = True
    IMPORT_ERR = e

# ===== Общие утилиты =====
from google.cloud import firestore
from google.oauth2 import service_account


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


def list_repo_tree(max_entries=300):
    rows = []
    for root, dirs, files in os.walk(ROOT):
        if len(rows) > max_entries:
            rows.append("… (truncated)")
            break
        rel = os.path.relpath(root, ROOT)
        rows.append(f"[DIR] {'.' if rel == '.' else rel}")
        for f in files:
            rows.append(f"     └─ {f}")
    return rows


# ===== Фоллбэк Firestore =====
@st.cache_resource
def get_db_fallback() -> firestore.Client:
    project_id = st.secrets.get("PROJECT_ID")
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not project_id:
        st.error("❌ В secrets отсутствует PROJECT_ID.")
        st.stop()
    if not svc:
        st.error("❌ В secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")
        st.stop()
    try:
        info = json.loads(svc) if isinstance(svc, str) else dict(svc)
        creds = service_account.Credentials.from_service_account_info(info)
        db = firestore.Client(credentials=creds, project=project_id)
        _ = list(db.collections())
        return db
    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        st.stop()


# ===== Фоллбэк-экраны =====
def render_sale_fallback(db: firestore.Client):
    st.subheader("Продажи (fallback)")
    st.info("Модули `app/...` не найдены. Снизу — подсказка, как исправить структуру.")


def render_inventory_fallback(db: firestore.Client):
    st.subheader("Склад (fallback)")
    st.write("Firestore подключён, но UI-модуль не найден.")


def render_reports_fallback(db: firestore.Client):
    st.subheader("Рецепты • Отчёты (fallback)")
    st.write("После исправления структуры появятся отчёты.")


# ===== Главный UI =====
def main():
    st.set_page_config(
        page_title="Gipsy Office — учёт",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("gipsy office — учёт")

    # если модульная структура ОК — используем её; иначе фоллбэк
    if not USE_FALLBACK:
        get_db = _get_db
        render_sale = _render_sale
        render_inventory = _render_inventory
        render_reports = _render_reports
    else:
        get_db = get_db_fallback
        render_sale = render_sale_fallback
        render_inventory = render_inventory_fallback
        render_reports = render_reports_fallback

    db = get_db()

    st.sidebar.header("Навигация")
    page = st.sidebar.radio(
        "Раздел",
        ["Продажи", "Склад", "Рецепты • Отчёты"],
        index=0,
        label_visibility="collapsed",
    )

    sidebar_secrets_check()

    if USE_FALLBACK:
        st.sidebar.markdown("---")
        st.sidebar.error("⚠️ Модульная структура не импортируется")
        st.sidebar.code(f"{type(IMPORT_ERR).__name__}: {IMPORT_ERR}")
        with st.sidebar.expander("📁 Текущее дерево проекта (top)", expanded=False):
            for line in list_repo_tree():
                st.text(line)

        # безопасно вставляем подсказку с помощью st.sidebar.write вместо тройных кавычек
        st.sidebar.write(
            "**Как должно быть в репозитории (всё в корне):**\n"
            "```\n"
            "streamlit_app.py\n"
            "app/\n"
            "  __init__.py\n"
            "  ui_sale.py\n"
            "  ui_inventory.py\n"
            "  ui_reports.py\n"
            "  services/\n"
            "    __init__.py\n"
            "    firestore_client.py\n"
            "    inventory.py\n"
            "    products.py\n"
            "    sales.py\n"
            "  logic/\n"
            "    __init__.py\n"
            "    calc.py\n"
            "    thresholds.py\n"
            "  utils/\n"
            "    __init__.py\n"
            "    format.py\n"
            "```\n"
            "⚙️ Убедись, что:\n"
            "- `app/` находится рядом со `streamlit_app.py`\n"
            "- В каждой папке есть пустой `__init__.py`\n"
            "- Имена файлов совпадают (регистр букв важен)"
        )

    st.divider()
    if page == "Продажи":
        render_sale(db)
    elif page == "Склад":
        render_inventory(db)
    else:
        render_reports(db)


if __name__ == "__main__":
    main()
