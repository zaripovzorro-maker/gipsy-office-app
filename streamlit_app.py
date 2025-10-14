# streamlit_app.py
# gipsy-office — учёт списаний / склад / рецепты
# Работает на Streamlit Cloud, Firestore (Firebase), Python 3.11–3.13

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import streamlit as st

# Firebase Admin / Firestore
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore


# =========================
#  ИНИЦИАЛИЗАЦИЯ FIRESTORE
# =========================

@st.cache_resource(show_spinner=False)
def init_firestore() -> firestore.Client:
    # PROJECT_ID обязателен
    project_id = st.secrets.get("PROJECT_ID")
    if not project_id:
        st.stop()  # прерываем рендер и просим настроить
        raise RuntimeError("Нет PROJECT_ID в Secrets")

    # Берём сервисный ключ из Secrets
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not svc:
        st.error("В Secrets нет FIREBASE_SERVICE_ACCOUNT. Открой меню: ⋮ → Edit secrets и вставь ключ.")
        st.stop()

    # Разрешаем 2 формата: JSON-строка или TOML-таблица
    if isinstance(svc, str):
        try:
            data = json.loads(svc)
        except Exception as e:
            st.error(f"FIREBASE_SERVICE_ACCOUNT задан строкой, но это не валидный JSON: {e}")
            st.stop()
    elif isinstance(svc, dict):
        data = dict(svc)
    else:
        st.error("FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML (мэп).")
        st.stop()

    # Инициализируем firebase_admin ровно один раз
    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, options={"projectId": project_id})

    return firestore.Client(project=project_id)


db = init_firestore()


# ================
#  УТИЛИТЫ / UI
# ================

def format_recipe_line(item: Dict[str, Any], ing_map: Dict[str, Dict[str, Any]]) -> str:
    """Красиво печатаем строчку рецепта."""
    ing = ing_map.get(item["ingredientId"])
    if not ing:
        return f"• неизвестный ингредиент ({item['ingredientId']})"
    qty = item.get("qtyPer", 0)
    unit = ing.get("unit", "")
    name = ing.get("name", "ингредиент")
    return f"• {name}: {qty:g} {unit}".strip()


def percent(stock: float, capacity: float) -> float:
    if not capacity:
        return 0.0
    return max(0.0, min(100.0, 100.0 * stock / capacity))


# ================
#  ДАННЫЕ / CRUD
# ================

def get_ingredients_map() -> Dict[str, Dict[str, Any]]:
    """Словарь ингредиентов {id: doc}."""
    docs = db.collection("ingredients").stream()
    result = {}
    for d in docs:
        v = d.to_dict()
        v["id"] = d.id
        # поля по умолчанию
        v.setdefault("name", d.id)
        v.setdefault("unit", "")
        v.setdefault("capacity", 0.0)
        v.setdefault("stock_quantity", 0.0)
        v.setdefault("reorder_threshold", 0.0)
        result[d.id] = v
    return result


def get_products() -> List[Dict[str, Any]]:
    docs = db.collection("products").order_by("name").stream()
    items = []
    for d in docs:
        v = d.to_dict()
        v["id"] = d.id
        v.setdefault("name", d.id)
        v.setdefault("price", 0)
        items.append(v)
    return items


def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    """Документ recipes/{product_id} с полем items (array)."""
    snap = db.collection("recipes").document(product_id).get()
    if not snap.exists:
        return []
    data = snap.to_dict()
    return data.get("items", []) or []


def save_recipe(product_id: str, items: List[Dict[str, Any]]) -> None:
    db.collection("recipes").document(product_id).set({"items": items}, merge=True)


# ================
#  ТРАНЗАКЦИИ
# ================

def sell_product(product_id: str) -> Optional[str]:
    """Списать ингредиенты по рецепту. Возвращает None если успех, иначе строку ошибки."""
    recipe = get_recipe(product_id)
    if not recipe:
        return "У позиции нет рецепта"

    def _tx(transaction: firestore.Transaction):
        # проверяем остатки + сразу списываем
        for item in recipe:
            ing_id = item["ingredientId"]
            qty = float(item.get("qtyPer", 0))
            ing_ref = db.collection("ingredients").document(ing_id)
            snap = ing_ref.get(transaction=transaction)
            if not snap.exists:
                raise ValueError(f"Ингредиент {ing_id} не найден")

            v = snap.to_dict()
            stock = float(v.get("stock_quantity", 0.0))
            new_val = stock - qty
            if new_val < -1e-9:
                raise ValueError(f"Недостаточно: {v.get('name', ing_id)}")

            transaction.update(ing_ref, {"stock_quantity": new_val})

        # фиксируем продажу
        sale_ref = db.collection("sales").document()
        transaction.set(
            sale_ref,
            {
                "productId": product_id,
                "ts": firestore.SERVER_TIMESTAMP,
                "items": recipe,
            },
        )
        st.session_state["last_sale_id"] = sale_ref.id

    try:
        db.transaction(_tx)
        return None
    except Exception as e:
        return str(e)


def undo_last_sale() -> Optional[str]:
    sale_id = st.session_state.get("last_sale_id")
    if not sale_id:
        return "Нет последней продажи"

    def _tx(transaction: firestore.Transaction):
        sale_ref = db.collection("sales").document(sale_id)
        snap = sale_ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError("Продажа не найдена")

        data = snap.to_dict()
        items = data.get("items", [])
        # возвращаем на склад
        for item in items:
            ing_id = item["ingredientId"]
            qty = float(item.get("qtyPer", 0))
            ing_ref = db.collection("ingredients").document(ing_id)
            snap_ing = ing_ref.get(transaction=transaction)
            if not snap_ing.exists:
                # если вдруг удалили ингредиент — пропускаем
                continue
            v = snap_ing.to_dict()
            stock = float(v.get("stock_quantity", 0.0))
            transaction.update(ing_ref, {"stock_quantity": stock + qty})

        # помечаем откат
        transaction.update(sale_ref, {"undone": True})
        st.session_state["last_sale_id"] = None

    try:
        db.transaction(_tx)
        return None
    except Exception as e:
        return str(e)


def adjust_stock(ingredient_id: str, delta: float) -> Optional[str]:
    """Пополнение/списание склада в ручном режиме (вкладка Склад)."""
    try:
        ref = db.collection("ingredients").document(ingredient_id)
        def _tx(tr: firestore.Transaction):
            snap = ref.get(transaction=tr)
            if not snap.exists:
                raise ValueError("Ингредиент не найден")
            v = snap.to_dict()
            stock = float(v.get("stock_quantity", 0.0))
            new_val = stock + float(delta)
            if new_val < 0:
                raise ValueError("Нельзя уйти в минус")
            tr.update(ref, {"stock_quantity": new_val})
        db.transaction(_tx)
        return None
    except Exception as e:
        return str(e)


# ================
#  СТИЛИ / ТЕМА
# ================

# Светлая нежная тема для склада + карточек
st.html(
    """
    <style>
      .g-card {
        border: 1px solid #ebeef5;
        border-radius: 12px;
        padding: 16px 16px 10px;
        background: #fff;
        box-shadow: 0 1px 0 rgba(30,35,40,0.03);
        transition: box-shadow .15s ease, border-color .15s ease;
      }
      .g-card:hover { box-shadow: 0 4px 16px rgba(30,35,40,.06); border-color:#e4ecfa;}
      .g-tag {
        display:inline-block; padding:2px 8px; border-radius:10px; font-size:12px;
        background:#f5faff; color:#2b61cf; border:1px solid #e4ecfa;
      }
      .g-ok   { background:#effaf1; color:#2c7a3f; border-color:#dcefe1; }
      .g-warn { background:#fff8ea; color:#915700; border-color:#f2e6c9; }
      .g-bad  { background:#ffefef; color:#a12b2b; border-color:#f3d5d5; }

      /* Подсветка выбранного товара */
      .g-active { border-color:#4f46e5; box-shadow:0 0 0 3px rgba(79,70,229,.15) !important; }

      /* Компактная кнопка справа от заголовка */
      .g-header { display:flex; align-items:center; justify-content:space-between; gap:12px; }
      .g-title  { font-weight:600; font-size:16px; margin:0; }
    </style>
    """,
)


# ================
#  СТРАНИЦА
# ================

st.set_page_config(
    page_title="gipsy-office — учёт",
    page_icon="☕",
    layout="wide",
)


# Панель «первая настройка»
with st.expander("⚙️ Первая настройка / создать тестовые данные", expanded=False):
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Создать пример продуктов/рецептов"):
            # создадим минимальные данные (без capacity — только склад)
            batch = db.batch()
            ing = {
                "beans": {"name": "Зёрна", "unit": "g", "stock_quantity": 2000.0, "reorder_threshold": 200},
                "milk": {"name": "Молоко", "unit": "ml", "stock_quantity": 5000.0, "reorder_threshold": 800},
            }
            for k, v in ing.items():
                batch.set(db.collection("ingredients").document(k), v, merge=True)
            prods = {
                "cappuccino": {"name": "Капучино", "price": 250},
                "espresso":   {"name": "Эспрессо", "price": 150},
            }
            for k, v in prods.items():
                batch.set(db.collection("products").document(k), v, merge=True)
            batch.set(db.collection("recipes").document("cappuccino"), {
                "items": [
                    {"ingredientId": "beans", "qtyPer": 18},
                    {"ingredientId": "milk",  "qtyPer": 150},
                ]
            })
            batch.set(db.collection("recipes").document("espresso"), {
                "items": [
                    {"ingredientId": "beans", "qtyPer": 18},
                ]
            })
            batch.commit()
            st.success("Готово! Примеры созданы.")
    with c2:
        if st.button("Очистить все продажи (sales)"):
            batch = db.batch()
            for d in db.collection("sales").stream():
                batch.delete(d.reference)
            batch.commit()
            st.success("Коллекция sales очищена.")
    with c3:
        if st.button("Обновить страницу"):
            st.rerun()


tab_pos, tab_stock, tab_recipes, tab_reports = st.tabs(["Позиции", "Склад", "Рецепты", "Отчёты"])


# -----------------
#   Вкладка ПОЗИЦИИ
# -----------------
with tab_pos:
    ing_map = get_ingredients_map()
    products = get_products()

    q1, q2 = st.columns(2)
    search_l, search_r = q1.text_input("Поиск слева", ""), q2.text_input("Поиск справа", "")
    last_clicked = st.session_state.get("last_clicked_product_id")

    def render_product_card(p: Dict[str, Any]):
        nonlocal last_clicked
        rid = p["id"]
        recipe = get_recipe(rid)
        is_active = last_clicked == rid
        css_active = " g-active" if is_active else ""

        with st.container(border=False):
            st.markdown(f"<div class='g-card{css_active}'>", unsafe_allow_html=True)
            # заголовок + кнопка в одну линию
            c1, c2 = st.columns([6, 1])
            with c1:
                st.markdown(f"<div class='g-header'><h3 class='g-title'>{p['name']} — {int(p['price'])} ₽</h3></div>",
                            unsafe_allow_html=True)
                if recipe:
                    lines = [format_recipe_line(x, ing_map) for x in recipe]
                    st.caption("Состав:\n" + "\n".join(lines))
                else:
                    st.caption("Состав не задан")
            with c2:
                st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
                if st.button("Списать", key=f"sell_{rid}"):
                    err = sell_product(rid)
                    if err:
                        st.error(err)
                    else:
                        st.session_state["last_clicked_product_id"] = rid
                        st.session_state["last_sale_name"] = p["name"]
                        st.session_state["last_sale_id_product"] = rid
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

    # фильтрация по двум колонкам (для широких меню)
    left, right = st.columns(2)
    with left:
        for p in [x for x in products if search_l.strip().lower() in x["name"].lower()]:
            render_product_card(p)
    with right:
        for p in [x for x in products if search_r.strip().lower() in x["name"].lower()]:
            render_product_card(p)

    st.divider()
    c_undo, _ = st.columns([1, 4])
    with c_undo:
        if st.button("↩️ Undo последней продажи", help="Вернуть списанные ингредиенты"):
            msg = undo_last_sale()
            if msg:
                st.error(msg)
            else:
                st.success("Откат выполнен")
                time.sleep(0.6)
                st.rerun()


# -------------
#   Вкладка СКЛАД
# -------------
with tab_stock:
    ing_map = get_ingredients_map()

    st.subheader("Склад (пополнение / корректировки)")
    st.caption("Лёгкая светлая панель для ручного учёта поставок. Быстрые кнопки: +50 / +100 / -10 / -50 и ввод произвольного числа.")

    for ing_id, ing in ing_map.items():
        with st.container(border=False):
            st.markdown("<div class='g-card'>", unsafe_allow_html=True)

            name = ing.get("name", ing_id)
            unit = ing.get("unit", "")
            stock = float(ing.get("stock_quantity", 0.0))
            cap = float(ing.get("capacity", 0.0))  # capacity необязателен — просто 0
            thr = float(ing.get("reorder_threshold", 0.0))

            # индикатор состояния (по относительному уровню к threshold/cap если есть)
            tag_class = "g-ok"
            text = "Супер"
            if cap > 0:
                pr = percent(stock, cap)
                if pr < 25:
                    tag_class, text = "g-bad", "Срочно докупить"
                elif pr < 50:
                    tag_class, text = "g-warn", "Готовиться к закупке"
                elif pr < 75:
                    tag_class, text = "g-tag", "Норм"
                else:
                    tag_class, text = "g-ok", "Супер"
            elif thr > 0:
                if stock <= thr:
                    tag_class, text = "g-bad", "Срочно докупить"
                elif stock <= thr * 2:
                    tag_class, text = "g-warn", "Готовиться к закупке"

            st.markdown(
                f"<div class='g-header'><h4 class='g-title'>{name}</h4><span class='g-tag {tag_class}'>{text}</span></div>",
                unsafe_allow_html=True,
            )

            # строка статуса
            if cap > 0:
                st.caption(f"Остаток: **{stock:g} {unit}** / норма **{cap:g} {unit}**")
            else:
                st.caption(f"Остаток: **{stock:g} {unit}**")

            # быстрые кнопки
            b1, b2, b3, b4, spacer, num, apply = st.columns([1, 1, 1, 1, 0.4, 2, 1])
            with b1:
                if st.button("+50", key=f"plus50_{ing_id}"):
                    msg = adjust_stock(ing_id, +50)
                    st.toast("Пополнено +50" if not msg else msg)
                    st.rerun()
            with b2:
                if st.button("+100", key=f"plus100_{ing_id}"):
                    msg = adjust_stock(ing_id, +100)
                    st.toast("Пополнено +100" if not msg else msg)
                    st.rerun()
            with b3:
                if st.button("-10", key=f"minus10_{ing_id}"):
                    msg = adjust_stock(ing_id, -10)
                    st.toast("Списано -10" if not msg else msg)
                    st.rerun()
            with b4:
                if st.button("-50", key=f"minus50_{ing_id}"):
                    msg = adjust_stock(ing_id, -50)
                    st.toast("Списано -50" if not msg else msg)
                    st.rerun()

            with num:
                val = st.number_input("±число", key=f"num_{ing_id}", value=0.0, step=10.0, label_visibility="collapsed")
            with apply:
                if st.button("Применить", key=f"apply_{ing_id}"):
                    if abs(val) < 1e-9:
                        st.info("Введите ненулевое значение")
                    else:
                        msg = adjust_stock(ing_id, float(val))
                        st.toast("Изменение применено" if not msg else msg)
                        st.rerun()

            st.markdown("</div>", unsafe_allow_html=True)


# ----------------
#   Вкладка РЕЦЕПТЫ
# ----------------
with tab_recipes:
    st.subheader("Редактор рецептов")
    ing_map = get_ingredients_map()
    products = get_products()

    for p in products:
        rid = p["id"]
        st.markdown(f"### {p['name']}")
        current = get_recipe(rid)

        with st.form(key=f"form_{rid}", border=False):
            cols = st.columns([3, 2, 1])
            cols[0].markdown("**Ингредиент**")
            cols[1].markdown("**Дозировка**")
            cols[2].markdown("")

            tmp_items: List[Dict[str, Any]] = []

            # существующие строки
            for idx, it in enumerate(current):
                cc = st.columns([3, 2, 1])
                # выбор ингредиента
                ing_ids = list(ing_map.keys())
                labels = [ing_map[x]["name"] for x in ing_ids]
                default_index = ing_ids.index(it["ingredientId"]) if it["ingredientId"] in ing_ids else 0
                chosen = cc[0].selectbox("ing", options=ing_ids, format_func=lambda x: ing_map[x]["name"],
                                         index=default_index, key=f"{rid}_ing_{idx}", label_visibility="collapsed")
                # количество
                qty = cc[1].number_input("qty", value=float(it.get("qtyPer", 0.0)), step=1.0,
                                         key=f"{rid}_qty_{idx}", label_visibility="collapsed")
                # удалить
                remove = cc[2].checkbox("удалить", value=False, key=f"{rid}_del_{idx}", label_visibility="collapsed")
                if not remove:
                    tmp_items.append({"ingredientId": chosen, "qtyPer": float(qty)})

            st.divider()

            # новая строка
            nc = st.columns([3, 2, 1])
            new_ing = nc[0].selectbox("new_ing", options=list(ing_map.keys()),
                                      format_func=lambda x: ing_map[x]["name"],
                                      key=f"{rid}_new_ing", label_visibility="collapsed")
            new_qty = nc[1].number_input("new_qty", value=0.0, step=1.0,
                                         key=f"{rid}_new_qty", label_visibility="collapsed")
            add_it = nc[2].checkbox("добавить", value=False, key=f"{rid}_add", label_visibility="collapsed")
            if add_it and new_qty > 0:
                tmp_items.append({"ingredientId": new_ing, "qtyPer": float(new_qty)})

            submitted = st.form_submit_button("💾 Сохранить рецепт")
            if submitted:
                save_recipe(rid, tmp_items)
                st.success("Сохранено")
                time.sleep(0.5)
                st.rerun()


# ----------------
#   Вкладка ОТЧЁТЫ
# ----------------
with tab_reports:
    st.subheader("Быстрые отчёты")
    # очень простой отчёт по продажам за сегодня
    today = datetime.now(timezone.utc).date()
    cnt = 0
    total = 0
    by_product = {}

    for d in db.collection("sales").order_by("ts", direction=firestore.Query.DESCENDING).limit(500).stream():
        doc = d.to_dict()
        ts = doc.get("ts")
        if not ts:
            continue
        ts_date = ts.astimezone(timezone.utc).date()
        if ts_date != today:
            continue
        pid = doc.get("productId")
        cnt += 1
        # цена из products:
        # (не идеально быстро, но для 500 документов ок)
        prod = db.collection("products").document(pid).get()
        price = 0
        if prod.exists:
            price = int((prod.to_dict() or {}).get("price", 0))
        total += price
        by_product[pid] = by_product.get(pid, 0) + 1

    c1, c2 = st.columns(2)
    c1.metric("Продаж сегодня", cnt)
    c2.metric("Оборот сегодня, ₽", total)
    st.write("По позициям:")
    if not by_product:
        st.info("Продаж за сегодня ещё нет.")
    else:
        for pid, n in by_product.items():
            prod = db.collection("products").document(pid).get()
            nm = pid
            if prod.exists:
                nm = (prod.to_dict() or {}).get("name", pid)
            st.write(f"- **{nm}**: {n} шт.")


# Конец файла
