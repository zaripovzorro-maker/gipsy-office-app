# streamlit_app.py
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

import streamlit as st
import pandas as pd

import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore

# -----------------------------
# Firestore init (через Secrets)
# -----------------------------
def init_firestore() -> firestore.Client:
    # PROJECT_ID обязателен
    project_id = st.secrets.get("PROJECT_ID", "").strip()
    if not project_id:
        st.error("В Secrets нет PROJECT_ID.")
        st.stop()

    # Ключ может быть JSON-строкой ИЛИ TOML-таблицей
    svc_raw = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)
    if svc_raw is None:
        st.error("В Secrets нет FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    if not firebase_admin._apps:
        if isinstance(svc_raw, str):
            try:
                data = json.loads(svc_raw)
            except Exception as e:
                st.error(
                    "Не удалось разобрать JSON-строку с сервисным ключом. "
                    "Проверь, что внутри private_key используются **двойные** слеши `\\n`."
                )
                st.stop()
        else:
            # TOML-таблица → dict
            data = dict(svc_raw)

        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, {"projectId": project_id})

    return firestore.Client(project=project_id)

db: firestore.Client = init_firestore()

# -----------------------------
# UI presets (светлая тема + CSS)
# -----------------------------
st.set_page_config(
    page_title="gipsy office — продажи",
    page_icon="☕",
    layout="wide",
)

# мягкие стили для плиток и кнопок
st.markdown(
    """
    <style>
      .app-note{
        background:#fff7e5;border:1px solid #ffe2a8;border-radius:12px;
        padding:.6rem 1rem;margin:.4rem 0;color:#6d5400;font-size:.9rem
      }
      .cart-box{
        background:#f8fafc;border:1px solid #e5e7eb;border-radius:16px;padding:1rem
      }
      .stButton>button{
        width:100%;border-radius:14px;border:1px solid #e5e7eb;
        padding:14px 12px;background:#ffffff;transition:.12s;
      }
      .stButton>button:hover{border-color:#cbd5e1;background:#f8fafc}
      .tile-selected .stButton>button{
        border:2px solid #6366f1;background:#eef2ff;
      }
      .price-tag{font-weight:600;color:#111827}
      .sub{color:#6b7280;font-size:.85rem}
      .muted{color:#6b7280}
      .tag{
        display:inline-block;background:#eef2ff;color:#3730a3;
        border:1px solid #c7d2fe;border-radius:999px;padding:2px 10px;
        font-size:.75rem;margin-left:.5rem
      }
      .danger{color:#b91c1c}
      .good{color:#065f46}
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------
# Helpers (Firestore)
# -----------------------------
def get_products() -> List[Dict]:
    """products: {name, category, price, active?}"""
    docs = db.collection("products").stream()
    res = []
    for d in docs:
        v = d.to_dict()
        v["id"] = d.id
        # допускаем отсутствие некоторых полей
        v.setdefault("category", "Разное")
        v.setdefault("price", 0)
        v.setdefault("active", True)
        res.append(v)
    # только активные
    return [p for p in res if p.get("active", True)]

def get_recipes() -> Dict[str, Dict[str, float]]:
    """recipes: docId == productId, fields: ingredients.{ingId: qty}"""
    res: Dict[str, Dict[str, float]] = {}
    for d in db.collection("recipes").stream():
        v = d.to_dict()
        ings = v.get("ingredients", {})
        res[d.id] = ings
    return res

def get_ingredients() -> Dict[str, Dict]:
    """ingredients -> docs: beans, milk, ... with {name, stock_quantity, unit, reorder_threshold?}"""
    res = {}
    for d in db.collection("ingredients").stream():
        res[d.id] = d.to_dict() | {"id": d.id}
    return res

def adjust_stock(transaction, ingredient_id: str, delta: float):
    ref = db.collection("ingredients").document(ingredient_id)
    snap = ref.get(transaction=transaction)
    cur = float(snap.get("stock_quantity") or 0)
    nxt = cur + delta
    if nxt < 0:
        raise ValueError("Нельзя уйти в минус по складу")
    transaction.update(ref, {"stock_quantity": nxt})

def sell_tx(items: List[Tuple[str, int]], recipes: Dict[str, Dict[str, float]]):
    """items: [(product_id, qty)]"""
    def _tx(transaction):
        # списание остатков по рецептам
        for pid, qty in items:
            ings = recipes.get(pid, {})
            for ing_id, dose in ings.items():
                adjust_stock(transaction, ing_id, -dose * qty)
        # запись продажи
        db.collection("sales").add(
            {
                "timestamp": firestore.SERVER_TIMESTAMP,
                "items": [{"pid": pid, "qty": qty} for pid, qty in items],
            }
        )
    db.transaction()(_tx)

# -----------------------------
# Session state
# -----------------------------
if "cart" not in st.session_state:
    st.session_state.cart: Dict[str, int] = {}

if "ui" not in st.session_state:
    st.session_state.ui = {"category": None, "last_clicked": None}

# -----------------------------
# Навигация
# -----------------------------
page = st.sidebar.radio(
    "Навигация",
    ["Продажи", "Склад", "Рецепты", "Поставки"],
    index=0,
)

# -----------------------------
# Страница: Продажи
# -----------------------------
if page == "Продажи":
    st.markdown('<div class="app-note">Продажа проводится только при нажатии <b>«Купить»</b>. До этого позиции лежат в корзине и остатки не меняются.</div>', unsafe_allow_html=True)

    products = get_products()
    recipes = get_recipes()

    # группировка по категориям
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for p in products:
        groups[p["category"]].append(p)

    # категория
    cats = sorted(groups.keys())
    colL, colR = st.columns([2, 1], gap="large")

    with colR:
        st.subheader("🧺 Корзина")
        if st.session_state.cart:
            total = 0.0
            for pid, qty in st.session_state.cart.items():
                prod = next((x for x in products if x["id"] == pid), None)
                if not prod:
                    continue
                line = prod["name"]
                price = float(prod.get("price") or 0)
                total += price * qty
                c1, c2, c3 = st.columns([5, 2, 2])
                with c1:
                    st.markdown(f"**{line}**  \n<span class='muted'>{price:.0f} ₽</span>", unsafe_allow_html=True)
                with c2:
                    if st.button("−", key=f"minus_{pid}"):
                        st.session_state.cart[pid] = max(0, qty - 1)
                        if st.session_state.cart[pid] == 0:
                            del st.session_state.cart[pid]
                with c3:
                    if st.button("+", key=f"plus_{pid}"):
                        st.session_state.cart[pid] = qty + 1
            st.markdown("---")
            st.markdown(f"**Итого:** <span class='price-tag'>{total:.0f} ₽</span>", unsafe_allow_html=True)
            buy = st.button("Купить ✅", type="primary", use_container_width=True)
            if buy:
                items = [(pid, q) for pid, q in st.session_state.cart.items() if q > 0]
                if not items:
                    st.warning("Корзина пуста.")
                else:
                    try:
                        sell_tx(items, recipes)
                        st.session_state.cart.clear()
                        st.success("Продажа проведена, склад списан.")
                    except Exception as e:
                        st.error(f"Ошибка при продаже: {e}")
        else:
            st.info("Корзина пуста. Добавьте напитки слева.")

    with colL:
        st.subheader("Категории")
        tag_cols = st.columns(min(4, max(1, len(cats))))
        for i, cat in enumerate(cats):
            holder = tag_cols[i % len(tag_cols)]
            with holder:
                sel = st.session_state.ui["category"]
                selected = sel == cat
                with st.container(border=True):
                    if st.button(cat, key=f"cat_{cat}"):
                        st.session_state.ui["category"] = cat

        st.markdown("---")
        cur_cat = st.session_state.ui["category"] or (cats[0] if cats else None)
        st.subheader(f"Напитки — {cur_cat or '—'}")
        cur_list = groups.get(cur_cat, [])

        # плитки-товары
        cols_in_row = 4 if len(cur_list) > 3 else max(2, len(cur_list))
        tile_cols = st.columns(cols_in_row) if cur_list else [st]
        for idx, prod in enumerate(cur_list):
            col = tile_cols[idx % len(tile_cols)]
            with col:
                # подсветка «последний клик»
                css_class = "tile-selected" if st.session_state.ui["last_clicked"] == prod["id"] else ""
                with st.container(border=True):
                    st.markdown(f"<div class='sub'>{prod.get('category','')}</div>", unsafe_allow_html=True)
                    st.markdown(f"**{prod['name']}**", unsafe_allow_html=True)
                    st.markdown(f"<div class='sub'>{float(prod.get('price') or 0):.0f} ₽</div>", unsafe_allow_html=True)
                    if st.button("Добавить", key=f"add_{prod['id']}", use_container_width=True):
                        st.session_state.cart[prod["id"]] = st.session_state.cart.get(prod["id"], 0) + 1
                        st.session_state.ui["last_clicked"] = prod["id"]

# -----------------------------
# Страница: Склад
# -----------------------------
elif page == "Склад":
    st.subheader("Склад (ингредиенты)")
    ings = get_ingredients()
    if not ings:
        st.info("Пока нет документов в коллекции `ingredients`.")
    else:
        df = pd.DataFrame(
            [
                {
                    "id": v["id"],
                    "Название": v.get("name", v["id"]),
                    "Остаток": float(v.get("stock_quantity") or 0),
                    "Ед.": v.get("unit", ""),
                    "Порог дозакупки": float(v.get("reorder_threshold") or 0),
                }
                for v in ings.values()
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

# -----------------------------
# Страница: Рецепты
# -----------------------------
elif page == "Рецепты":
    st.subheader("Рецепты")
    products = get_products()
    recipes = get_recipes()
    ings = get_ingredients()

    pid = st.selectbox(
        "Напиток",
        options=[p["id"] for p in products],
        format_func=lambda x: next((p["name"] for p in products if p["id"] == x), x),
    )
    cur = recipes.get(pid, {})
    st.write("Текущий состав (ингредиент → доза):")
    if not cur:
        st.info("Рецепт пуст. Добавь ингредиенты ниже.")
    else:
        for ing_id, dose in cur.items():
            left, mid, right = st.columns([5, 3, 2])
            with left:
                st.write(ings.get(ing_id, {}).get("name", ing_id))
            with mid:
                new_val = st.number_input(
                    f"Доза для {ing_id}",
                    value=float(dose),
                    step=1.0,
                    key=f"dose_{ing_id}",
                )
            with right:
                if st.button("Удалить", key=f"del_{ing_id}"):
                    cur.pop(ing_id, None)
                    db.collection("recipes").document(pid).set({"ingredients": cur}, merge=True)
                    st.experimental_rerun()

    st.markdown("---")
    st.write("Добавить ингредиент в рецепт:")
    add_ing = st.selectbox(
        "Ингредиент",
        options=list(ings.keys()),
        format_func=lambda x: ings.get(x, {}).get("name", x),
        key="add_ing",
    )
    add_dose = st.number_input("Доза", min_value=0.0, step=1.0, key="add_dose")
    if st.button("Добавить в рецепт"):
        new_map = dict(cur)
        new_map[add_ing] = float(add_dose)
        db.collection("recipes").document(pid).set({"ingredients": new_map}, merge=True)
        st.success("Обновлено.")
        st.experimental_rerun()

# -----------------------------
# Страница: Поставки
# -----------------------------
elif page == "Поставки":
    st.subheader("Фиксация поставок")
    ings = get_ingredients()
    if not ings:
        st.info("Нет ингредиентов.")
    else:
        ing_id = st.selectbox(
            "Ингредиент",
            options=list(ings.keys()),
            format_func=lambda x: ings.get(x, {}).get("name", x),
        )
        unit = ings.get(ing_id, {}).get("unit", "")
        qty = st.number_input(f"Количество (+{unit})", min_value=0.0, step=10.0)
        when = st.date_input("Дата поставки", datetime.today())
        if st.button("Зачесть поставку"):
            def _tx(transaction):
                adjust_stock(transaction, ing_id, float(qty))
                db.collection("deliveries").add(
                    {
                        "ingredient": ing_id,
                        "qty": float(qty),
                        "unit": unit,
                        "date": datetime(when.year, when.month, when.day),
                        "ts": firestore.SERVER_TIMESTAMP,
                    }
                )
            db.transaction()(_tx)
            st.success("Поставка внесена.")

