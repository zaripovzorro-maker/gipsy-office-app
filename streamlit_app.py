# streamlit_app.py — монолит без внешних импортов из твоего репо
# Работает на Streamlit Cloud при наличии корректных секретов.

import os, sys, json, math, time
from typing import Dict, List, Tuple
import streamlit as st

# ---- Firebase ----
from google.cloud import firestore
from google.oauth2 import service_account
from google.api_core.retry import Retry


# =========================
# Firestore init (secrets)
# =========================
@st.cache_resource
def init_firestore() -> firestore.Client:
    """
    Подключение к Firestore из Streamlit Secrets.
    Поддерживает 2 формата:
      - JSON-строка в FIREBASE_SERVICE_ACCOUNT
      - TOML-таблица [FIREBASE_SERVICE_ACCOUNT] в secrets.toml
    """
    project_id = st.secrets.get("PROJECT_ID")
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")

    if not project_id:
        st.error("❌ В secrets отсутствует PROJECT_ID.")
        st.stop()

    if not svc:
        st.error("❌ В secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    try:
        if isinstance(svc, str):
            data = json.loads(svc)
        else:
            data = dict(svc)

        # приватный ключ может быть многострочным -> ОК (google-auth сам разберёт)
        creds = service_account.Credentials.from_service_account_info(data)
        db = firestore.Client(credentials=creds, project=project_id)
        # «пробное» чтение, чтобы быстро поймать проблемы
        _ = list(db.collections())  # не дорого
        return db
    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        st.stop()


# =====================================
# Чтение каталога из Firestore (MVP)
# =====================================
def fetch_inventory(db: firestore.Client) -> Dict[str, dict]:
    inv = {}
    for doc in db.collection("inventory").stream():
        d = doc.to_dict() or {}
        inv[doc.id] = {
            "id": doc.id,
            "name": d.get("name", doc.id),
            "unit": d.get("unit", "g"),
            "capacity": float(d.get("capacity", 0)),
            "current": float(d.get("current", 0)),
            "updated_at": d.get("updated_at"),
        }
    return inv


def fetch_recipes(db: firestore.Client) -> Dict[str, dict]:
    rec = {}
    for doc in db.collection("recipes").stream():
        d = doc.to_dict() or {}
        rec[doc.id] = {
            "id": doc.id,
            "base_volume_ml": float(d.get("base_volume_ml", 200)),
            "ingredients": d.get("ingredients", []),  # [{ingredient_id, qty, unit}]
        }
    return rec


def fetch_products(db: firestore.Client) -> Dict[str, dict]:
    prods = {}
    for doc in db.collection("products").where("is_active", "==", True).stream():
        d = doc.to_dict() or {}
        prods[doc.id] = {
            "id": doc.id,
            "name": d.get("name", doc.id),
            "category": d.get("category", "Прочее"),
            "volumes": d.get("volumes", [200]),
            "base_price": int(d.get("base_price", 0)),
            "addons": d.get("addons", []),  # [{id,name,price_delta,ingredients:{}}]
            "recipe_ref": d.get("recipe_ref", None),  # 'recipes/xxx' или reference
        }
    return prods


# =====================================
# Утилиты расчёта рецептов / статусов
# =====================================
def fmt_money_kop(v: int) -> str:
    rub = v // 100
    kop = v % 100
    return f"{rub} ₽ {kop:02d}"


def inv_status(capacity: float, current: float) -> Tuple[str, float]:
    ratio = current / capacity if capacity > 0 else 0.0
    if ratio > 0.75:
        return "🔵", ratio
    if ratio > 0.50:
        return "🟡", ratio
    if ratio > 0.25:
        return "🟠", ratio
    return "🔴", ratio


def compute_base_consumption(recipe: dict, volume_ml: float) -> Dict[str, float]:
    """Возвращает потребление по ингредиентам для базового рецепта под заданный объём."""
    base_ml = float(recipe.get("base_volume_ml", 200)) or 200.0
    k = volume_ml / base_ml
    out: Dict[str, float] = {}
    for item in recipe.get("ingredients", []):
        iid = item["ingredient_id"]
        qty = float(item["qty"])
        out[iid] = out.get(iid, 0.0) + qty * k
    return out


def sum_maps(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def consumption_for_item(product: dict, volume_ml: float, addon_ids: List[str], recipes: Dict[str, dict]) -> Dict[str, float]:
    total: Dict[str, float] = {}
    # рецепт
    rkey = None
    if product.get("recipe_ref"):
        # допускаем строку вида 'recipes/xxx' или dict/ссылка
        if isinstance(product["recipe_ref"], str):
            rkey = product["recipe_ref"].split("/")[-1]
        elif isinstance(product["recipe_ref"], dict) and "path" in product["recipe_ref"]:
            rkey = str(product["recipe_ref"]["path"]).split("/")[-1]

    if rkey and rkey in recipes:
        total = sum_maps(total, compute_base_consumption(recipes[rkey], volume_ml))

    # добавки
    addons = {a["id"]: a for a in product.get("addons", [])}
    for add_id in addon_ids:
        ad = addons.get(add_id)
        if not ad:
            continue
        for iid, q in (ad.get("ingredients") or {}).items():
            total[iid] = total.get(iid, 0.0) + float(q)

    return total


def total_cart_consumption(cart: List[dict], products: Dict[str, dict], recipes: Dict[str, dict]) -> Dict[str, float]:
    need: Dict[str, float] = {}
    for item in cart:
        prod = products.get(item["product_id"])
        if not prod:
            continue
        cons = consumption_for_item(prod, item["volume_ml"], item.get("addons", []), recipes)
        for k, v in cons.items():
            need[k] = need.get(k, 0.0) + v * int(item["qty"])
    return need


def find_shortages(need: Dict[str, float], inventory: Dict[str, dict]) -> List[dict]:
    shortages = []
    for iid, req in need.items():
        have = float(inventory.get(iid, {}).get("current", 0.0))
        if have + 1e-9 < req:
            shortages.append({
                "ingredient_id": iid,
                "need": req,
                "have": have,
                "deficit": req - have
            })
    return shortages


# =====================================
# Транзакция списания + запись продажи
# =====================================
def commit_sale(db: firestore.Client, cart: List[dict], products: Dict[str, dict], recipes: Dict[str, dict]) -> Tuple[bool, str]:
    need = total_cart_consumption(cart, products, recipes)

    @firestore.transactional
    def _txn(transaction: firestore.Transaction):
        # читаем все нужные документы инвентаря
        inv_refs = {iid: db.collection("inventory").document(iid) for iid in need.keys()}
        inv_snap = {iid: inv_refs[iid].get(transaction=transaction) for iid in inv_refs}
        # проверка остатков
        for iid, req in need.items():
            cur = float(inv_snap[iid].to_dict().get("current", 0.0) if inv_snap[iid].exists else 0.0)
            if cur + 1e-9 < req:
                raise RuntimeError(f"Недостаточно '{iid}': нужно {req}, есть {cur}")

        # обновления
        for iid, req in need.items():
            cur = float(inv_snap[iid].to_dict().get("current", 0.0))
            transaction.update(inv_refs[iid], {"current": cur - req, "updated_at": firestore.SERVER_TIMESTAMP})

        # запись продажи + лог
        total_amount = sum(int(i["price_total"]) for i in cart)
        sale_ref = db.collection("sales").document()
        transaction.set(sale_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "items": cart,
            "total_amount": total_amount,
            "inventory_delta": {k: -v for k, v in need.items()},
        })
        log_ref = db.collection("inventory_log").document()
        transaction.set(log_ref, {
            "created_at": firestore.SERVER_TIMESTAMP,
            "type": "sale",
            "delta": {k: -v for k, v in need.items()},
            "sale_id": sale_ref.id
        })
        return sale_ref.id

    try:
        tid = _txn(db.transaction())
        return True, tid
    except Exception as e:
        return False, str(e)


# =====================================
# UI — платы/плитки/корзина (MVP)
# =====================================
def ensure_state():
    if "cart" not in st.session_state:
        st.session_state.cart = []  # [{product_id,name,volume_ml,qty,addons,price_total}]
    if "ui" not in st.session_state:
        st.session_state.ui = {"category": None, "product": None}


def ui_sale(db: firestore.Client):
    ensure_state()

    st.subheader("Продажи")
    st.info("Продажа проводится только при нажатии **«Купить»**. До этого позиции лежат в корзине и остатки не меняются.")

    # грузим данные
    with st.spinner("Загрузка каталога..."):
        inventory = fetch_inventory(db)
        recipes = fetch_recipes(db)
        products = fetch_products(db)

    if not products:
        st.warning("В коллекции **products** нет активных товаров. Добавь документы и обнови страницу.")
        return

    # категории
    cats = {}
    for p in products.values():
        cats.setdefault(p["category"], []).append(p)

    left, right = st.columns([7, 5], gap="large")

    # ------ Левая часть: выбор товара ------
    with left:
        st.markdown("### Категории")
        cc1, cc2, cc3, cc4 = st.columns(4)
        cols = [cc1, cc2, cc3, cc4]
        ci = 0
        for cat in sorted(cats.keys()):
            with cols[ci % 4]:
                if st.button(f"🗂️ {cat}", use_container_width=True):
                    st.session_state.ui["category"] = cat
                    st.session_state.ui["product"] = None
            ci += 1

        st.markdown("---")
        cat = st.session_state.ui.get("category") or sorted(cats.keys())[0]
        st.caption(f"Выбрана категория: **{cat}**")
        pcols = st.columns(4)
        i = 0
        for p in sorted(cats[cat], key=lambda x: x["name"]):
            with pcols[i % 4]:
                if st.button(f"☕ {p['name']}", use_container_width=True):
                    st.session_state.ui["product"] = p["id"]
            i += 1

        st.markdown("---")
        pid = st.session_state.ui.get("product")
        if pid:
            prod = products[pid]
            st.markdown(f"#### {prod['name']}")
            vol = st.segmented_control("Объём, мл", options=prod["volumes"], selection_mode="single", key="sel_volume")
            qty = st.number_input("Количество", 1, 20, 1)
            # добавки
            add_ids = []
            if prod.get("addons"):
                st.caption("Добавки:")
                for add in prod["addons"]:
                    if st.checkbox(f"{add['name']} (+{fmt_money_kop(int(add.get('price_delta',0)))})", key=f"add_{pid}_{add['id']}"):
                        add_ids.append(add["id"])

            # цена
            price = int(prod["base_price"])
            for add in prod.get("addons", []):
                if add["id"] in add_ids:
                    price += int(add.get("price_delta", 0))
            total_item = price * qty

            st.write(f"**Цена за шт.:** {fmt_money_kop(price)}  |  **Итого:** {fmt_money_kop(total_item)}")

            if st.button("➕ В корзину", type="primary"):
                st.session_state.cart.append({
                    "product_id": pid,
                    "name": prod["name"],
                    "volume_ml": float(vol),
                    "qty": int(qty),
                    "addons": add_ids,
                    "price_total": total_item
                })
                st.success("Добавлено в корзину.")

    # ------ Правая часть: корзина ------
    with right:
        st.markdown("### 🧺 Корзина")
        cart = st.session_state.cart
        if not cart:
            st.info("Корзина пуста. Добавьте напиток слева.")
        else:
            for i, it in enumerate(cart):
                st.markdown(
                    f"{i+1}. **{it['name']}** — {int(it['volume_ml'])} мл × {it['qty']}  "
                    f" | {fmt_money_kop(int(it['price_total']))}  "
                    f"{'(+' + ','.join(it['addons']) + ')' if it['addons'] else ''}"
                )
                rm = st.button("Удалить", key=f"rm_{i}", use_container_width=False)
                if rm:
                    cart.pop(i)
                    st.experimental_rerun()
            st.markdown("---")
            total = sum(int(i["price_total"]) for i in cart)
            st.write(f"**Итого к оплате:** {fmt_money_kop(total)}")

            # Предварительная проверка дефицитов
            need = total_cart_consumption(cart, products, recipes)
            shortages = find_shortages(need, inventory)
            if shortages:
                with st.expander("❗ Возможная нехватка ингредиентов (предварительно)"):
                    for s in shortages:
                        st.write(f"- {s['ingredient_id']}: нужно {s['need']:.1f}, есть {s['have']:.1f} (дефицит {s['deficit']:.1f})")
                st.warning("Покупка будет заблокирована, если нехватка подтвердится в транзакции.")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("🗑️ Очистить корзину", use_container_width=True):
                    st.session_state.cart = []
                    st.experimental_rerun()
            with c2:
                if st.button("💳 Купить", type="primary", use_container_width=True):
                    ok, msg = commit_sale(db, cart, products, recipes)
                    if ok:
                        st.success(f"Продажа проведена (sale_id={msg}).")
                        st.session_state.cart = []
                        st.balloons()
                    else:
                        st.error(f"Не удалось провести продажу: {msg}")


def ui_inventory(db: firestore.Client):
    st.subheader("Склад")
    inv = fetch_inventory(db)
    if not inv:
        st.info("Пока нет записей в `inventory`.")
        return

    rows = []
    for iid, d in inv.items():
        icon, ratio = inv_status(d["capacity"], d["current"])
        rows.append({
            "Ингредиент": d["name"],
            "Текущее": d["current"],
            "Ед.": d["unit"],
            "Макс.": d["capacity"],
            "Статус": icon,
            "Заполненность %": round(ratio * 100, 1),
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("➕ Операции пополнения/корректировки (быстрое)"):
        choice = st.selectbox("Ингредиент", options=list(inv.keys()), format_func=lambda k: inv[k]["name"])
        delta = st.number_input("Изменение (плюс к текущему)", value=0.0, step=10.0)
        if st.button("Сохранить"):
            ref = db.collection("inventory").document(choice)
            ref.update({"current": firestore.Increment(float(delta)), "updated_at": firestore.SERVER_TIMESTAMP})
            db.collection("inventory_log").add({
                "created_at": firestore.SERVER_TIMESTAMP,
                "type": "restock" if delta >= 0 else "adjust",
                "delta": {choice: float(delta)}
            })
            st.success("Обновлено.")
            st.experimental_rerun()


def ui_reports(db: firestore.Client):
    st.subheader("Рецепты • Отчёты (MVP)")
    # Топ продаж за период — простой вариант
    col1, col2 = st.columns(2)
    with col1:
        st.caption("Последние 30 продаж:")
        sales = list(db.collection("sales").order_by("created_at", direction=firestore.Query.DESCENDING).limit(30).stream())
        if not sales:
            st.info("Пока нет продаж.")
        else:
            for s in sales:
                d = s.to_dict()
                st.write(f"- **{fmt_money_kop(int(d.get('total_amount',0)))}**, позиций: {len(d.get('items',[]))}")

    with col2:
        st.caption("Ингредиенты на исходе (🟠/🔴):")
        inv = fetch_inventory(db)
        danger = []
        for x in inv.values():
            icon, ratio = inv_status(x["capacity"], x["current"])
            if icon in ("🟠", "🔴"):
                danger.append(f"{icon} {x['name']} — {x['current']}/{x['capacity']} {x['unit']}")
        if not danger:
            st.success("Критичных остатков нет.")
        else:
            for line in danger:
                st.write("• " + line)


# =========================
# Основной запуск
# =========================
def main():
    st.set_page_config(page_title="Gipsy Office — учёт", layout="wide", initial_sidebar_state="expanded")
    st.title("gipsy office — учёт")

    db = init_firestore()

    # Навигация
    st.sidebar.header("Навигация")
    page = st.sidebar.radio("", ["Продажи", "Склад", "Рецепты • Отчёты"], index=0)

    with st.sidebar.expander("🔍 Secrets check"):
        svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
        st.write("PROJECT_ID:", bool(st.secrets.get("PROJECT_ID")))
        st.write("FIREBASE_SERVICE_ACCOUNT type:", type(svc).__name__)
        if isinstance(svc, str):
            st.write("contains \\n literal:", "\\n" in svc)
        if isinstance(svc, dict) and "private_key" in svc:
            pk = svc["private_key"]
            st.write("pk starts with BEGIN:", str(pk).strip().startswith("-----BEGIN"))

    st.divider()
    if page == "Продажи":
        ui_sale(db)
    elif page == "Склад":
        ui_inventory(db)
    else:
        ui_reports(db)


if __name__ == "__main__":
    main()
