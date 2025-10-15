from typing import Dict, List
import streamlit as st
from google.cloud import firestore

from app.services.inventory import fetch_inventory
from app.services.products import fetch_products, fetch_recipes
from app.logic.calc import total_cart_consumption, find_shortages
from app.services.sales import commit_sale
from app.utils.format import fmt_money_kop


def _ensure_state():
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "ui" not in st.session_state:
        st.session_state.ui = {"category": None, "product": None}


def _build_categories(products: Dict[str, dict]) -> Dict[str, List[dict]]:
    cats = {}
    for p in products.values():
        cats.setdefault(p["category"], []).append(p)
    return cats


def render_sale(db: firestore.Client):
    _ensure_state()

    st.subheader("Продажи")
    st.info("Продажа проводится только при нажатии **«Купить»**. До этого позиции лежат в корзине и остатки не меняются.")

    with st.spinner("Загрузка каталога..."):
        inventory = fetch_inventory(db)
        recipes = fetch_recipes(db)
        products = fetch_products(db)

    if not products:
        st.warning("В коллекции **products** нет активных товаров.")
        return

    cats = _build_categories(products)
    left, right = st.columns([7, 5], gap="large")

    # -------- ЛЕВО --------
    with left:
        st.markdown("### Категории")
        cat_row = st.columns(4)
        i = 0
        for cat in sorted(cats.keys()):
            with cat_row[i % 4]:
                if st.button(f"🗂️ {cat}", use_container_width=True):
                    st.session_state.ui["category"] = cat
                    st.session_state.ui["product"] = None
            i += 1

        st.markdown("---")
        cat = st.session_state.ui.get("category") or sorted(cats.keys())[0]
        st.caption(f"Выбрана категория: **{cat}**")

        prod_row = st.columns(4)
        i = 0
        for p in sorted(cats[cat], key=lambda x: x["name"]):
            with prod_row[i % 4]:
                if st.button(f"☕ {p['name']}", use_container_width=True):
                    st.session_state.ui["product"] = p["id"]
            i += 1

        st.markdown("---")
        pid = st.session_state.ui.get("product")
        if pid:
            prod = products[pid]
            st.markdown(f"#### {prod['name']}")
            vol = st.radio("Объём, мл", prod["volumes"], horizontal=True, index=0, key=f"vol_{pid}")
            qty = st.number_input("Количество", 1, 20, 1, key=f"qty_{pid}")

            add_ids = []
            if prod.get("addons"):
                st.caption("Добавки:")
                for add in prod["addons"]:
                    if st.checkbox(
                        f"{add['name']} (+{fmt_money_kop(int(add.get('price_delta',0)))})",
                        key=f"add_{pid}_{add['id']}",
                    ):
                        add_ids.append(add["id"])

            price = int(prod["base_price"])
            for add in prod.get("addons", []):
                if add["id"] in add_ids:
                    price += int(add.get("price_delta", 0))
            total_item = price * int(qty)

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

    # -------- ПРАВО --------
    with right:
        st.markdown("### 🧺 Корзина")
        cart = st.session_state.cart
        if not cart:
            st.info("Корзина пуста. Добавьте напиток слева.")
        else:
            for i, it in enumerate(cart):
                st.markdown(
                    f"{i+1}. **{it['name']}** — {int(it['volume_ml'])} мл × {it['qty']} | {fmt_money_kop(int(it['price_total']))}"
                )
                if st.button("Удалить", key=f"rm_{i}"):
                    cart.pop(i)
                    st.experimental_rerun()

            st.markdown("---")
            total = sum(int(i["price_total"]) for i in cart)
            st.write(f"**Итого к оплате:** {fmt_money_kop(total)}")

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
