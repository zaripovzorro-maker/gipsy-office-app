# streamlit_app.py
# gipsy-office — учёт списаний / склад / рецепты (Streamlit + Firestore)

from __future__ import annotations
import json
import time
from typing import Dict, List, Any

import pandas as pd
import streamlit as st

import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore

# ==============================
# Firestore init из st.secrets
# ==============================
def init_firestore() -> firestore.Client:
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)
    if not svc:
        st.error("В Secrets нет FIREBASE_SERVICE_ACCOUNT. Открой Manage app → Edit secrets и вставь ключ.")
        st.stop()

    # допускаем JSON-строку или TOML-таблицу
    if isinstance(svc, str):
        try:
            data = json.loads(svc)
        except Exception as e:
            st.error(f"FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой. Ошибка: {e}")
            st.stop()
    else:
        # toml-таблица → dict
        data = dict(svc)

    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)

    project_id = st.secrets.get("PROJECT_ID")
    if not project_id:
        st.error("В Secrets нет PROJECT_ID.")
        st.stop()

    return firestore.Client(project=project_id)

db = init_firestore()

# =======================================
# Коллекции и базовые функции Firestore
# =======================================
COL_PRODUCTS   = "products"
COL_ING        = "ingredients"
COL_RECIPES    = "recipes"
COL_SALES      = "sales"
DOC_META_LAST  = "meta/last_sale"  # для Undo

def doc_to_dict(doc) -> Dict[str, Any]:
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d

def get_products() -> List[Dict[str, Any]]:
    return [doc_to_dict(d) for d in db.collection(COL_PRODUCTS).stream()]

def get_ingredients_map() -> Dict[str, Dict[str, Any]]:
    m = {}
    for d in db.collection(COL_ING).stream():
        m[d.id] = doc_to_dict(d)
    return m

def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    doc = db.collection(COL_RECIPES).document(product_id).get()
    data = doc.to_dict() or {}
    return data.get("items", [])

# транзакция списания с записью операций — пригодится для Undo
def _sell_tx(transaction, product_id: str) -> str | None:
    recipe = get_recipe(product_id)
    if not recipe:
        return "У продукта нет рецепта."

    ing_refs = []
    ops = []  # для Undo
    # проверка остатков и сбор обновлений
    for item in recipe:
        ing_id = item["ingredientId"]
        qty    = float(item.get("qtyPer", 0))
        if qty <= 0:
            return f"В рецепте {product_id} неверная дозировка {qty} у {ing_id}."

        ref = db.collection(COL_ING).document(ing_id)
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return f"Ингредиент {ing_id} не найден."
        data = snap.to_dict() or {}
        stock = float(data.get("stock_quantity", 0.0))
        next_val = stock - qty
        if next_val < 0:
            return f"Недостаточно на складе: {data.get('name', ing_id)}. Остаток {stock}, нужно {qty}."

        ing_refs.append((ref, next_val))
        ops.append({"ingredientId": ing_id, "delta": -qty})

    # применяем обновления
    for ref, next_val in ing_refs:
        transaction.update(ref, {"stock_quantity": next_val})

    # сохраняем «последнюю продажу» для Undo в одном документе
    transaction.set(db.document(DOC_META_LAST), {"productId": product_id, "ops": ops, "ts": firestore.SERVER_TIMESTAMP})
    # и лог продаж
    transaction.set(db.collection(COL_SALES).document(), {"productId": product_id, "ops": ops, "ts": firestore.SERVER_TIMESTAMP})
    return None

def sell_product(product_id: str) -> str | None:
    try:
        return db.transaction()( _sell_tx )(product_id)
    except Exception as e:
        return f"Ошибка списания: {e}"

def _undo_tx(transaction) -> str | None:
    snap = db.document(DOC_META_LAST).get(transaction=transaction)
    if not snap.exists:
        return "Нет операции для отката."

    meta = snap.to_dict() or {}
    ops: List[Dict[str, Any]] = meta.get("ops", [])
    if not ops:
        return "Пустая операция."

    # откатываем: просто меняем знак и плюсуем
    for op in ops:
        ing_id = op["ingredientId"]
        delta  = float(op.get("delta", 0))  # в last было отрицательное
        ref = db.collection(COL_ING).document(ing_id)
        s = ref.get(transaction=transaction).to_dict() or {}
        stock = float(s.get("stock_quantity", 0.0))
        transaction.update(ref, {"stock_quantity": stock - delta})  # минус минус = плюс

    transaction.delete(db.document(DOC_META_LAST))
    return None

def undo_last_sale() -> str | None:
    try:
        return db.transaction()(_undo_tx)()
    except Exception as e:
        return f"Ошибка Undo: {e}"

# ================================
# Стили (карточки / плитки / UI)
# ================================
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")

st.markdown("""
<style>
:root{
  --card-bg:#ffffff; --card-border:#e5e7eb;
}
[data-testid="stAppViewContainer"] { background:#fafafa; }

/* заголовок компактнее */
h1 { margin-bottom:.25rem }

/* карточки одной высоты + кнопка внизу */
.card {
  background:var(--card-bg); border:1px solid var(--card-border);
  border-radius:14px; padding:14px; margin-bottom:12px;
  display:flex; flex-direction:column; min-height:220px;
}
.card .price{ font-weight:700; opacity:.85; margin:.25rem 0 .5rem }
.card .caption{ color:#6b7280; font-size:0.9rem; line-height:1.35rem }
.card .grow{ flex:1 }
.big-btn button{ width:100%; padding:14px 16px; font-size:18px; border-radius:12px; }

/* бейдж «только что» */
.badge{
  display:inline-block; margin-left:.5rem;
  background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0;
  padding:.15rem .45rem; font-size:.78rem; border-radius:999px;
}

/* плитки — большие кнопки */
.tile{ margin-bottom:12px; }
.tile .stButton>button {
  width:100%; height:88px; font-size:20px; font-weight:700;
  border-radius:14px; border:1px solid var(--card-border);
  background:#ffffff;
}
.tile .stButton>button:hover { background:#f3f4f6; }
.tile .price { font-weight:600; opacity:.85; margin-left:8px }

/* лёгкий «хайлайт» последней продажи */
.highlight{ box-shadow:0 0 0 2px #86efac inset; }
</style>
""", unsafe_allow_html=True)

st.title("gipsy-office — учёт списаний")

# Панель «первая настройка»
with st.expander("⚙️ Первая настройка / создать тестовые данные"):
    colA, colB = st.columns(2)
    with colA:
        if st.button("Создать пример данных"):
            # ингредиенты
            db.collection(COL_ING).document("beans").set({
                "name":"Зёрна", "unit":"g", "capacity":2000, "stock_quantity":1964, "reorder_threshold":200
            })
            db.collection(COL_ING).document("milk").set({
                "name":"Молоко", "unit":"ml", "capacity":5000, "stock_quantity":1650, "reorder_threshold":1000
            })
            # продукты
            db.collection(COL_PRODUCTS).document("cappuccino").set({"name":"Капучино", "price":250})
            db.collection(COL_PRODUCTS).document("espresso").set({"name":"Эспрессо", "price":150})
            # рецепты
            db.collection(COL_RECIPES).document("espresso").set({"items":[{"ingredientId":"beans","qtyPer":18}]})
            db.collection(COL_RECIPES).document("cappuccino").set({"items":[
                {"ingredientId":"beans","qtyPer":18},{"ingredientId":"milk","qtyPer":150}
            ]})
            st.success("Готово.")

# =================
# ВКЛАДКИ
# =================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["Позиции","Склад","Рецепты","Отчёты","QR-коды"])

# -----------------
# Позиции (продажи)
# -----------------
with tab1:
    try:
        view_mode = st.segmented_control("Вид", options=["Карточки","Плитки"], default="Карточки")
    except Exception:
        view_mode = st.radio("Вид", ["Карточки","Плитки"], horizontal=True, index=0)

    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id   = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.success(f"Списано: {last_sale_name}", icon="✅")

    prods  = get_products()
    ingmap = get_ingredients_map()

    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        if view_mode == "Карточки":
            cols_per_row = 3
            for i in range(0, len(prods), cols_per_row):
                row = prods[i:i+cols_per_row]
                cols = st.columns(cols_per_row)
                for col, p in zip(cols, row):
                    recipe = get_recipe(p["id"])
                    is_last = (p["id"] == last_sale_id)
                    with col:
                        st.markdown(f'<div class="card {"highlight" if is_last else ""}">', unsafe_allow_html=True)
                        st.write(f"**{p['name']}** " + (f"<span class='badge'>только что</span>" if is_last else ""), unsafe_allow_html=True)
                        st.write(f"<div class='price'>{int(p.get('price',0))} ₽</div>", unsafe_allow_html=True)
                        # состав
                        if recipe:
                            lines=[]
                            for it in recipe:
                                ing = ingmap.get(it["ingredientId"], {"name":it["ingredientId"], "unit":""})
                                qty = float(it.get("qtyPer",0))
                                qty_txt = str(int(qty)) if qty.is_integer() else f"{qty}"
                                lines.append(f"• {ing['name']}: {qty_txt} {ing['unit']}")
                            st.write("<div class='caption'>"+"\n".join(lines)+"</div>", unsafe_allow_html=True)
                        else:
                            st.write("<div class='caption'>Состав не задан</div>", unsafe_allow_html=True)

                        st.markdown('<div class="grow"></div>', unsafe_allow_html=True)
                        st.markdown('<div class="big-btn">', unsafe_allow_html=True)
                        if st.button("Списать", key=f"sell_{p['id']}", use_container_width=True):
                            err = sell_product(p["id"])
                            if err: st.error(err)
                            else:
                                st.session_state["last_sale_name"] = p["name"]
                                st.session_state["last_sale_id"]   = p["id"]
                                st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)
                        st.markdown('</div>', unsafe_allow_html=True)

        else:  # Плитки
            cols_per_row = 4
            for i in range(0, len(prods), cols_per_row):
                row = prods[i:i+cols_per_row]
                cols = st.columns(cols_per_row)
                for col, p in zip(cols, row):
                    with col:
                        st.markdown('<div class="tile">', unsafe_allow_html=True)
                        label = f"☕ {p['name']}  ·  <span class='price'>{int(p.get('price',0))} ₽</span>"
                        clicked = st.button(label, key=f"tile_{p['id']}")
                        if clicked:
                            err = sell_product(p["id"])
                            if err: st.error(err)
                            else:
                                st.session_state["last_sale_name"] = p["name"]
                                st.session_state["last_sale_id"]   = p["id"]
                                st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)

        st.divider()
        if st.button("↩️ Undo последней продажи"):
            err = undo_last_sale()
            if err: st.error(err)
            else:
                st.success("✅ Откат выполнен.")
                st.session_state["last_sale_name"] = None
                st.session_state["last_sale_id"]   = None
                st.rerun()

# -------------
# Склад (объёмы)
# -------------
with tab2:
    st.subheader("Склад / пополнение")
    ingmap = get_ingredients_map()
    if not ingmap:
        st.info("Нет ингредиентов.")
    else:
        for ing_id, ing in ingmap.items():
            cap   = float(ing.get("capacity", 0) or 0)
            stock = float(ing.get("stock_quantity", 0) or 0)
            unit  = ing.get("unit","")

            pct = int(round(stock / cap * 100)) if cap else 0
            col1, col2, col3, col4 = st.columns([2,2,3,2])
            with col1:
                st.write(f"**{ing.get('name', ing_id)}**")
                st.write(f"Остаток: {int(stock)} {unit} / норма {int(cap)} {unit}")
            with col2:
                st.progress(min(pct,100), text=f"{pct}%")
            with col3:
                inc = st.number_input(f"Изменить {ing_id}", value=0.0, step=10.0, key=f"chg_{ing_id}")
                if st.button("Применить", key=f"apply_{ing_id}"):
                    try:
                        ref = db.collection(COL_ING).document(ing_id)
                        s = ref.get().to_dict() or {}
                        curr = float(s.get("stock_quantity",0.0))
                        ref.update({"stock_quantity": max(0.0, curr + float(inc))})
                        st.success("Обновлено.")
                        time.sleep(0.4)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
            with col4:
                # быстрые кнопки
                c1,c2,c3 = st.columns(3)
                with c1:
                    if st.button("+10", key=f"b1_{ing_id}"):
                        ref = db.collection(COL_ING).document(ing_id); s = ref.get().to_dict() or {}
                        ref.update({"stock_quantity": float(s.get("stock_quantity",0))+10})
                        st.rerun()
                with c2:
                    if st.button("+100", key=f"b2_{ing_id}"):
                        ref = db.collection(COL_ING).document(ing_id); s = ref.get().to_dict() or {}
                        ref.update({"stock_quantity": float(s.get("stock_quantity",0))+100})
                        st.rerun()
                with c3:
                    if st.button("-50", key=f"b3_{ing_id}"):
                        ref = db.collection(COL_ING).document(ing_id); s = ref.get().to_dict() or {}
                        ref.update({"stock_quantity": max(0.0, float(s.get("stock_quantity",0))-50)})
                        st.rerun()
        st.caption("Подсказка: кнопки справа — быстрое изменение объёмов.")

# ---------------------
# РЕЦЕПТЫ — редактировать
# ---------------------
with tab3:
    st.subheader("Рецепты")
    prods = get_products()
    if not prods:
        st.info("Нет продуктов.")
    else:
        names = {p["name"]:p["id"] for p in prods}
        sel = st.selectbox("Выбери продукт", list(names.keys()))
        pid = names[sel]

        current = get_recipe(pid)
        # приводим к удобному df
        rows=[]
        for it in current:
            rows.append({
                "ingredientId": it.get("ingredientId",""),
                "qtyPer": float(it.get("qtyPer",0.0))
            })
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["ingredientId","qtyPer"])

        st.write("Ингредиенты и дозировки (qtyPer):")
        edited = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "ingredientId": st.column_config.TextColumn("Ингредиент ID"),
                "qtyPer": st.column_config.NumberColumn("Дозировка", step=1.0, format="%.2f"),
            }
        )

        colS, colR = st.columns([1,1])
        with colS:
            if st.button("💾 Сохранить рецепт"):
                # валидация
                items=[]
                for _, r in edited.iterrows():
                    iid = str(r.get("ingredientId","")).strip()
                    qty = float(r.get("qtyPer",0))
                    if not iid or qty<=0:
                        continue
                    items.append({"ingredientId":iid,"qtyPer":qty})
                db.collection(COL_RECIPES).document(pid).set({"items": items})
                st.success("Рецепт сохранён.")
        with colR:
            if st.button("Очистить рецепт"):
                db.collection(COL_RECIPES).document(pid).set({"items":[]})
                st.success("Очищено.")
                st.rerun()

# -----
# отчёты (заглушка)
# -----
with tab4:
    st.subheader("Отчёты")
    st.caption("Здесь позже сделаем: продажи по дням, расход ингредиентов, прогноз закупок.")
    # простая таблица последних продаж
    sales = [doc_to_dict(d) for d in db.collection(COL_SALES).order_by("ts", direction=firestore.Query.DESCENDING).limit(20).stream()]
    if sales:
        st.write(pd.DataFrame([
            {"when": s.get("ts"), "productId": s.get("productId"), "ops": s.get("ops")} for s in sales
        ]))
    else:
        st.info("Пока нет продаж.")

# -----
# QR-коды (идея)
# -----
with tab5:
    st.subheader("QR-коды (идея)")
    st.write(
        "Можно сгенерировать QR, который открывает приложение с параметром ?sell=ID и сразу списывает. "
        "Это удобно для наклеек на кофемашине.\n\n"
        "Сейчас это демо-вкладка; если захочешь — добавим генерацию QR и обработку параметров."
    )
