# streamlit_app.py
# ──────────────────────────────────────────────────────────────────────────────
# Gipsy Office — учёт списаний (Streamlit + Firestore)
# Вкладки: "Позиции" (списание) и "Склад" (остатки, статусы, экспорт)
# Требуется коллекции в Firestore:
#   - ingredients (документы: <ingredientId> ; поля: stock_quantity: number, unit: "g"/"ml")
#   - products   (документы: <productId>    ; поля: name: string, price: number)
#   - recipes    (документы: <productId>    ; поля: items: [{ingredientId: str, qtyPer: number}])
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import time
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import streamlit as st

import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore


# ==========================
# Firestore init (secrets)
# ==========================
def init_firestore() -> firestore.Client:
    """
    Инициализируем Firebase Admin из Streamlit secrets.
    Поддерживаются два формата:
      A) FIREBASE_SERVICE_ACCOUNT = """{…json…}"""
      B) [FIREBASE_SERVICE_ACCOUNT] ...  (TOML-таблица)
    И нужен PROJECT_ID = "gipsy-office"
    """
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
    if not svc:
        st.error(
            "В Secrets нет FIREBASE_SERVICE_ACCOUNT. "
            "Откройте Manage app → Advanced settings → Edit secrets и вставьте ключ."
        )
        st.stop()

    # TOML-таблица приходит как dict; JSON-строка — как str
    if isinstance(svc, dict):
        data = dict(svc)
    else:
        s = str(svc).strip()
        if s.startswith("{"):
            data = json.loads(s)
        else:
            st.error(
                "FIREBASE_SERVICE_ACCOUNT сохранён в неподдерживаемом виде.\n"
                "Используйте либо TOML-таблицу [FIREBASE_SERVICE_ACCOUNT], "
                "либо JSON в тройных кавычках."
            )
            st.stop()

    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)

    project_id = st.secrets.get("PROJECT_ID")
    if not project_id:
        st.error('В Secrets отсутствует PROJECT_ID = "gipsy-office"')
        st.stop()

    return firestore.Client(project=project_id)


db = init_firestore()

# ==========================
# Доменные настройки
# ==========================
# Нормы для процента заполнения
DEFAULT_CAPACITY: Dict[str, float] = {
    "beans": 2000.0,  # грамм
    "milk": 5000.0,   # мл
}

STATUS_LABELS = [
    (0.75, "Супер"),
    (0.50, "Норм"),
    (0.25, "Готовиться к закупке"),
    (0.00, "Срочно докупить"),
]


def human_status(value: float, capacity: float) -> str:
    """
    Возвращает текстовый статус по порогам.
    value и capacity в одних единицах (г/мл).
    """
    if capacity <= 0:
        return "Нет нормы"
    pct = max(0.0, min(1.0, value / capacity))
    for thr, label in STATUS_LABELS:
        if pct >= thr:
            return label
    return STATUS_LABELS[-1][1]


# ==========================
# Firestore helpers
# ==========================
def _ingredients_ref():
    return db.collection("ingredients")


def _products_ref():
    return db.collection("products")


def _recipes_ref():
    return db.collection("recipes")


def get_ingredients() -> List[Dict[str, Any]]:
    """Читает все ингредиенты. Если нет — возвращает пустой список."""
    docs = _ingredients_ref().stream()
    items: List[Dict[str, Any]] = []
    for d in docs:
        data = d.to_dict() or {}
        items.append({
            "id": d.id,
            "stock_quantity": float(data.get("stock_quantity", 0.0)),
            "unit": str(data.get("unit", "g" if d.id == "beans" else "ml")),
        })
    # подставим capacity из DEFAULT_CAPACITY (если есть)
    for x in items:
        x["capacity"] = float(DEFAULT_CAPACITY.get(x["id"], 0))
    return sorted(items, key=lambda x: x["id"])


def get_products() -> List[Dict[str, Any]]:
    """Читает все продукты из коллекции products."""
    docs = _products_ref().stream()
    items: List[Dict[str, Any]] = []
    for d in docs:
        data = d.to_dict() or {}
        items.append({
            "id": d.id,
            "name": data.get("name", d.id),
            "price": float(data.get("price", 0)),
        })
    return sorted(items, key=lambda x: x["name"].lower())


def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    """
    Возвращает массив {ingredientId, qtyPer} из документа recipes/<product_id>.
    Если документа нет — пустой список.
    """
    doc = _recipes_ref().document(product_id).get()
    if not doc.exists:
        return []
    data = doc.to_dict() or {}
    items = data.get("items", []) or []
    out: List[Dict[str, Any]] = []
    for it in items:
        out.append({
            "ingredientId": str(it.get("ingredientId")),
            "qtyPer": float(it.get("qtyPer", 0)),
        })
    return out


# транзакционное изменение одного ингредиента (нельзя уйти в минус)
def _adjust_tx(transaction, ingredient_id: str, delta: float):
    ref = _ingredients_ref().document(ingredient_id)
    snap = ref.get(transaction=transaction)
    cur = float((snap.to_dict() or {}).get("stock_quantity", 0.0))
    new_val = cur + delta
    if new_val < 0:
        raise ValueError("Нельзя уйти в минус")
    transaction.update(ref, {"stock_quantity": new_val})


def adjust(ingredient_id: str, delta: float):
    tx = db.transaction()
    tx.run(lambda t: _adjust_tx(t, ingredient_id, delta))


# продажа продукта: вычесть ингредиенты по рецепту
def sell_product(product_id: str) -> Tuple[bool, str]:
    recipe = get_recipe(product_id)
    if not recipe:
        return False, "Нет рецепта для этой позиции"

    try:
        deltas = [(it["ingredientId"], -float(it["qtyPer"])) for it in recipe]

        def _tx(t):
            for ing_id, d in deltas:
                _adjust_tx(t, ing_id, d)

        db.transaction().run(_tx)

        # запишем "последнюю продажу" для Undo
        db.collection("meta").document("lastSale").set({
            "ts": firestore.SERVER_TIMESTAMP,
            "productId": product_id,
            "deltas": [{"ingredientId": a, "delta": b} for (a, b) in deltas],
        })
        return True, "Списано"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Ошибка: {e}"


def undo_last_sale() -> Tuple[bool, str]:
    doc = db.collection("meta").document("lastSale").get()
    if not doc.exists:
        return False, "Нет последней продажи"
    data = doc.to_dict() or {}
    deltas = data.get("deltas") or []
    if not deltas:
        return False, "Лог пуст"

    try:
        def _tx(t):
            for it in deltas:
                _adjust_tx(t, it["ingredientId"], -float(it["delta"]))  # обратный знак

        db.transaction().run(_tx)
        # очистим лог
        db.collection("meta").document("lastSale").delete()
        return True, "Последняя продажа отменена"
    except Exception as e:
        return False, f"Ошибка: {e}"


# ==========================
# UI
# ==========================
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

tab1, tab2 = st.tabs(["Позиции", "Склад"])

# --- Позиции ---
with tab1:
    prods = get_products()
    if not prods:
        st.info("Добавьте документы в коллекцию `products`.")
    else:
        c1, c2, c3 = st.columns([6, 2, 2])
        c1.subheader("Позиция")
        c2.subheader("Цена, ₽")
        c3.subheader("Списать")

        for p in prods:
            name = p["name"]
            price = p["price"]
            r1, r2, r3 = st.columns([6, 2, 2])
            r1.write(name)
            r2.write(int(price) if price.is_integer() else price)
            if r3.button("Списать", key=f"sell-{p['id']}"):
                ok, msg = sell_product(p["id"])
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
                st.rerun()

    st.divider()
    if st.button("Undo последней продажи"):
        ok, msg = undo_last_sale()
        (st.success if ok else st.error)(msg)
        st.rerun()

# --- Склад ---
with tab2:
    ing = get_ingredients()
    if not ing:
        st.info("Добавьте документы в коллекцию `ingredients`.")
    else:
        # быстрые шаги по юнитам
        def steps_for_unit(u: str) -> List[Tuple[str, float]]:
            if u == "g":
                return [("+50 g", 50), ("+100 g", 100), ("-10 g", -10), ("-50 g", -50)]
            return [("+50 ml", 50), ("+100 ml", 100), ("-10 ml", -10), ("-50 ml", -50)]

        # левая — кнопки, правая — остатки/статусы
        lc, rc = st.columns([7, 5])

        with lc:
            st.subheader("Склад (операции)")
            for item in ing:
                st.markdown(f"**{item['id'].capitalize()}**  \n"
                            f"{round(100 * item['stock_quantity'] / (item['capacity'] or 1)):d}%")
                cols = st.columns(5)
                for i, (label, d) in enumerate(steps_for_unit(item["unit"])):
                    if cols[i].button(label, key=f"inc-{item['id']}-{label}"):
                        try:
                            adjust(item["id"], d)
                            st.success("Ок")
                        except Exception as e:
                            st.error(str(e))
                        st.rerun()
                # произвольный delta
                delta = cols[-1].number_input("±число", key=f"num-{item['id']}", value=0.0, step=10.0)
                if st.button("Применить", key=f"apply-{item['id']}"):
                    try:
                        adjust(item["id"], float(delta))
                        st.success("Ок")
                    except Exception as e:
                        st.error(str(e))
                    st.rerun()
                st.write("")

        with rc:
            st.subheader("Состояние склада")
            rows = []
            for x in ing:
                cap = x["capacity"] or 0.0
                val = x["stock_quantity"]
                status = human_status(val, cap) if cap > 0 else "Нет нормы"
                rows.append({
                    "Ингредиент": x["id"],
                    "Остаток": f"{int(val) if val.is_integer() else round(val, 1)} {x['unit']}",
                    "Норма": f"{int(cap)} {x['unit']}" if cap else "—",
                    "Статус": status,
                    "Процент": round(100 * val / cap) if cap else 0,
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, hide_index=True, use_container_width=True)

            st.write("")
            # Экспорт списков на закупку
            def low_df(th: float) -> pd.DataFrame:
                data = []
                for x in ing:
                    cap = x["capacity"] or 0
                    if cap <= 0:
                        continue
                    if (x["stock_quantity"] / cap) < th:
                        data.append({
                            "Ингредиент": x["id"],
                            "Остаток": int(x["stock_quantity"]) if float(x["stock_quantity"]).is_integer() else round(x["stock_quantity"], 1),
                            "Норма": int(cap),
                            "Ед.": x["unit"],
                            "Процент": int(round(100 * x["stock_quantity"] / cap)),
                        })
                return pd.DataFrame(data)

            c25, c50 = st.columns(2)
            df25 = low_df(0.25)
            df50 = low_df(0.50)
            c25.download_button(
                "Экспорт <25%",
                data=df25.to_csv(index=False).encode("utf-8"),
                file_name=f"need_to_buy_under_25_{int(time.time())}.csv",
                mime="text/csv",
            )
            c50.download_button(
                "Экспорт <50%",
                data=df50.to_csv(index=False).encode("utf-8"),
                file_name=f"need_to_buy_under_50_{int(time.time())}.csv",
                mime="text/csv",
            )

# ──────────────────────────────────────────────────────────────────────────────
