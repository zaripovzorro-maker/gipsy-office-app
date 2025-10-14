# streamlit_app.py
# gipsy office — POS для бариста (категории → напитки → объём → корзина → покупка)

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import streamlit as st

# Firebase / Firestore
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore


# =========================
# Инициализация Firestore
# =========================
def init_firestore() -> firestore.Client:
    import json, os
    from collections.abc import Mapping
    import streamlit as st
    from google.oauth2 import service_account
    from google.cloud import firestore

    project_id = (st.secrets.get("PROJECT_ID") or os.getenv("PROJECT_ID") or "").strip()
    svc_raw = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)

    # Диагностика (без утечек содержимого)
    st.sidebar.write("🔍 Secrets check")
    st.sidebar.write(f"- PROJECT_ID present: {bool(project_id)}")
    st.sidebar.write(f"- FIREBASE_SERVICE_ACCOUNT type: {type(svc_raw).__name__ if svc_raw is not None else 'None'}")

    if not project_id:
        st.error("❌ В secrets нет PROJECT_ID.")
        st.stop()
    if svc_raw is None:
        st.error("❌ В secrets нет FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    # Приводим к dict
    if isinstance(svc_raw, Mapping):
        data = dict(svc_raw)
    elif isinstance(svc_raw, str):
        try:
            data = json.loads(svc_raw)
        except Exception as e:
            st.error(f"❌ FIREBASE_SERVICE_ACCOUNT: невалидный JSON-строкой ({e}). "
                     "Если используешь TOML-таблицу, не заключай её в кавычки.")
            st.stop()
    else:
        st.error("❌ FIREBASE_SERVICE_ACCOUNT должен быть таблицей TOML или JSON-строкой.")
        st.stop()

    # Валидация ключевых полей
    required_keys = ["type", "project_id", "private_key_id", "private_key", "client_email", "token_uri"]
    missing = [k for k in required_keys if not data.get(k)]
    if missing:
        st.error(f"❌ В service account отсутствуют поля: {', '.join(missing)}. "
                 "Скопируй JSON из Firebase консоли без изменений.")
        st.stop()

    # Нормализация приватного ключа
    pk = data.get("private_key", "")
    # Если ключ пришёл с литералами \r\n или \\n — превращаем в реальные переводы строк
    if "\\n" in pk and "\n" not in pk:
        pk = pk.replace("\\r\\n", "\n").replace("\\n", "\n")
    # Убираем возможные лишние пробелы по краям
    pk = pk.strip()
    data["private_key"] = pk

    # Доп. диагностика по ключу (без вывода содержимого)
    st.sidebar.write(f"- private_key length: {len(pk)}")
    st.sidebar.write(f"- starts with BEGIN: {pk.startswith('-----BEGIN PRIVATE KEY-----')}")
    st.sidebar.write(f"- contains newline: {('\\n' in pk) or (chr(10) in pk)}")

    # Пробуем создать креды
    try:
        creds = service_account.Credentials.from_service_account_info(data)
    except Exception as e:
        st.error("❌ Не удалось обработать service account. Чаще всего это из-за поломанного private_key "
                 "(попали лишние символы/кавычки). "
                 "Ещё раз скопируй JSON из Firebase и вставь в Secrets ровно как есть "
                 "(для JSON — одной строкой с \\n; для TOML — многострочно без \\n).")
        st.stop()

    try:
        return firestore.Client(project=project_id, credentials=creds)
    except Exception as e:
        st.error(f"❌ Firestore client init failed: {e}")
        st.stop()

db: firestore.Client = init_firestore()


# =========================
# Константы / настройки
# =========================

# Предустановленные размеры (лейбл, множитель объёма/рецепта, множитель цены)
SIZE_PRESETS: List[Tuple[str, float, float]] = [
    ("S", 1.00, 1.00),
    ("M", 1.40, 1.35),
    ("L", 1.80, 1.70),
]

# Лёгкая палитра / css
LIGHT_CSS = """
<style>
/* базовый фон */
section.main > div {background: #ffffff;}
/* тайлы-категории и продукты */
.tile {
  border: 2px solid #E5E7EB;
  border-radius: 16px;
  padding: 14px 14px 10px 14px;
  text-align: center;
  background: #F9FAFB;
  transition: all 140ms ease;
}
.tile:hover { transform: translateY(-2px); border-color: #60A5FA; box-shadow: 0 4px 16px rgba(96,165,250,.25); }

.tile-title {
  font-weight: 700;
  letter-spacing: .3px;
  color: #111827;
}
.tile-sub {
  font-size: 12px;
  color: #6B7280;
  margin-top: 4px;
}

.tile-selected {
  border-color: #2563EB !important;
  box-shadow: 0 0 0 3px rgba(37,99,235,.15);
}

.badge {
  display: inline-block;
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 10px;
  background: #EEF2FF;
  color: #3730A3;
  margin-top: 6px;
}

.size-btn {
  display:inline-block;
  padding:8px 14px;
  border-radius: 10px;
  border: 1px solid #E5E7EB;
  margin-right:6px;
  margin-bottom:6px;
  background:#FFF;
  cursor:pointer;
}
.size-btn-active {
  border-color:#2563EB;
  background:#EFF6FF;
  color:#1E3A8A;
  box-shadow: 0 0 0 2px rgba(37,99,235,.2);
}

.qty-box {
  display:flex; align-items:center; gap:8px; margin-top:8px;
}
.qty {
  width:56px; text-align:center; font-weight:700; padding:6px 8px; border-radius:8px; border:1px solid #E5E7EB
}

.cart {
  border-left: 1px solid #E5E7EB;
  padding-left: 16px;
}
.cart-item {
  display:flex; justify-content:space-between; align-items:center;
  background:#F3F4F6; padding:10px 12px; border-radius:12px; margin-bottom:8px;
}
.cart-sum {
  margin-top:10px; padding-top:10px; border-top:1px dashed #E5E7EB;
  display:flex; justify-content:space-between; font-weight:800;
}
.btn-solid {
  background:#111827; color:#FFF; padding:12px 16px; border-radius:12px; font-weight:800; text-align:center;
}
.btn-outline {
  border:2px solid #111827; padding:10px 14px; border-radius:12px; font-weight:700; text-align:center;
}
.note {
  background:#FFFBEB; color:#92400E; padding:8px 12px; border-radius:10px; font-size:12px; border:1px solid #FCD34D;
}
</style>
"""
st.set_page_config(page_title="gipsy office — продажи", page_icon="☕", layout="wide")
st.markdown(LIGHT_CSS, unsafe_allow_html=True)


# =========================
# Доступ к данным
# =========================
def get_ingredients_map() -> Dict[str, Dict]:
    """Словарь ингредиентов: id -> {name, stock_quantity, unit, ...}"""
    res = {}
    for d in db.collection("ingredients").stream():
        res[d.id] = d.to_dict()
    return res


def get_products() -> List[Dict]:
    """Список продуктов."""
    out = []
    for d in db.collection("products").order_by("name").stream():
        doc = d.to_dict()
        doc["id"] = d.id
        out.append(doc)
    return out


def get_categories(products: List[Dict]) -> List[str]:
    cats = sorted({p.get("category", "прочее") for p in products})
    return cats


def get_recipe(product_id: str) -> Dict:
    """Документ из коллекции 'recipes' (опционально). Формат:
       { items: [ {ingredientId: "beans", qtyPer: 18}, ... ] }"""
    snap = db.collection("recipes").document(product_id).get()
    return snap.to_dict() or {"items": []}


# =========================
# Корзина (session_state)
# =========================
def _ensure_state():
    st.session_state.setdefault("category", None)
    st.session_state.setdefault("selected_product", None)  # id
    st.session_state.setdefault("selected_size", "M")
    st.session_state.setdefault("qty", 1)
    st.session_state.setdefault("cart", [])


def reset_selection():
    st.session_state["selected_product"] = None
    st.session_state["selected_size"] = "M"
    st.session_state["qty"] = 1


def add_to_cart(prod: Dict, size_lbl: str, qty: int):
    # цена за 1 с учётом размера
    base_price = float(prod.get("price", 0.0))
    # если в продукте есть map sizes — берём оттуда, иначе считаем множителем
    sizes_map = prod.get("sizes", {})
    if size_lbl in sizes_map:
        unit_price = float(sizes_map[size_lbl])
    else:
        # fallback по нашей таблице
        mult = next((m for (lbl, _, m) in SIZE_PRESETS if lbl == size_lbl), 1.0)
        unit_price = round(base_price * mult)

    st.session_state["cart"].append(
        {
            "product_id": prod["id"],
            "name": prod.get("name", ""),
            "size": size_lbl,
            "qty": int(qty),
            "unit_price": unit_price,
        }
    )


def cart_total() -> float:
    return float(sum(item["unit_price"] * item["qty"] for item in st.session_state["cart"]))


def remove_cart_item(idx: int):
    if 0 <= idx < len(st.session_state["cart"]):
        st.session_state["cart"].pop(idx)


def clear_cart():
    st.session_state["cart"].clear()


# =========================
# Списание при покупке
# =========================
def commit_sale(cart: List[Dict]) -> Tuple[bool, str]:
    """Проверяет остатки, если хватает — списывает и создаёт документ в sales."""
    if not cart:
        return False, "Корзина пуста."

    ingredients_cache = get_ingredients_map()

    # 1) посчитать требуемые количества по ингредиентам
    required: Dict[str, float] = {}
    for item in cart:
        pid = item["product_id"]
        qty = item["qty"]
        size_lbl = item["size"]

        # множитель рецепта по размеру
        size_mult = next((vol for (lbl, vol, _) in SIZE_PRESETS if lbl == size_lbl), 1.0)

        recipe = get_recipe(pid)
        for r in recipe.get("items", []):
            ing = r.get("ingredientId")
            base = float(r.get("qtyPer", 0.0))
            need = base * size_mult * qty
            required[ing] = required.get(ing, 0.0) + need

    # 2) проверка остатков
    lacks = []
    for ing_id, req in required.items():
        info = ingredients_cache.get(ing_id)
        if not info:
            # игнорируем неизвестный ингредиент — можно трактовать как предупреждение
            continue
        have = float(info.get("stock_quantity", 0.0))
        if have < req:
            lacks.append(
                f"{info.get('name', ing_id)}: нужно {req:.0f} {info.get('unit','')}, есть {have:.0f}"
            )

    if lacks:
        return False, "Не хватает:\n- " + "\n- ".join(lacks)

    # 3) транзакционно списать и записать продажу
    def _tx_func(tx: firestore.Transaction):
        # списание
        for ing_id, req in required.items():
            doc_ref = db.collection("ingredients").document(ing_id)
            snap = doc_ref.get(transaction=tx)
            if not snap.exists:
                continue
            cur = float(snap.to_dict().get("stock_quantity", 0.0))
            next_v = cur - req
            if next_v < -1e-6:
                raise ValueError("На полке закончилось во время покупки.")
            tx.update(doc_ref, {"stock_quantity": next_v})

        # запись чека
        sale_doc = {
            "created_at": datetime.now(timezone.utc),
            "items": cart,
            "total": cart_total(),
        }
        tx.set(db.collection("sales").document(), sale_doc)

    try:
        db.transaction()( _tx_func )
        return True, "Готово! Продажа проведена."
    except Exception as e:
        return False, f"Ошибка транзакции: {e}"


# =========================
# UI — Шапка
# =========================
st.title("☕ gipsy office — продажи")

# заметка
st.markdown(
    """
<div class="note">
Продажа проводится только при нажатии <b>«Купить»</b>. До этого позиции лежат в корзине и
остатки не меняются.
</div>
""",
    unsafe_allow_html=True,
)

_ensure_state()

# =========================
# Загружаем данные
# =========================
products = get_products()
categories = get_categories(products)
prod_map = {p["id"]: p for p in products}


# =========================
# Разметка: левая панель (категории, продукты, размер) + правая корзина
# =========================
left, right = st.columns([7, 5], gap="large")

with left:
    # --- Категории плитками ---
    st.subheader("Категории")
    cols = st.columns(6)
    for i, cat in enumerate(categories):
        c = cols[i % 6]
        is_sel = (st.session_state["category"] == cat)
        block = f"""
        <div class="tile {'tile-selected' if is_sel else ''}">
          <div class="tile-title">{cat.title()}</div>
          <div class="tile-sub">напитки</div>
        </div>
        """
        # клик-плитка
        if c.button(block, key=f"cat_{cat}", use_container_width=True):
            st.session_state["category"] = cat
            reset_selection()
        c.markdown("&nbsp;", unsafe_allow_html=True)

    st.markdown("---")

    # --- Продукты выбранной категории ---
    cur_cat = st.session_state["category"] or (categories[0] if categories else None)
    if cur_cat and cur_cat not in categories and categories:
        cur_cat = categories[0]
        st.session_state["category"] = cur_cat

    st.subheader(f"Напитки — {cur_cat.title() if cur_cat else '—'}")

    grid = st.columns(4)
    cat_products = [p for p in products if p.get("category") == cur_cat] if cur_cat else []

    for i, p in enumerate(cat_products):
        col = grid[i % 4]
        pid = p["id"]
        selected = (st.session_state["selected_product"] == pid)

        # Подпись цены (базовая)
        base_price = float(p.get("price", 0.0))
        subtitle = f"{int(base_price)} ₽ (база)"
        html = f"""
        <div class="tile {'tile-selected' if selected else ''}">
          <div class="tile-title">{p.get('name','')}</div>
          <div class="tile-sub">{subtitle}</div>
        </div>
        """
        if col.button(html, key=f"prod_{pid}", use_container_width=True):
            st.session_state["selected_product"] = pid
            st.session_state["selected_size"] = "M"
            st.session_state["qty"] = 1

    
    st.markdown("---")

    # --- Параметры выбранного напитка ---
    if st.session_state["selected_product"]:
        prod = prod_map[st.session_state["selected_product"]]
        st.subheader(f"Выбрано: {prod.get('name','')}")

        # размеры
        st.markdown("**Объём / размер**")

        # если у продукта есть map sizes — показываем цены оттуда
        sizes_map = prod.get("sizes", {})

        size_cols = st.columns(6)
        for lbl, vol_mult, price_mult in SIZE_PRESETS:
            if lbl in sizes_map:
                price_lbl = int(sizes_map[lbl])
            else:
                price_lbl = int(round(float(prod.get("price", 0.0)) * price_mult))
            active = (st.session_state["selected_size"] == lbl)
            html_btn = f"""
            <span class="size-btn {'size-btn-active' if active else ''}">
              <b>{lbl}</b>&nbsp;&nbsp;—&nbsp;{price_lbl} ₽
            </span>
            """
            if size_cols[SIZE_PRESETS.index((lbl, vol_mult, price_mult)) % 6].button(
                html_btn, key=f"size_{lbl}"
            ):
                st.session_state["selected_size"] = lbl

        # qty
        st.markdown("**Количество**")
        qcols = st.columns([1, 2, 1, 6])
        if qcols[0].button("−", use_container_width=True):
            st.session_state["qty"] = max(1, st.session_state["qty"] - 1)
        qcols[1].markdown(f"<div class='qty' style='text-align:center'>{st.session_state['qty']}</div>", unsafe_allow_html=True)
        if qcols[2].button("+", use_container_width=True):
            st.session_state["qty"] = st.session_state["qty"] + 1

        st.markdown("")
        add_cols = st.columns([3, 2, 5])

        # калькуляция цены для кнопки
        cur_size = st.session_state["selected_size"]
        if cur_size in sizes_map:
            unit_price = int(sizes_map[cur_size])
        else:
            price_mult = next((m for (lbl, _, m) in SIZE_PRESETS if lbl == cur_size), 1.0)
            unit_price = int(round(float(prod.get("price", 0.0)) * price_mult))

        if add_cols[0].button(f"➕ В корзину · {unit_price * st.session_state['qty']} ₽", use_container_width=True):
            add_to_cart(prod, cur_size, st.session_state["qty"])
            st.success("Добавлено в корзину.")

        if add_cols[1].button("Очистить выбор", use_container_width=True):
            reset_selection()
            st.info("Выбор очищен.")


with right:
    st.subheader("🧺 Корзина")
    st.markdown("<div class='cart'>", unsafe_allow_html=True)

    if not st.session_state["cart"]:
        st.info("Корзина пуста. Добавьте напитки слева.")
    else:
        for idx, item in enumerate(st.session_state["cart"]):
            left_c, mid_c, right_c = st.columns([5, 3, 2])
            left_c.markdown(
                f"<div class='cart-item'><div><b>{item['name']}</b> — {item['size']} × {item['qty']}</div>"
                f"<div><b>{int(item['unit_price'] * item['qty'])} ₽</b></div></div>",
                unsafe_allow_html=True,
            )
            # Кнопка удаления справа:
            if right_c.button("✕", key=f"rm_{idx}"):
                remove_cart_item(idx)
                st.experimental_rerun()

        # Итого + кнопки
        st.markdown(
            f"<div class='cart-sum'><div>Итого</div><div>{int(cart_total())} ₽</div></div>",
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns([3, 2])
        if c1.button("✅ Купить", use_container_width=True):
            ok, msg = commit_sale(st.session_state["cart"])
            if ok:
                clear_cart()
                st.success(msg)
            else:
                st.error(msg)

        if c2.button("🗑 Очистить", use_container_width=True):
            clear_cart()
            st.info("Корзина очищена.")

    st.markdown("</div>", unsafe_allow_html=True)
