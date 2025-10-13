# -*- coding: utf-8 -*-
import os
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import streamlit as st

# --- Firebase / Firestore ---
from firebase_admin import credentials, initialize_app, _apps as firebase_apps
from google.cloud import firestore


# -------------------------------
# Настройки по умолчанию (нормы)
# -------------------------------
DEFAULT_CAPACITY: Dict[str, float] = {
    "beans": 2000.0,   # грамм
    "milk": 5000.0,    # мл
}

# -------------------------------
# Инициализация Firestore
# -------------------------------
def init_firestore() -> firestore.Client:
    """
    Читает ключ из st.secrets["FIREBASE_SERVICE_ACCOUNT"] (JSON-строка или TOML-таблица)
    и PROJECT_ID. Возвращает firestore.Client.
    """
    # 1) PROJECT_ID из secrets или ENV
    project_id = (st.secrets.get("PROJECT_ID") or os.getenv("PROJECT_ID") or "").strip()
    # 2) Сам ключ
    svc_raw: Any = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)

    if not project_id:
        st.error("❌ В secrets нет PROJECT_ID. Открой меню ⋯ → **Edit secrets** и добавь `PROJECT_ID = \"gipsy-office\"`.")
        st.stop()

    if svc_raw is None:
        st.error("❌ В secrets нет FIREBASE_SERVICE_ACCOUNT. Вставь **полный JSON** сервис-аккаунта либо TOML-таблицу.")
        st.stop()

    # JSON-строка или TOML-таблица — приведём к dict
    if isinstance(svc_raw, str):
        try:
            svc = json.loads(svc_raw)
        except Exception:
            st.error("❌ FIREBASE_SERVICE_ACCOUNT задан строкой, но это не валидный JSON. Скопируй ключ ещё раз целиком.")
            st.stop()
    elif isinstance(svc_raw, dict):
        svc = dict(svc_raw)
    else:
        st.error("❌ FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML (мэп).")
        st.stop()

    # Небольшая диагностика в сайдбаре
    with st.sidebar:
        st.caption("🔎 Диагностика секретов")
        st.write("PROJECT_ID:", project_id)
        st.write("FIREBASE_SERVICE_ACCOUNT type:", type(svc_raw).__name__)
        st.write("has private_key:", bool(svc.get("private_key")))
        st.write("sa project_id:", svc.get("project_id"))

    # Инициализация firebase_admin (ровно один раз)
    cred = credentials.Certificate(svc)
    if not firebase_apps:
        initialize_app(cred, {"projectId": project_id})

    # Клиент Firestore с явным проектом/кредитами
    return firestore.Client(project=project_id, credentials=cred)


# Глобальный клиент БД
db = init_firestore()


# -------------------------------
# Утилиты коллекций
# -------------------------------
def _ingredients_ref():
    return db.collection("ingredients")


def _products_ref():
    return db.collection("products")


def _recipes_ref():
    return db.collection("recipes")


def _sales_ref():
    return db.collection("sales")


# -------------------------------
# Чтение данных с защитой
# -------------------------------
def get_ingredients() -> List[Dict[str, Any]]:
    try:
        docs = _ingredients_ref().stream()
        items: List[Dict[str, Any]] = []
        for d in docs:
            data = d.to_dict() or {}
            items.append({
                "id": d.id,
                "name": data.get("name", d.id),
                "stock_quantity": float(data.get("stock_quantity", 0)),
                "unit": data.get("unit", "g" if d.id == "beans" else "ml"),
                "capacity": float(data.get("capacity", DEFAULT_CAPACITY.get(d.id, 0))),
                "reorder_threshold": float(data.get("reorder_threshold", 0)),
            })
        return sorted(items, key=lambda x: x["id"])
    except Exception as e:
        st.error(f"⚠️ Firestore (ingredients) не отвечает: {e.__class__.__name__}")
        st.info("Проверь, что Firestore создан и у сервисного аккаунта есть роль **Cloud Datastore User**.")
        st.stop()


def get_products() -> List[Dict[str, Any]]:
    try:
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
    except Exception as e:
        st.error(f"⚠️ Firestore (products) не отвечает: {e.__class__.__name__}")
        st.stop()


def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    try:
        doc = _recipes_ref().document(product_id).get()
        if not doc.exists:
            return []
        data = doc.to_dict() or {}
        return list(data.get("items", []))
    except Exception as e:
        st.error(f"⚠️ Firestore (recipes) не отвечает: {e.__class__.__name__}")
        st.stop()


# -------------------------------
# Транзакции: продажа / откат
# -------------------------------
def _sell_tx(tx: firestore.Transaction, product_id: str):
    items = get_recipe(product_id)
    if not items:
        raise ValueError(f"Для продукта '{product_id}' нет рецепта.")

    # проверяем и списываем
    for it in items:
        ing_id = it["ingredientId"]
        qty = float(it["qtyPer"])

        ref = _ingredients_ref().document(ing_id)
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError(f"Ингредиент '{ing_id}' отсутствует.")

        data = snap.to_dict() or {}
        cur = float(data.get("stock_quantity", 0))
        if cur - qty < 0:
            raise ValueError(f"Недостаточно '{ing_id}': есть {cur}, нужно {qty}.")
        tx.update(ref, {"stock_quantity": cur - qty})

    # записываем продажу
    _sales_ref().document().set({
        "product_id": product_id,
        "ts": firestore.SERVER_TIMESTAMP,
        "items": items,
    })


def sell_product(product_id: str) -> Optional[str]:
    tx = db.transaction()
    try:
        tx.run(lambda t: _sell_tx(t, product_id))
        return None
    except Exception as e:
        return str(e)


def undo_last_sale() -> Optional[str]:
    try:
        q = _sales_ref().order_by("ts", direction=firestore.Query.DESCENDING).limit(1).stream()
        last = None
        for d in q:
            last = d
            break
        if not last:
            return "Продаж пока нет."

        sale = last.to_dict() or {}
        items: List[Dict[str, Any]] = sale.get("items", [])
        # возвращаем
        for it in items:
            ing_id = it["ingredientId"]
            qty = float(it["qtyPer"])
            ref = _ingredients_ref().document(ing_id)
            snap = ref.get()
            cur = float((snap.to_dict() or {}).get("stock_quantity", 0))
            ref.update({"stock_quantity": cur + qty})

        last.reference.delete()
        return None
    except Exception as e:
        return str(e)


def adjust_stock(ingredient_id: str, delta: float) -> Optional[str]:
    try:
        ref = _ingredients_ref().document(ingredient_id)
        snap = ref.get()
        if not snap.exists:
            return f"Ингредиент '{ingredient_id}' не найден."
        cur = float((snap.to_dict() or {}).get("stock_quantity", 0))
        new_val = cur + delta
        if new_val < 0:
            return "Нельзя увести остаток в минус."
        ref.update({"stock_quantity": new_val})
        return None
    except Exception as e:
        return str(e)


# -------------------------------
# UI
# -------------------------------
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

# Кнопка первичной инициализации
with st.expander("⚙️ Первая настройка / создать стартовые данные"):
    if st.button("Создать тестовые данные в Firestore"):
        try:
            # ingredients
            _ingredients_ref().document("beans").set({
                "name": "Зёрна",
                "stock_quantity": 2000,
                "unit": "g",
                "capacity": 2000,
                "reorder_threshold": 200,
            }, merge=True)
            _ingredients_ref().document("milk").set({
                "name": "Молоко",
                "stock_quantity": 5000,
                "unit": "ml",
                "capacity": 5000,
                "reorder_threshold": 500,
            }, merge=True)

            # products
            _products_ref().document("cappuccino").set({"name": "Капучино", "price": 250}, merge=True)
            _products_ref().document("espresso").set({"name": "Эспрессо", "price": 150}, merge=True)

            # recipes
            _recipes_ref().document("cappuccino").set({
                "items": [
                    {"ingredientId": "beans", "qtyPer": 18},
                    {"ingredientId": "milk", "qtyPer": 180},
                ]
            }, merge=True)
            _recipes_ref().document("espresso").set({
                "items": [
                    {"ingredientId": "beans", "qtyPer": 18},
                ]
            }, merge=True)

            st.success("Стартовые данные созданы. Перезагрузи страницу (или нажми R).")
        except Exception as e:
            st.error(f"Не удалось создать данные: {e.__class__.__name__}: {e}")

tab1, tab2 = st.tabs(["Позиции", "Склад"])

# -------------------------------
# Позиции (продажи)
# -------------------------------
with tab1:
    prods = get_products()
    if not prods:
        st.info("Добавь продукты в коллекцию `products`, а рецепты — в `recipes`.")
    else:
        cols = st.columns(3)
        for i, p in enumerate(prods):
            with cols[i % 3]:
                st.subheader(p["name"])
                st.caption(f'Цена: {int(p["price"])} ₽')
                if st.button(f"Сделать {p['name']}", key=f"make_{p['id']}"):
                    err = sell_product(p["id"])
                    if err:
                        st.error(f"Не продано: {err}")
                    else:
                        st.success("Списано по рецепту ✅")
                        time.sleep(0.4)
                        st.rerun()

    st.divider()
    if st.button("↩️ Undo последней продажи"):
        err = undo_last_sale()
        if err:
            st.error(err)
        else:
            st.success("Откатили последнюю продажу.")
            time.sleep(0.4)
            st.rerun()

# -------------------------------
# Склад (остатки, статусы, корректировка)
# -------------------------------
def status_label(percent: float) -> str:
    if percent >= 75:
        return "🟢 Супер"
    if percent >= 50:
        return "🟡 Норм"
    if percent >= 25:
        return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"

with tab2:
    ings = get_ingredients()
    if not ings:
        st.info("Нет ингредиентов. Создай стартовые данные (экспандер наверху).")
    else:
        left_col, right_col = st.columns([2, 1])

        with left_col:
            st.subheader("Склад")
            for ing in ings:
                cap = ing.get("capacity") or DEFAULT_CAPACITY.get(ing["id"], 0.0)
                cur = float(ing["stock_quantity"])
                unit = ing["unit"]
                percent = (cur / cap * 100.0) if cap > 0 else 0.0

                st.markdown(f"**{ing['name']}** — {percent:.0f}%")
                c1, c2, c3, c4, c5 = st.columns(5)
                # быстрые кнопки
                step_small = 10 if unit == "g" else 50
                step_big = 100 if unit == "g" else 100

                if c1.button(f"+{step_small} {unit}", key=f"plus_s_{ing['id']}"):
                    err = adjust_stock(ing["id"], step_small)
                    st.experimental_rerun() if not err else st.error(err)
                if c2.button(f"+{step_big} {unit}", key=f"plus_b_{ing['id']}"):
                    err = adjust_stock(ing["id"], step_big)
                    st.experimental_rerun() if not err else st.error(err)
                if c3.button(f"-{step_small} {unit}", key=f"minus_s_{ing['id']}"):
                    err = adjust_stock(ing["id"], -step_small)
                    st.experimental_rerun() if not err else st.error(err)
                if c4.button(f"-{step_big} {unit}", key=f"minus_b_{ing['id']}"):
                    err = adjust_stock(ing["id"], -step_big)
                    st.experimental_rerun() if not err else st.error(err)

                # ручное изменение
                delta = c5.number_input("±число", value=0.0, step=1.0, key=f"delta_{ing['id']}")
                if st.button("Применить", key=f"apply_{ing['id']}"):
                    if delta != 0:
                        err = adjust_stock(ing["id"], float(delta))
                        if err:
                            st.error(err)
                        else:
                            st.success("Изменено")
                            time.sleep(0.4)
                            st.rerun()

                # справа показываем остаток/норму/статус
                st.caption(f"Остаток: **{int(cur)} {unit}** / норма **{int(cap)} {unit}** — {status_label(percent)}")
                st.write("")

        with right_col:
            st.subheader("Экспорт списков")
            low25 = []
            low50 = []
            for ing in ings:
                cap = ing.get("capacity") or DEFAULT_CAPACITY.get(ing["id"], 0.0)
                cur = float(ing["stock_quantity"])
                p = (cur / cap * 100.0) if cap > 0 else 0.0
                if p < 25:
                    low25.append(f"{ing['name']}: осталось {int(cur)} / {int(cap)}")
                elif p < 50:
                    low50.append(f"{ing['name']}: осталось {int(cur)} / {int(cap)}")

            if st.button("Экспорт <25%"):
                if not low25:
                    st.info("Все позиции ≥ 25% 👍")
                else:
                    st.code("\n".join(low25))
            if st.button("Экспорт <50%"):
                if not low50:
                    st.info("Все позиции ≥ 50% 👍")
                else:
                    st.code("\n".join(low50))
