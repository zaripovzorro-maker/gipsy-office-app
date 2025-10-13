import json
import streamlit as st
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore

def init_firestore() -> firestore.Client:
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not svc:
        st.error("В Secrets нет FIREBASE_SERVICE_ACCOUNT. Открой ⋮ → Edit secrets и вставь ключ.")
        st.stop()

    # Приходит либо строка (JSON), либо Mapping (таблица TOML)
    if isinstance(svc, str):
        data = json.loads(svc)
    else:
        data = dict(svc)

    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)

    project_id = st.secrets.get("PROJECT_ID")
    return firestore.Client(project=project_id)

db = init_firestore()

# =========================
#  Бизнес-логика
# =========================
# Нормы для процентов по умолчанию (можно менять)
DEFAULT_CAPACITY = {"beans": 2000, "milk": 5000}

def capacity_for(ing: dict) -> float:
    # из поля capacity, иначе из дефолтов
    return float(ing.get("capacity") or DEFAULT_CAPACITY.get(ing["id"]) or 0)

def calc_percent(ing: dict) -> Optional[float]:
    cap = capacity_for(ing)
    if cap <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * float(ing.get("stock_quantity", 0)) / cap))

def status_label(p: Optional[float]) -> tuple[str, str]:
    if p is None:
        return ("Нет нормы", "gray")
    if p > 75:
        return ("Супер", "green")
    if p > 50:
        return ("Норм", "limegreen")
    if p > 25:
        return ("Готовиться к закупке", "orange")
    return ("Срочно докупить", "crimson")

def get_products():
    col = db.collection("products").stream()
    return [{"id": d.id, **(d.to_dict() or {})} for d in col]

def get_ingredients():
    col = db.collection("ingredients").stream()
    items = [{"id": d.id, **(d.to_dict() or {})} for d in col]
    for it in items:
        it["stock_quantity"] = float(it.get("stock_quantity", 0))
        it["reorder_threshold"] = float(it.get("reorder_threshold", 0))
    items.sort(key=lambda x: x.get("name", ""))
    return items

# ---------- транзакции ----------
@firestore.transactional
def _adjust_tx(transaction, ingredient_id: str, delta: float):
    ref = db.collection("ingredients").document(ingredient_id)
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        raise ValueError("Ингредиент не найден")
    stock = float(snap.to_dict().get("stock_quantity", 0))
    nxt = stock + float(delta)
    if nxt < 0:
        raise ValueError("Нельзя уйти в минус")
    transaction.update(ref, {"stock_quantity": nxt})

def adjust(ingredient_id: str, delta: float):
    tx = db.transaction()
    _adjust_tx(tx, ingredient_id, delta)

@firestore.transactional
def _sell_tx(transaction, product_id: str, qty: int):
    rref = db.collection("recipes").document(product_id)
    rsnap = rref.get(transaction=transaction)
    if not rsnap.exists:
        raise ValueError("Рецепт не найден")
    items = (rsnap.to_dict() or {}).get("items") or []

    plan = []
    for it in items:
        ing_ref = db.collection("ingredients").document(it["ingredientId"])
        ing_snap = ing_ref.get(transaction=transaction)
        if not ing_snap.exists:
            raise ValueError(f"Ингредиент {it['ingredientId']} не найден")
        stock = float(ing_snap.to_dict().get("stock_quantity", 0))
        need = float(it["qtyPer"]) * qty
        if stock < need:
            name = ing_snap.to_dict().get("name", it["ingredientId"])
            raise ValueError(f"{name}: нужно {need}, есть {stock}")
        plan.append((ing_ref, stock, need))

    for ing_ref, stock, need in plan:
        transaction.update(ing_ref, {"stock_quantity": stock - need})

    sref = db.collection("sales").document()
    transaction.set(sref, {"productId": product_id, "qty": qty, "ts": firestore.SERVER_TIMESTAMP})
    return sref.id

def sell(product_id: str, qty: int = 1):
    tx = db.transaction()
    return _sell_tx(tx, product_id, qty)

def last_sale():
    docs = list(
        db.collection("sales")
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    if not docs:
        return None
    d = docs[0]
    data = d.to_dict() or {}
    data["id"] = d.id
    return data

@firestore.transactional
def _undo_tx(transaction, sale_id: str):
    sref = db.collection("sales").document(sale_id)
    ssnap = sref.get(transaction=transaction)
    if not ssnap.exists:
        raise ValueError("Продажа не найдена")
    s = ssnap.to_dict()
    if s.get("undone"):
        return
    qty = int(s.get("qty", 1))
    pid = s["productId"]

    rref = db.collection("recipes").document(pid)
    rsnap = rref.get(transaction=transaction)
    items = (rsnap.to_dict() or {}).get("items") or []
    for it in items:
        ing_ref = db.collection("ingredients").document(it["ingredientId"])
        ing_snap = ing_ref.get(transaction=transaction)
        stock = float(ing_snap.to_dict().get("stock_quantity", 0))
        back = float(it["qtyPer"]) * qty
        transaction.update(ing_ref, {"stock_quantity": stock + back})

    transaction.update(sref, {"undone": True, "undoneAt": firestore.SERVER_TIMESTAMP})

def undo_last():
    s = last_sale()
    if not s or s.get("undone"):
        raise ValueError("Нет продажи для отката")
    tx = db.transaction()
    _undo_tx(tx, s["id"])

# =========================
#  UI
# =========================
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

tab1, tab2 = st.tabs(["Позиции", "Склад"])

# --- Позиции ---
with tab1:
    products = get_products()
    cols = st.columns(3)
    if not products:
        st.info("Добавь документы в коллекцию `products` (name, price).")
    else:
        for i, p in enumerate(products):
            with cols[i % 3]:
                label = f"{p.get('name','?')} • {p.get('price','')} ₽"
                if st.button(label, key=f"sell-{p['id']}"):
                    try:
                        sid = sell(p["id"], 1)
                        st.success(f"Продано: {p.get('name','?')} (saleId: {sid})")
                        time.sleep(0.3)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()
    if st.button("Undo последней продажи"):
        try:
            undo_last()
            st.success("Откат выполнен")
            time.sleep(0.3)
            st.rerun()
        except Exception as e:
            st.error(str(e))

# --- Склад ---
with tab2:
    ings = get_ingredients()

    # быстрые экспорт-кнопки
    rows = []
    for ing in ings:
        p = calc_percent(ing)
        label, _ = status_label(p)
        rows.append({
            "id": ing["id"],
            "name": ing.get("name",""),
            "unit": ing.get("unit",""),
            "stock": ing.get("stock_quantity",0),
            "capacity": capacity_for(ing) or "",
            "percent": None if p is None else round(p),
            "status": label,
        })

    c1, c2, _ = st.columns([1,1,4])
    if c1.button("Экспорт <50%"):
        df = pd.DataFrame([r for r in rows if (r["percent"] is not None and r["percent"] < 50)])
        if df.empty:
            st.info("Нет позиций < 50%")
        else:
            st.download_button("Скачать CSV (<50%)", df.to_csv(index=False).encode("utf-8"),
                               "under_50.csv", "text/csv")
    if c2.button("Экспорт <25%"):
        df = pd.DataFrame([r for r in rows if (r["percent"] is not None and r["percent"] < 25)])
        if df.empty:
            st.info("Нет позиций < 25%")
        else:
            st.download_button("Скачать CSV (<25%)", df.to_csv(index=False).encode("utf-8"),
                               "under_25.csv", "text/csv")

    st.write("")
    for ing in ings:
        p = calc_percent(ing)
        label, color = status_label(p)
        with st.container(border=True):
            a, b, c = st.columns([2,4,3])
            with a:
                st.subheader(ing.get("name",""))
            with b:
                st.progress(int(p or 0), text=f"{'' if p is None else int(p)}% • {label}")
            with c:
                cap = capacity_for(ing)
                if cap:
                    st.markdown(f"**Остаток:** {int(ing['stock_quantity'])} {ing['unit']} / норма {int(cap)} {ing['unit']}")
                else:
                    st.markdown(f"**Остаток:** {int(ing['stock_quantity'])} {ing['unit']} / норма не задана")

            c1, c2, c3, c4, c5, c6 = st.columns([1,1,1,1,2,2])
            if c1.button(f"+50 {ing['unit']}", key=f"p50-{ing['id']}"):
                try: adjust(ing["id"], +50); st.rerun()
                except Exception as e: st.error(str(e))
            if c2.button(f"+100 {ing['unit']}", key=f"p100-{ing['id']}"):
                try: adjust(ing["id"], +100); st.rerun()
                except Exception as e: st.error(str(e))
            if c3.button(f"-10 {ing['unit']}", key=f"m10-{ing['id']}"):
                try: adjust(ing["id"], -10); st.rerun()
                except Exception as e: st.error(str(e))
            if c4.button(f"-50 {ing['unit']}", key=f"m50-{ing['id']}"):
                try: adjust(ing["id"], -50); st.rerun()
                except Exception as e: st.error(str(e))
            delta = c5.number_input("±число", key=f"n-{ing['id']}", value=0, step=1)
            if c6.button("Применить", key=f"a-{ing['id']}"):
                try:
                    if delta != 0:
                        adjust(ing["id"], float(delta))
                        st.rerun()
                except Exception as e:
                    st.error(str(e))
