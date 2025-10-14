import json, streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore

def init_firestore():
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not svc:
        st.error("❌ В Secrets не найден FIREBASE_SERVICE_ACCOUNT. Открой ⋯ → Edit secrets и вставь ключ.")
        st.stop()

    # Преобразуем к dict
    if isinstance(svc, dict):
        data = svc
    elif isinstance(svc, str):
        try:
            data = json.loads(svc)
        except Exception as e:
            st.error("❌ FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или TOML-таблицей.")
            st.stop()
    else:
        st.error("❌ FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или TOML-таблицей.")
        st.stop()

    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, {"projectId": st.secrets.get("PROJECT_ID", data.get("project_id"))})
    return firestore.client()

db = init_firestore()

# ======= Коллекции / схемы =======
COL_ING = "ingredients"   # документы: beans, milk, ...  поля: name, unit, stock_quantity, reorder_threshold
COL_PROD = "products"     # документы: произвольные id, поля: name, price
COL_REC = "recipes"       # документы: productId, поле items: [{ingredientId, qtyPer}]
COL_SALES = "sales"       # документы: {productId, ts, items:[{ingredientId, qty}]}

DEFAULT_UNITS = {"beans": "g", "milk": "ml"}


# =============  Helpers (чтение данных)  =============
@st.cache_data(ttl=10)
def get_ingredients_map() -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    for doc in db.collection(COL_ING).stream():
        d = doc.to_dict() or {}
        d.setdefault("name", doc.id)
        d.setdefault("unit", DEFAULT_UNITS.get(doc.id, ""))
        d.setdefault("stock_quantity", 0)
        d.setdefault("reorder_threshold", 0)
        out[doc.id] = d
    return out


@st.cache_data(ttl=10)
def get_products() -> List[Dict]:
    rows = []
    for doc in db.collection(COL_PROD).stream():
        d = doc.to_dict() or {}
        d["id"] = doc.id
        d.setdefault("name", doc.id)
        d.setdefault("price", 0)
        rows.append(d)
    rows.sort(key=lambda x: x["name"].lower())
    return rows


@st.cache_data(ttl=10)
def get_recipe(product_id: str) -> List[Dict]:
    r = db.collection(COL_REC).document(product_id).get()
    if not r.exists:
        return []
    d = r.to_dict() or {}
    return d.get("items", [])


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# =============  Транзакции списания/отката  =============
def sell_product(product_id: str) -> Optional[str]:
    """
    Списывает ингредиенты по рецепту продукта.
    Пишет документ в sales для отката.
    """
    ing_map = get_ingredients_map()
    items = get_recipe(product_id)
    if not items:
        return "У продукта нет рецепта."

    # Валидация и подготовка
    need: Dict[str, float] = {}
    for it in items:
        ing_id = str(it.get("ingredientId", "")).strip()
        qty = float(it.get("qtyPer", 0))
        if ing_id and qty > 0:
            need[ing_id] = need.get(ing_id, 0.0) + qty

    # Транзакция
    def _tx_fn(transaction: firestore.Transaction):
        for ing_id, qty in need.items():
            ref = db.collection(COL_ING).document(ing_id)
            snap = ref.get(transaction=transaction)
            cur = (snap.to_dict() or {}).get("stock_quantity", 0)
            nxt = cur - qty
            if nxt < 0:
                raise ValueError(f"Ингредиента «{ing_map.get(ing_id,{}).get('name',ing_id)}» не хватает.")
            transaction.update(ref, {"stock_quantity": nxt})

        db.collection(COL_SALES).add({
            "productId": product_id,
            "ts": _now_ts(),
            "items": [{"ingredientId": k, "qty": v} for k, v in need.items()]
        })

    try:
        db.transaction()( _tx_fn )  # run transaction
        get_ingredients_map.clear()  # сброс кэша
        return None
    except Exception as e:
        return str(e)


def undo_last_sale() -> Optional[str]:
    """
    Находит самый свежий документ в sales, возвращает списанное.
    """
    try:
        q = db.collection(COL_SALES).order_by("ts", direction=firestore.Query.DESCENDING).limit(1).stream()
        last = None
        for d in q:
            last = d
            break
        if not last:
            return "Нет продаж для отката."

        sale = last.to_dict() or {}
        items = sale.get("items", [])

        def _tx_fn(transaction: firestore.Transaction):
            for it in items:
                ing_id = it["ingredientId"]
                qty = float(it["qty"])
                ref = db.collection(COL_ING).document(ing_id)
                snap = ref.get(transaction=transaction)
                cur = (snap.to_dict() or {}).get("stock_quantity", 0.0)
                transaction.update(ref, {"stock_quantity": cur + qty})
            transaction.delete(last.reference)

        db.transaction()(_tx_fn)
        get_ingredients_map.clear()
        return None
    except Exception as e:
        return str(e)


# =============  UI: CSS  =============
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")

st.markdown("""
<style>
:root{
  --card-bg: #fff;
  --card-border: #e5e7eb;
}
.card {
  background:var(--card-bg); border:1px solid var(--card-border);
  border-radius:14px; padding:14px; margin-bottom:12px;
  display:flex; flex-direction:column; min-height:220px;
}
.card .grow { flex:1; }
.big-btn button[kind="secondary"]{ width:100%; padding:16px 18px; font-size:18px; border-radius:12px; }

.badge { font-size:12px; padding:2px 8px; border-radius:999px; background:#eef; margin-left:6px;}
.caption { color:#6b7280; font-size:14px; line-height:1.5; }
.price { font-weight:600; opacity:.85; }

/* плитки */
.tile { margin-bottom:12px; }
.tile .stButton>button {
  width:100%; height:88px; font-size:20px; font-weight:700;
  border-radius:14px; border:1px solid var(--card-border);
  background:#ffffff;
}
.tile .stButton>button:hover { background:#f3f4f6; }
.tile .price { font-weight:600; opacity:.85; margin-left:6px; }

/* таблицы пополнения */
.small-input input{ height:36px; }
.ok {color:#16a34a;}
.warn {color:#f59e0b;}
.danger {color:#dc2626;}
</style>
""", unsafe_allow_html=True)


# =============  UI: Заголовок  =============
st.title("☕ gipsy-office — учёт списаний")
st.caption("Лёгкий интерфейс для бариста: быстрые продажи, понятный склад, редактирование рецептов.")


# =============  TABs  =============
TAB_POS, TAB_STOCK, TAB_REC, TAB_REP, TAB_QR = st.tabs(["Позиции", "Склад", "Рецепты", "Отчёты", "QR-коды"])


# ---------- TAB: Позиции ----------
with TAB_POS:
    # режим карточки/плитки
    try:
        view_mode = st.segmented_control("Вид", options=["Карточки", "Плитки"], default="Карточки")
    except Exception:
        view_mode = st.radio("Вид", ["Карточки", "Плитки"], horizontal=True, index=0)

    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.success(f"Списано: {last_sale_name}", icon="✅")

    prods = get_products()
    ing_map = get_ingredients_map()

    if not prods:
        st.info("Добавьте продукты в коллекцию `products`.")
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
                        st.markdown(f'<div class="card">', unsafe_allow_html=True)
                        st.write(f"**{p['name']}** " + (f"<span class='badge'>только что</span>" if is_last else ""), unsafe_allow_html=True)
                        st.write(f"<span class='price'>{int(p['price'])} ₽</span>", unsafe_allow_html=True)

                        # состав
                        if recipe:
                            lines = []
                            for it in recipe:
                                ing = ing_map.get(it["ingredientId"], {"name": it["ingredientId"], "unit": ""})
                                qty = float(it.get("qtyPer", 0))
                                qty_text = str(int(qty)) if qty.is_integer() else f"{qty}"
                                lines.append(f"• {ing['name']}: {qty_text} {ing['unit']}")
                            st.write("<div class='caption'>" + "<br>".join(lines) + "</div>", unsafe_allow_html=True)
                        else:
                            st.write("<div class='caption'>Состав не задан</div>", unsafe_allow_html=True)

                        st.markdown('<div class="grow"></div>', unsafe_allow_html=True)
                        st.markdown('<div class="big-btn">', unsafe_allow_html=True)
                        if st.button("Списать", key=f"sell_{p['id']}", use_container_width=True):
                            err = sell_product(p["id"])
                            if err: st.error(err)
                            else:
                                st.session_state["last_sale_name"] = p["name"]
                                st.session_state["last_sale_id"] = p["id"]
                                st.rerun()
                        st.markdown('</div></div>', unsafe_allow_html=True)

        else:  # Плитки
            cols_per_row = 4
            for i in range(0, len(prods), cols_per_row):
                row = prods[i:i+cols_per_row]
                cols = st.columns(cols_per_row)
                for col, p in zip(cols, row):
                    with col:
                        st.markdown('<div class="tile">', unsafe_allow_html=True)
                        label = f"☕ {p['name']}  ·  <span class='price'>{int(p['price'])} ₽</span>"
                        clicked = st.button(label, key=f"tile_{p['id']}")
                        if clicked:
                            err = sell_product(p["id"])
                            if err: st.error(err)
                            else:
                                st.session_state["last_sale_name"] = p["name"]
                                st.session_state["last_sale_id"] = p["id"]
                                st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)

        st.divider()
        if st.button("↩️ Undo последней продажи"):
            err = undo_last_sale()
            if err: st.error(err)
            else:
                st.success("✅ Откат выполнен.")
                st.session_state["last_sale_name"] = None
                st.session_state["last_sale_id"] = None
                st.rerun()


# ---------- TAB: Склад ----------
with TAB_STOCK:
    st.subheader("Склад (пополнение/списание)")

    ing_map = get_ingredients_map()
    if not ing_map:
        st.info("Добавьте документы в коллекцию `ingredients` (например, `beans`, `milk`).")
    else:
        c1, c2, c3, c4 = st.columns([2,1,1,2])
        c1.write("**Ингредиент**")
        c2.write("**Остаток**")
        c3.write("**Ед.**")
        c4.write("**Действие**")

        for ing_id, ing in ing_map.items():
            name = ing.get("name", ing_id)
            unit = ing.get("unit", "")
            q = float(ing.get("stock_quantity", 0))
            thr = float(ing.get("reorder_threshold", 0))

            status = "ok"
            if thr > 0:
                if q <= thr * 0.25: status = "danger"
                elif q <= thr * 0.5: status = "warn"
            badge = {"ok":"🟢","warn":"🟠","danger":"🔴"}[status]

            col1, col2, col3, col4 = st.columns([2,1,1,2])
            col1.write(f"{badge} **{name}**")
            col2.write(f"{int(q) if q.is_integer() else q}")
            col3.write(unit)

            with col4:
                a, b, c, d, e = st.columns([1,1,1,1,2])
                if a.button("+", key=f"plus1_{ing_id}"):
                    db.collection(COL_ING).document(ing_id).update({"stock_quantity": q + 1})
                if b.button("+10", key=f"plus10_{ing_id}"):
                    db.collection(COL_ING).document(ing_id).update({"stock_quantity": q + 10})
                if c.button("-1", key=f"minus1_{ing_id}") and q-1 >= 0:
                    db.collection(COL_ING).document(ing_id).update({"stock_quantity": q - 1})
                if d.button("-10", key=f"minus10_{ing_id}") and q-10 >= 0:
                    db.collection(COL_ING).document(ing_id).update({"stock_quantity": q - 10})
                delta = e.number_input("±", key=f"custom_{ing_id}", value=0.0, step=1.0, label_visibility="collapsed")
                if st.button("Применить", key=f"apply_{ing_id}"):
                    new_q = q + float(delta)
                    if new_q < 0: new_q = 0
                    db.collection(COL_ING).document(ing_id).update({"stock_quantity": new_q})
                    st.experimental_rerun()

        st.caption("Подсветка: 🟢 норма, 🟠 готовимся к закупке, 🔴 срочно докупить.")


# ---------- TAB: Рецепты ----------
with TAB_REC:
    st.subheader("Рецепты")
    prods = get_products()
    if not prods:
        st.info("Добавьте хотя бы один продукт в `products`.")
    else:
        p_names = {p["name"]: p["id"] for p in prods}
        chosen_name = st.selectbox("Выберите продукт", list(p_names.keys()))
        pid = p_names[chosen_name]
        items = get_recipe(pid)
        ing_map = get_ingredients_map()

        st.write("Текущий состав:")
        for idx, it in enumerate(items):
            col1, col2, col3, col4 = st.columns([3,1,1,1])
            ing_id = it.get("ingredientId", "")
            qty = float(it.get("qtyPer", 0))
            ing_name = ing_map.get(ing_id, {}).get("name", ing_id)
            unit = ing_map.get(ing_id, {}).get("unit", "")

            col1.write(ing_name)
            col2.write(qty)
            col3.write(unit)
            if col4.button("Удалить", key=f"del_{pid}_{idx}"):
                new = items[:idx] + items[idx+1:]
                db.collection(COL_REC).document(pid).set({"items": new}, merge=True)
                st.experimental_rerun()

        st.divider()
        st.write("Добавить ингредиент:")
        ing_options = {v.get("name", k): k for k, v in ing_map.items()}
        new_ing_name = st.selectbox("Ингредиент", list(ing_options.keys()), key="add_ing")
        new_qty = st.number_input("Количество", min_value=0.0, step=1.0, key="add_qty")
        if st.button("Добавить в рецепт"):
            new_item = {"ingredientId": ing_options[new_ing_name], "qtyPer": float(new_qty)}
            db.collection(COL_REC).document(pid).set({"items": items + [new_item]}, merge=True)
            st.success("Сохранено.")
            st.experimental_rerun()


# ---------- TAB: Отчёты ----------
with TAB_REP:
    st.subheader("Отчёты")
    st.info("Здесь можем сделать сводку продаж по дням/неделям, контроль списаний и т.д. (позже).")


# ---------- TAB: QR-коды ----------
with TAB_QR:
    st.subheader("QR-коды")
    st.info("Идея: печатаем QR для инвентаризации/поставок. В сканере открываем ссылку на ингредиент "
            "с быстрым пополнением. Реализуем, когда решим, как будете сканировать на точке.")
