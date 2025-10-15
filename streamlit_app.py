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

# ===== Общие утилиты (используются и в модуле, и в фоллбэке) =====
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
                st.write("pk begins with BEGIN:", str(j.get("private_key","")).strip().startswith("-----BEGIN"))
            except Exception as e:
                st.write("json ok:", False, str(e))
        elif isinstance(svc, dict):
            st.write("pk begins with BEGIN:", str(svc.get("private_key","")).strip().startswith("-----BEGIN"))

def list_repo_tree(max_entries=300):
    rows = []
    for root, dirs, files in os.walk(ROOT):
        # ограничимся разумным числом строк, чтобы не грузить UI
        if len(rows) > max_entries:
            rows.append("… (truncated)")
            break
        rel = os.path.relpath(root, ROOT)
        rows.append(f"[DIR] {'.' if rel == '.' else rel}")
        for f in files:
            rows.append(f"     └─ {f}")
    return rows

# ====== Фоллбэк-инициализация Firestore ======
@st.cache_resource
def get_db_fallback() -> firestore.Client:
    project_id = st.secrets.get("PROJECT_ID")
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not project_id:
        st.error("❌ В secrets отсутствует PROJECT_ID."); st.stop()
    if not svc:
        st.error("❌ В secrets отсутствует FIREBASE_SERVICE_ACCOUNT."); st.stop()
    try:
        info = json.loads(svc) if isinstance(svc, str) else dict(svc)
        creds = service_account.Credentials.from_service_account_info(info)
        db = firestore.Client(credentials=creds, project=project_id)
        _ = list(db.collections())
        return db
    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        st.stop()

# ====== Фоллбэк-экраны (минимум, но рабочие) ======
def render_sale_fallback(db: firestore.Client):
    st.subheader("Продажи (fallback)")
    st.info("Это резервный режим. Модули `app/...` не найдены. Снизу — подсказки, как починить структуру.")

def render_inventory_fallback(db: firestore.Client):
    st.subheader("Склад (fallback)")
    st.write("Попробуй открыть Firestore → `inventory` — данные доступны, UI модулем появится после исправления импорта.")

def render_reports_fallback(db: firestore.Client):
    st.subheader("Рецепты • Отчёты (fallback)")
    st.write("После починки модулей тут появятся отчёты и рецепты.")

# ====== Главный UI ======
def main():
    st.set_page_config(page_title="Gipsy Office — учёт", layout="wide", initial_sidebar_state="expanded")
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

    # Если мы в фоллбэке — покажем диагностику импорта и дерево проекта
    if USE_FALLBACK:
        st.sidebar.markdown("---")
        st.sidebar.error("⚠️ Модульная структура не импортируется")
        st.sidebar.code(f"{type(IMPORT_ERR).__name__}: {IMPORT_ERR}")
        with st.sidebar.expander("📁 Текущее дерево проекта (top)", expanded=False):
            for line in list_repo_tree():
                st.text(line)
        st.sidebar.markdown(
