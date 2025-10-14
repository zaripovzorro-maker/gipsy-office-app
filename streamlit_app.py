from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Any

import streamlit as st
from google.cloud import firestore
import firebase_admin
from firebase_admin import credentials


# ============== Firestore init (секреты) =================

def _read_firebase_service_account() -> Dict:
    """
    Поддерживает два формата Secrets:
      A) TOML-таблица:
         [FIREBASE_SERVICE_ACCOUNT]
         type="service_account"
         ...
         private_key = """-----BEGIN... (с реальными переводами строк) ... END-----"""
      B) JSON-строка:
         FIREBASE_SERVICE_ACCOUNT = "{\"type\":\"service_account\",...,\"private_key\":\"-----BEGIN...\\n...\\nEND-----\\n\"}"
    """
    if "FIREBASE_SERVICE_ACCOUNT" not in st.secrets:
        raise RuntimeError("В Secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")

    svc = st.secrets["FIREBASE_SERVICE_ACCOUNT"]

    # Вариант B: JSON-строка
    if isinstance(svc, str):
        try:
            data = json.loads(svc)
        except Exception:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой (валидный JSON) или таблицей TOML.")
        # если в ключе литералы \n — превращаем в реальные переводы строк
        pk = data.get("private_key", "")
        if "\\n" in pk and "\n" not in pk:
            data["private_key"] = pk.replace("\\n", "\n")
        return data

    # Вариант A: таблица TOML (dict)
    if isinstance(svc, dict):
        data = dict(svc)
        pk = data.get("private_key", "")
        # Для TOML всё должно уже быть многострочно — НИЧЕГО не меняем.
        # Лёгкая валидация шапки:
        if not str(pk).startswith("-----BEGIN PRIVATE KEY-----"):
            raise RuntimeError("private_key (TOML) должен начинаться с -----BEGIN PRIVATE KEY-----")
        return data

    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML.")


@st.cache_resource(show_spinner=False)
def init_firestore() -> firestore.Client:
    project_id = st.secrets.get("PROJECT_ID", "")
    if not project_id:
        raise RuntimeError("В Secrets нет PROJECT_ID.")

    data = _read_firebase_service_account()

    # Инициализация firebase_admin ровно 1 раз
    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, {"projectId": project_id})

    return firestore.Client(project=project_id)


# ============== вспомогалки для UI & данных =================

def badge(text: str, color: str = "gray"):
    st.markdown(
        f"<span style='background:{color};color:white;padding:2px 8px;border-radius:8px;font-size:12px'>"
        f"{text}</span>",
        unsafe_allow_html=True,
    )


def ensure_state():
    if "cart" not in st.session_state:
        st.session_state.cart: List[Dict[str, Any]] = []
    if "active_category" not in st.session_state:
        st.session_state.active_category = None
    if "active_product" not in st.session_state:
        st.session_state.active_product = None


# --------- выборки из Firestore ---------

def get_products(db: firestore.Client) -> List[Dict[str, Any]]:
    docs = db.collection("products").stream()
    res = []
    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        # ожидается структура:
        # {
        #   "category": "Кофе",
        #   "name": "Капучино",
        #   "sizes": [{"name":"S","label":"250 мл","price":150,"mult":1.0}, ...],
        #   "recipe": {"beans": 16, "milk": 150}  # базовая доза
        # }
        res.append(item)
    return res


def get_ingredients(db: firestore.Client) -> List[Dict[str, Any]]:
    docs = db.collection("ingredients").stream()
    res = []
    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        # ожидается структура:
        # { "name":"Зёрна", "unit":"g", "stock_quantity": 1200, "reorder_threshold": 200 }
        res.append(item)
    return res


# --------- продажи ---------

def ui_sales(db: firestore.Client):
    ensure_state()
    st.title("gipsy office — продажи")
    st.info("Продажа проводится только при нажатии «Купить». До этого позиции лежат в корзине и остатки не меняются.")

    products = get_products(db)
    if not products:
        st.warning("В коллекции `products` пусто. Добавьте напитки (category, name, sizes[], recipe{}) в Firestore.")
        return

    # Корзина справа
    cart_col = st.sidebar if st.session_state.get("cart_on_sidebar", False) else None
    right = st.container()
    left, right = st.columns([2, 1])

    # Категории
    categories = sorted({p.get("category", "Без категории") for p in products})
    with left:
        st.subheader("Категории")
        cat_cols = st.columns(min(len(categories), 4) or 1)
        for i, cat in enumerate(categories):
            with cat_cols[i % len(cat_cols)]:
                is_active = (st.session_state.active_category == cat)
                btn = st.button(
                    f"🍱 {cat}",
                    use_container_width=True,
                    type="primary" if is_active else "secondary",
                    key=f"cat_{cat}"
                )
                if btn:
                    st.session_state.active_category = cat
                    st.session_state.active_product = None

        st.markdown("---")

        if st.session_state.active_category:
            st.subheader(f"Напитки — {st.session_state.active_category}")
            prods = [p for p in products if p.get("category") == st.session_state.active_category]
            if not prods:
                st.write("В этой категории пока ничего нет.")
            else:
                grid = st.columns(3)
                for i, p in enumerate(prods):
                    with grid[i % 3]:
                        is_selected = (st.session_state.active_product == p.get("id"))
                        st.markdown(
                            f"""
                            <div style="
                                border:2px solid {'#3b82f6' if is_selected else '#e5e7eb'};
                                border-radius:14px;padding:12px;margin-bottom:12px;">
                                <div style="font-weight:600">{p.get('name','Без имени')}</div>
                                <div style="font-size:12px;color:#6b7280">{p.get('category','')}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                        if st.button("Выбрать", key=f"pick_{p['id']}", use_container_width=True):
                            st.session_state.active_product = p["id"]

                # Объёмы/размеры
                st.markdown("---")
                if st.session_state.active_product:
                    prod = next((x for x in products if x["id"] == st.session_state.active_product), None)
                    if prod:
                        st.subheader(f"{prod.get('name')}: объём/цена")
                        sizes: List[Dict[str, Any]] = prod.get("sizes", [])
                        scols = st.columns(min(len(sizes), 4) or 1)
                        for i, s in enumerate(sizes):
                            with scols[i % len(scols)]:
                                label = s.get("label", s.get("name", ""))
                                price = s.get("price", 0)
                                pressed = st.button(f"{label}\n{price} ₽",
                                                    key=f"size_{prod['id']}_{i}",
                                                    use_container_width=True)
                                if pressed:
                                    st.session_state.cart.append({
                                        "product_id": prod["id"],
                                        "product_name": prod.get("name", ""),
                                        "size_name": s.get("name", ""),
                                        "size_label": label,
                                        "price": price,
                                        "mult": float(s.get("mult", 1.0)),
                                    })
                                    st.success(f"Добавлено в корзину: {prod.get('name')} ({label})")

    # Корзина справа
    with right:
        st.subheader("🧺 Корзина")
        cart = st.session_state.cart
        if not cart:
            st.info("Корзина пуста. Добавьте напитки слева.")
        else:
            total = 0
            for idx, it in enumerate(cart):
                c1, c2, c3 = st.columns([4, 2, 1])
                with c1:
                    st.write(f"**{it['product_name']}** — {it['size_label']}")
                with c2:
                    st.write(f"{it['price']} ₽")
                with c3:
                    if st.button("✖", key=f"rm_{idx}"):
                        cart.pop(idx)
                        st.experimental_rerun()
                total += it["price"]

            st.markdown("---")
            st.write(f"**Итого:** {total} ₽")

            if st.button("Купить", type="primary", use_container_width=True):
                try:
                    _commit_sale(db, cart, products)
                except Exception as e:
                    st.error(f"Не удалось провести продажу: {e}")
                else:
                    st.success("Продажа проведена!")
                    st.session_state.cart = []
                    st.experimental_rerun()


# списание по рецептам
def _commit_sale(db: firestore.Client, cart: List[Dict[str, Any]], products: List[Dict[str, Any]]):
    if not cart:
        return
    # готовим агрегат списаний по ингредиентам
    to_deduct: Dict[str, float] = {}
    items = []

    for it in cart:
        prod = next((p for p in products if p["id"] == it["product_id"]), None)
        if not prod:
            continue
        mult = float(it.get("mult", 1.0))
        recipe: Dict[str, float] = prod.get("recipe", {})
        for ing_id, base_amount in recipe.items():
            to_deduct[ing_id] = to_deduct.get(ing_id, 0.0) + float(base_amount) * mult

        items.append({
            "product_id": it["product_id"],
            "name": it["product_name"],
            "size": it["size_label"],
            "price": it["price"],
        })

    # транзакция: списать и записать продажу
    @firestore.transactional
    def tx_op(tx: firestore.Transaction):
        # списания
        for ing_id, delta in to_deduct.items():
            ref = db.collection("ingredients").document(ing_id)
            snap = ref.get(transaction=tx)
            if not snap.exists:
                raise RuntimeError(f"Ингредиент {ing_id} не найден.")
            stock = float(snap.to_dict().get("stock_quantity", 0))
            new_stock = stock - delta
            if new_stock < -1e-6:
                raise RuntimeError(f"Нельзя уйти в минус по {ing_id} (надо {delta}, остаток {stock}).")
            tx.update(ref, {"stock_quantity": new_stock})

        # запись продажи
        sale_ref = db.collection("sales").document()
        tx.set(sale_ref, {
            "created_at": datetime.utcnow(),
            "items": items,
            "total": sum(i["price"] for i in items),
        })

    tx = db.transaction()
    tx_op(tx)


# --------- склад ---------

def _status_color(pct: float) -> str:
    if pct >= 0.75:
        return "#10b981"  # green
    if pct >= 0.5:
        return "#60a5fa"  # blue
    if pct >= 0.25:
        return "#f59e0b"  # amber
    return "#ef4444"      # red


def ui_stock(db: firestore.Client):
    st.title("Склад")
    ing = get_ingredients(db)
    if not ing:
        st.info("Ингредиенты не найдены. Добавьте документы в коллекцию `ingredients`.")
        return

    DEFAULT_CAPACITY = {i["id"]: max(float(i.get("stock_quantity", 0)), float(i.get("reorder_threshold", 0)) * 4 or 1)
                        for i in ing}

    for item in ing:
        cap = DEFAULT_CAPACITY.get(item["id"], 1.0)
        stock = float(item.get("stock_quantity", 0))
        pct = stock / cap if cap > 0 else 0
        color = _status_color(pct)

        with st.container():
            st.markdown(
                f"<div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin-bottom:10px'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div><b>{item.get('name','')}</b> "
                f"<span style='color:#6b7280'>(норма {int(cap)} {item.get('unit','')})</span></div>"
                f"<div style='color:{color}'><b>{int(pct*100)}%</b></div>"
                f"</div></div>", unsafe_allow_html=True
            )

            c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 2, 2])
            if c1.button("+", key=f"plus_{item['id']}"):
                _adj_stock(db, item["id"], +50)
            if c2.button("-", key=f"minus_{item['id']}"):
                _adj_stock(db, item["id"], -50)
            delta = c3.number_input("±", key=f"delta_{item['id']}", step=10, value=0)
            if c4.button("Применить", key=f"apply_{item['id']}"):
                _adj_stock(db, item["id"], float(delta))
            c5.write(f"Остаток: **{int(stock)} {item.get('unit','')}**")


def _adj_stock(db: firestore.Client, ing_id: str, delta: float):
    @firestore.transactional
    def tx_op(tx: firestore.Transaction):
        ref = db.collection("ingredients").document(ing_id)
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise RuntimeError("Ингредиент не найден.")
        stock = float(snap.to_dict().get("stock_quantity", 0))
        new_stock = stock + delta
        if new_stock < -1e-6:
            raise RuntimeError("Нельзя уйти в минус.")
        tx.update(ref, {"stock_quantity": new_stock})

    tx = db.transaction()
    tx_op(tx)


# --------- рецепты ---------

def ui_recipes(db: firestore.Client):
    st.title("Рецепты")
    prods = get_products(db)
    ing = get_ingredients(db)
    ing_map = {i["id"]: i for i in ing}

    if not prods:
        st.info("Пусто в `products`.")
        return

    for p in prods:
        with st.expander(f"{p.get('category','?')} • {p.get('name','?')}", expanded=False):
            recipe: Dict[str, float] = dict(p.get("recipe", {}))
            st.write("**Базовая доза (для размера с mult=1.0):**")
            # показ рецепта
            for ing_id, amount in recipe.items():
                row = st.columns([3, 2, 1])
                with row[0]:
                    st.write(ing_map.get(ing_id, {}).get("name", ing_id))
                with row[1]:
                    new_amount = st.number_input("г/мл", value=float(amount), key=f"rcp_{p['id']}_{ing_id}")
                    recipe[ing_id] = new_amount
                with row[2]:
                    if st.button("Удалить", key=f"rcp_del_{p['id']}_{ing_id}"):
                        recipe.pop(ing_id, None)
                        _save_product_recipe(db, p["id"], recipe)
                        st.experimental_rerun()

            # добавление строки
            st.markdown("---")
            c1, c2, c3 = st.columns([3, 2, 1])
            with c1:
                add_ing = st.selectbox("Ингредиент", options=["—"] + [i["id"] for i in ing],
                                       format_func=lambda x: "—" if x == "—" else ing_map[x]["name"],
                                       key=f"add_ing_{p['id']}")
            with c2:
                add_amt = st.number_input("Доза", min_value=0.0, value=0.0, step=1.0, key=f"add_amt_{p['id']}")
            with c3:
                if st.button("Добавить", key=f"add_btn_{p['id']}") and add_ing != "—" and add_amt > 0:
                    recipe[add_ing] = add_amt
                    _save_product_recipe(db, p["id"], recipe)
                    st.success("Сохранено.")
                    st.experimental_rerun()

            if st.button("💾 Сохранить все изменения", key=f"save_{p['id']}"):
                _save_product_recipe(db, p["id"], recipe)
                st.success("Сохранено.")


def _save_product_recipe(db: firestore.Client, prod_id: str, recipe: Dict[str, float]):
    db.collection("products").document(prod_id).update({"recipe": recipe})


# --------- поставки ---------

def ui_deliveries(db: firestore.Client):
    st.title("Поставки (приход на склад)")
    ing = get_ingredients(db)
    if not ing:
        st.info("Нет ингредиентов.")
        return
    ing_map = {i["name"]: i for i in ing}
    names = sorted(ing_map.keys())

    with st.form("delivery_form", clear_on_submit=True):
        name = st.selectbox("Ингредиент", names)
        qty = st.number_input("Количество (в единицах ингредиента)", min_value=0.0, step=10.0)
        dt = st.date_input("Дата поставки", value=datetime.utcnow().date())
        submitted = st.form_submit_button("Добавить приход")
        if submitted and qty > 0:
            item = ing_map[name]
            _adj_stock(db, item["id"], float(qty))
            db.collection("deliveries").add({
                "ingredient_id": item["id"],
                "name": item["name"],
                "qty": float(qty),
                "at": datetime(dt.year, dt.month, dt.day),
            })
            st.success("Поставка зафиксирована.")

    # последние поставки
    st.markdown("---")
    st.subheader("Последние поставки")
    docs = db.collection("deliveries").order_by("at", direction=firestore.Query.DESCENDING).limit(20).stream()
    rows = []
    for d in docs:
        r = d.to_dict()
        rows.append([r.get("name"), r.get("qty"), r.get("at")])
    if rows:
        st.table(rows)
    else:
        st.write("Пока пусто.")


# ===================== main =====================

def secrets_check():
    st.sidebar.markdown("### ✅ Secrets check")
    ok = True
    def row(label, cond):
        nonlocal ok
        ok = ok and cond
        st.sidebar.write(f"• {label}: {'🟩 True' if cond else '🟥 False'}")

    row("PROJECT_ID present", "PROJECT_ID" in st.secrets)
    has = "FIREBASE_SERVICE_ACCOUNT" in st.secrets
    row("FIREBASE_SERVICE_ACCOUNT type: str", has and isinstance(st.secrets["FIREBASE_SERVICE_ACCOUNT"], str))
    # если таблица — тоже ок
    if has and isinstance(st.secrets["FIREBASE_SERVICE_ACCOUNT"], dict):
        st.sidebar.write("• FIREBASE_SERVICE_ACCOUNT type: dict (ok)")
    # лёгкая диагностика приватного ключа
    try:
        svc = st.secrets["FIREBASE_SERVICE_ACCOUNT"]
        if isinstance(svc, str):
            data = json.loads(svc)
        else:
            data = dict(svc)
        pk = data.get("private_key", "")
        st.sidebar.write(f"• private_key length:  {len(pk)}")
        st.sidebar.write(f"• starts with BEGIN:   {pk.startswith('-----BEGIN PRIVATE KEY-----')}")
        st.sidebar.write(f"• contains \\n literal: {'\\n' in pk}")
    except Exception:
        st.sidebar.write("• private_key: (не разобран)")

    st.sidebar.markdown("---")
    st.sidebar.markdown("### Навигация")
    st.sidebar.write("• Продажи • Склад • Рецепты • Поставки (см. верхние вкладки, если реализуете позже)")


def main():
    st.set_page_config(page_title="gipsy office — учёт", page_icon="☕", layout="wide")
    secrets_check()

    # Firestore
    try:
        db = init_firestore()
    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        return

    tabs = st.tabs(["Продажи", "Склад", "Рецепты", "Поставки"])

    with tabs[0]:
        ui_sales(db)
    with tabs[1]:
        ui_stock(db)
    with tabs[2]:
        ui_recipes(db)
    with tabs[3]:
        ui_deliveries(db)


if __name__ == "__main__":
    main()
