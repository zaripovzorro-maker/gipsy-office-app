import os
import sys
import json
import importlib.util
from types import ModuleType
import streamlit as st

# ---------- где искать ----------
# Текущая папка файла
HERE = os.path.dirname(os.path.abspath(__file__))

# Кандидаты для обхода: текущая, 1–3 родителя, и /mount/src (корень чекаута на Streamlit)
SEARCH_ROOTS = []
cur = HERE
for _ in range(4):
    if cur not in SEARCH_ROOTS:
        SEARCH_ROOTS.append(cur)
    parent = os.path.dirname(cur)
    if parent == cur:
        break
    cur = parent

# Иногда Streamlit кладёт репозиторий в /mount/src/<repo>
MOUNT_SRC = "/mount/src"
if os.path.exists(MOUNT_SRC):
    SEARCH_ROOTS.append(MOUNT_SRC)

# гарантируем sys.path
for p in [HERE] + SEARCH_ROOTS:
    if p not in sys.path:
        sys.path.insert(0, p)


def repo_tree(max_lines=600):
    """Показывает, что реально видит рантайм (для дебага)."""
    rows = []
    seen = set()
    for root in SEARCH_ROOTS:
        if not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        rows.append(f"=== ROOT: {root} ===")
        for r, dirs, files in os.walk(root):
            rel = os.path.relpath(r, root)
            rows.append(f"[DIR] {'.' if rel == '.' else rel}")
            for f in files:
                rows.append(f"     └─ {f}")
            if len(rows) >= max_lines:
                rows.append("… (truncated)")
                return "\n".join(rows)
    return "\n".join(rows)


def find_file(filename: str) -> str | None:
    """Ищет первый попавшийся файл по имени в SEARCH_ROOTS."""
    for root in SEARCH_ROOTS:
        for r, _, files in os.walk(root):
            if filename in files:
                return os.path.join(r, filename)
    return None


def load_module_from_path(mod_name: str, file_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {mod_name} at {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod


def try_import_modules():
    """
    1) Пытаемся обычные импорты (если app/ в PYTHONPATH)
    2) Иначе ищем файлы по именам и грузим напрямую
    """
    try:
        from app.services.firestore_client import get_db as get_db  # type: ignore
        from app.ui_sale import render_sale                       # type: ignore
        from app.ui_inventory import render_inventory             # type: ignore
        from app.ui_reports import render_reports                 # type: ignore
        return get_db, render_sale, render_inventory, render_reports, None
    except Exception as e_primary:
        # поимённый поиск
        targets = {
            "fc": "firestore_client.py",
            "sale": "ui_sale.py",
            "inv": "ui_inventory.py",
            "rep": "ui_reports.py",
        }
        paths = {k: find_file(v) for k, v in targets.items()}
        missing = [v for k, v in targets.items() if not paths[k]]
        if missing:
            return None, None, None, None, (e_primary, f"Не найдены файлы: {', '.join(missing)}")

        try:
            fc_mod = load_module_from_path("dyn_firestore_client", paths["fc"])  # type: ignore
            sale_mod = load_module_from_path("dyn_ui_sale", paths["sale"])       # type: ignore
            inv_mod = load_module_from_path("dyn_ui_inventory", paths["inv"])    # type: ignore
            rep_mod = load_module_from_path("dyn_ui_reports", paths["rep"])      # type: ignore

            get_db = getattr(fc_mod, "get_db")
            render_sale = getattr(sale_mod, "render_sale")
            render_inventory = getattr(inv_mod, "render_inventory")
            render_reports = getattr(rep_mod, "render_reports")
            return get_db, render_sale, render_inventory, render_reports, None
        except Exception as e_fallback:
            return None, None, None, None, (e_primary, e_fallback)


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
                st.write("pk begins with BEGIN:", str(j.get("private_key", "")).strip().startswith("-----BEGIN"))
            except Exception as e:
                st.write("json ok:", False, str(e))
        elif isinstance(svc, dict):
            st.write("pk begins with BEGIN:", str(svc.get("private_key", "")).strip().startswith("-----BEGIN"))


# ---------- fallback экраны ----------
def render_sale_fallback(*_):
    st.subheader("Продажи (fallback)")
    st.info("Не найден модуль `ui_sale.py`. Проверь структуру репозитория (см. сайдбар).")

def render_inventory_fallback(*_):
    st.subheader("Склад (fallback)")

def render_reports_fallback(*_):
    st.subheader("Рецепты • Отчёты (fallback)")


# ---------- run ----------
def main():
    st.set_page_config(page_title="Gipsy Office — учёт", layout="wide", initial_sidebar_state="expanded")
    st.title("gipsy office — учёт")

    get_db, render_sale, render_inventory, render_reports, import_errs = try_import_modules()

    st.sidebar.header("Навигация")
    page = st.sidebar.radio("Раздел", ["Продажи", "Склад", "Рецепты • Отчёты"], index=0, label_visibility="collapsed")
    sidebar_secrets_check()

    if import_errs:
        st.sidebar.markdown("---")
        st.sidebar.error("⚠️ Не удалось импортировать модули.")
        e1, e2 = import_errs
        st.sidebar.code(f"primary: {e1}\nfallback: {e2}")
        st.sidebar.caption("Дерево того, что реально видит рантайм:")
        st.sidebar.code(repo_tree())

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
            "Если файлы лежат глубже — этот файл всё равно их найдёт, но они должны **существовать в чекауте**."
        )

    # Если импорты провалились — показываем фоллбэки, но приложение работает
    if get_db is None:
        render_sale = render_sale_fallback
        render_inventory = render_inventory_fallback
        render_reports = render_reports_fallback
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
