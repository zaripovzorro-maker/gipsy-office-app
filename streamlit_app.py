import os
import sys
import json
import importlib
import importlib.util
from types import ModuleType
import streamlit as st

# ---------- утилиты динамической загрузки ----------

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

def _repo_tree(max_lines=300):
    rows = []
    for root, dirs, files in os.walk(ROOT):
        if len(rows) >= max_lines:
            rows.append("… (truncated)")
            break
        rel = os.path.relpath(root, ROOT)
        rows.append(f"[DIR] {'.' if rel == '.' else rel}")
        for f in files:
            rows.append(f"     └─ {f}")
    return "\n".join(rows)

def _find_file(filename: str) -> str | None:
    """Рекурсивно ищет первый попавшийся файл с таким именем внутри репо."""
    for root, _, files in os.walk(ROOT):
        if filename in files:
            return os.path.join(root, filename)
    return None

def _load_module_from_path(mod_name: str, file_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {mod_name} at {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod

def _safe_import():
    """
    1) пытаемся обычные импорты пакета app.*
    2) если не вышло — ищем файлы по имени и подгружаем напрямую
    """
    try:
        from app.services.firestore_client import get_db as get_db  # type: ignore
        from app.ui_sale import render_sale                       # type: ignore
        from app.ui_inventory import render_inventory             # type: ignore
        from app.ui_reports import render_reports                 # type: ignore
        return get_db, render_sale, render_inventory, render_reports, None
    except Exception as e:
        # План Б: загружаем по путям файлов
        try:
            fc_path = _find_file("firestore_client.py")
            s_path  = _find_file("ui_sale.py")
            i_path  = _find_file("ui_inventory.py")
            r_path  = _find_file("ui_reports.py")

            missing = [n for n, p in [
                ("firestore_client.py", fc_path),
                ("ui_sale.py", s_path),
                ("ui_inventory.py", i_path),
                ("ui_reports.py", r_path),
            ] if p is None]
            if missing:
                msg = "Не нашёл файлы: " + ", ".join(missing)
                raise ImportError(msg)

            fc_mod = _load_module_from_path("dyn_firestore_client", fc_path)  # type: ignore
            s_mod  = _load_module_from_path("dyn_ui_sale", s_path)            # type: ignore
            i_mod  = _load_module_from_path("dyn_ui_inventory", i_path)       # type: ignore
            r_mod  = _load_module_from_path("dyn_ui_reports", r_path)         # type: ignore

            get_db = getattr(fc_mod, "get_db")
            render_sale = getattr(s_mod, "render_sale")
            render_inventory = getattr(i_mod, "render_inventory")
            render_reports = getattr(r_mod, "render_reports")
            return get_db, render_sale, render_inventory, render_reports, None
        except Exception as e2:
            return None, None, None, None, (e, e2)

# ---------- проверка secrets ----------

def sidebar_secrets_check():
    with st.sidebar.expander("🔍 Secrets check", expanded=False):
        st.write("PROJECT_ID present:", bool(st.secrets.get("PROJECT_ID")))
        svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
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

# ---------- фоллбэки экранов ----------

def render_sale_fallback(*_):
    st.subheader("Продажи (fallback)")
    st.info("UI-модуль не найден. Ниже подсказки по структуре.")

def render_inventory_fallback(*_):
    st.subheader("Склад (fallback)")

def render_reports_fallback(*_):
    st.subheader("Рецепты • Отчёты (fallback)")

# ---------- приложение ----------

def main():
    st.set_page_config(page_title="Gipsy Office — учёт", layout="wide", initial_sidebar_state="expanded")
    st.title("gipsy office — учёт")

    get_db, render_sale, render_inventory, render_reports, import_errs = _safe_import()

    st.sidebar.header("Навигация")
    page = st.sidebar.radio(
        "Раздел",
        ["Продажи", "Склад", "Рецепты • Отчёты"],
        index=0,
        label_visibility="collapsed",
    )

    sidebar_secrets_check()

    if import_errs:
        e1, e2 = import_errs
        st.sidebar.markdown("---")
        st.sidebar.error("⚠️ Модульная структура не импортируется")
        st.sidebar.code(f"primary: {type(e1).__name__}: {e1}\nfallback: {type(e2).__name__}: {e2}")
        st.sidebar.caption("Ниже дерево репозитория (первые ~300 строк):")
        st.sidebar.code(_repo_tree())

        # показываем, как должно быть
        st.sidebar.write(
            "**Ожидаемая структура рядом со `streamlit_app.py`:**\n"
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
            "Проверь регистр имён и наличие `__init__.py`."
        )

    # если импорт не удался — мягкие фоллбэки
    if get_db is None:
        render_sale = render_sale_fallback
        render_inventory = render_inventory_fallback
        render_reports = render_reports_fallback

        # без БД всё равно позволим открыть вкладки, чтобы увидеть подсказки
        db = None
    else:
        db = get_db()

    st.divider()
    if page == "Продажи":
        render_sale(db)
    elif page == "Склад":
        render_inventory(db)
    else:
        render_reports(db)

if __name__ == "__main__":
    main()
