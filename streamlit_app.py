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
    project_id = st.secrets.get("PROJECT_ID", "").strip()
    if not project_id:
        st.error("В Secrets нет PROJECT_ID.")
        st.stop()

    svc_raw = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)
    if svc_raw is None:
        st.error("В Secrets нет FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    if not firebase_admin._apps:
        if isinstance(svc_raw, str):
            try:
                data = json.loads(svc_raw)
            except Exception:
                st.error(
                    "Ключ в Secrets задан как строка, но это не валидный JSON. "
                    "Убедись, что в `private_key` используются `\\n`, а не настоящие переводы строк."
                )
                st.stop()
        else:
            data = dict(svc_raw)

        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, {"projectId": project_id})

    return firestore.Client(project=project_id)


db: firestore.Client = init_firestore()


# -----------------------------
# UI общие настройки + мягкая тема
# -----------------------------
st.set_page_config(
    page_title="gipsy office — учёт",
    page_icon="☕",
    layout="wide",
)

st.markdown(
    """
    <style>
      /* базовые цвета */
      :root {
        --card-bg: #ffffff;
        --card-br: #e5e7eb;
        --card-hv: #f8fafc;
        --pill-bg:#eef2ff; --pill-br:#c7d2fe; --pill-tx:#3730a3;
        --accent:#6366f1; --accent-weak:#eef2ff; --muted:#6b7280; --ink:#111827;
        --ok:#065f46; --warn:#b45309; --bad:#b91c1c;
      }

      .note{
        background:#fff7e5;border:1px solid #ffe2a8;border-radius:12px;
        padding:.6rem 1rem;color:#6d5400;font-size:.9rem;margin:.4rem 0 1rem;
      }

      .muted{color:var(--muted)}
      .price{color:var(--ink);font-weight:700}

      .pill{
        display:inline-block;background:var(--pill-bg);color:var(--pill-tx);
        border:1px solid var(--pill-br);border-radius:999px;
        padding:4px 12px;font-size:.8rem
      }

      .tile, .tile-sel{
        background:var(--card-bg);border:1px solid var(--card-br);
        border-radius:16px;padding:14px; transition:.12s;
      }
      .tile:hover{background:var(--card-hv)}
      .tile-sel{ border:2px solid var(--accent); background:var(--accent-weak) }

      .stButton>button{
        width:100%;border-radius:12px;border:1px solid var(--card-br);
        padding:10px 12px;background:var(--card-bg)
      }
      .stButton>button:hover{border-color:#cbd5e1;background:var(--card-hv)}

      .cart{
        background:#f8fafc;border:1px solid var(--card-br);border-radius:16px;padding:1rem
      }
      .good{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# Firestore helpers
# -----------------------------
def get_products() -> List[Dict]:
    docs = db.collection("products").stream()
    res = []
    for d in docs:
        v = d.to_dict()
        v["id"] = d.id
        v.setdefault("category", "Разное")
        v.setdefault("price", 0)
        v.setdefault("active", True)
        if v["active"]:
            res.append(v)
    return res


def get_recipes() -> Dict[str, Dict[str, float]]:
    res: Dict[str, Dict[str, float]] = {}
    for d in db.collection("recipes").stream():
        v = d.to_dict()
        res[d.id] = v.get("ingredients", {})
    return res


def get_ingredients() -> Dict[str, Dict]:
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
    def _tx(transaction):
        for pid, qty in items:
            ings = recipes.get(pid, {})
            for ing_id, dose in ings.items():
                adjust_stock(transaction, ing_id, -dose * qty)
        db.collection("sales").add(
            {"timestamp": firestore.SERVER_TIMESTAMP, "items": [{"pid": p, "qty": q} for p, q in items]}
        )

    db.transaction()(_tx)


# -----------------------------
# Session
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

# =============================
# Продажи
# =============================
if page == "Продажи":
    st.title("gipsy office — продажи")
    st.markdown('<div class="note">Продажа проводится только при нажатии <b>«Купить»</b>. До этого позиции лежат в корзине и остатки не меняются.</div>', unsafe_allow_html=True)

    products = get_products()
    recipes = get_recipes()

    # Категории
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for p in products:
        groups[p["category"]].append(p)
    cats = sorted(groups.keys())

    left, right = st.columns([2, 1], gap="large")

    # Корзина
    with right:
        st.subheader("🧺 Корзина")
        st.markdown('<div class="cart">', unsafe_allow_html=True)

        if st.session_state.cart:
            total = 0.0
            for pid, qty in st.session_state.cart.items():
                prod = next((x for x in products if x["id"] == pid), None)
                if not prod:
                    continue
                price = float(prod.get("price") or 0)
                total += price * qty
                c1, c2, c3 = st.columns([5, 2, 2])
                with c1:
                    st.markdown(f"**{prod['name']}**  \n<span class='muted'>{price:.0f} ₽</span>", unsafe_allow_html=True)
                with c2:
                    if st.button("−", key=f"minus_{pid}"):
                        st.session_state.cart[pid] = max(0, qty - 1)
                        if st.session_state.cart[pid] == 0:
                            del st.session_state.cart[pid]
                with c3:
                    if st.button("+", key=f"plus_{pid}"):
                        st.session_state.cart[pid] = qty + 1

            st.markdown("---")
            st.markdown(f"**Итого:** <span class='price'>{total:.0f} ₽</span>", unsafe_allow_html=True)
            if st.button("Купить ✅", type="primary", use_container_width=True):
                items = [(pid, q) for pid, q in st.session_state.cart.items() if q > 0]
                if not items:
                    st.warning("Корзина пуста.")
                else:
                    try:
                        sell_tx(items, recipes)
                        st.session_state.cart.clear()
                        st.success("Продажа проведена, склад списан.")
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
        else:
            st.info("Корзина пуста. Добавьте напитки слева.")

        st.markdown("</div>", unsafe_allow_html=True)

    # Категории + плитки напитков
    with left:
        st.subheader("Категории")
        tag_cols = st.columns(min(6, max(1, len(cats)))) if cats else [st]
        for i, cat in enumerate(cats):
            with tag_cols[i % len(tag_cols)]:
                selected = st.session_state.ui["category"] == cat
                cls = "pill" if not selected else "pill"
                if st.button(f"{cat}", key=f"cat_{cat}"):
                    st.session_state.ui["category"] = cat

        st.markdown("---")
        cur_cat = st.session_state.ui["category"] or (cats[0] if cats else None)
        st.subheader(f"Напитки — {cur_cat or '—'}")
        cur_list = groups.get(cur_cat, [])

        if not cur_list:
            st.info("В этой категории пока нет активных позиций.")
        else:
            # плитки в 4 колонки
            cols = st.columns(4)
            for i, prod in enumerate(cur_list):
                col = cols[i % 4]
                with col:
                    sel = st.session_state.ui["last_clicked"] == prod["id"]
                    st.markdown(f"<div class='{'tile-sel' if sel else 'tile'}'>", unsafe_allow_html=True)
                    st.markdown(f"<div class='muted'>{prod.get('category','')}</div>", unsafe_allow_html=True)
                    st.markdown(f"**{prod['name']}**", unsafe_allow_html=True)
                    st.markdown(f"<div class='muted'>{float(prod.get('price') or 0):.0f} ₽</div>", unsafe_allow_html=True)
                    if st.button("Добавить", key=f"add_{prod['id']}", use_container_width=True):
                        st.session_state.cart[prod["id"]] = st.session_state.cart.get(prod["id"], 0) + 1
                        st.session_state.ui["last_clicked"] = prod["id"]
                    st.markdown("</div>", unsafe_allow_html=True)

# =============================
# Склад
# =============================
elif page == "Склад":
    st.title("Склад")
    ings = get_ingredients()
    if not ings:
        st.info("Пока нет документов в коллекции `ingredients`.")
    else:
        df = pd.DataFrame(
            [
                {
                    "ID": v["id"],
                    "Название": v.get("name", v["id"]),
                    "Остаток": float(v.get("stock_quantity") or 0),
                    "Ед.": v.get("unit", ""),
                    "Порог дозакупки": float(v.get("reorder_threshold") or 0),
                }
                for v in ings.values()
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

# =============================
# Рецепты
# =============================
elif page == "Рецепты":
    st.title("Рецепты")
    products = get_products()
    recipes = get_recipes()
    ings = get_ingredients()

    if not products:
        st.info("Сначала добавь продукты в коллекцию `products`.")
    else:
        pid = st.selectbox(
            "Напиток",
            options=[p["id"] for p in products],
            format_func=lambda x: next((p["name"] for p in products if p["id"] == x), x),
        )

        cur = dict(recipes.get(pid, {}))
        st.write("Состав / дозы:")
        if not cur:
            st.info("Рецепт пуст. Добавь ингредиенты ниже.")
        else:
            for ing_id, dose in list(cur.items()):
                c1, c2, c3 = st.columns([5, 3, 2])
                with c1:
                    st.write(ings.get(ing_id, {}).get("name", ing_id))
                with c2:
                    new_dose = st.number_input("Доза", value=float(dose), key=f"dose_{ing_id}", step=1.0)
                with c3:
                    if st.button("Удалить", key=f"del_{ing_id}"):
                        cur.pop(ing_id, None)
                        db.collection("recipes").document(pid).set({"ingredients": cur}, merge=True)
                        st.experimental_rerun()
                # если поменяли дозу — сохраняем
                if new_dose != dose:
                    cur[ing_id] = float(new_dose)
                    db.collection("recipes").document(pid).set({"ingredients": cur}, merge=True)

        st.markdown("---")
        st.write("Добавить ингредиент:")
        add_ing = st.selectbox(
            "Ингредиент",
            options=list(ings.keys()),
            format_func=lambda x: ings.get(x, {}).get("name", x),
            key="add_ing",
        )
        add_dose = st.number_input("Доза", min_value=0.0, step=1.0, key="add_dose")
        if st.button("Добавить в рецепт"):
            cur = dict(recipes.get(pid, {}))
            cur[add_ing] = float(add_dose)
            db.collection("recipes").document(pid).set({"ingredients": cur}, merge=True)
            st.success("Обновлено.")
            st.experimental_rerun()

# =============================
# Поставки (светлая форма)
# =============================
elif page == "Поставки":
    st.title("Поставки (ингредиенты)")
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

        if st.button("Зачесть поставку ✅", type="primary"):
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
            st.success("Поставка зафиксирована.")
