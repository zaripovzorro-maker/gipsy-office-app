# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore)
import os
import json
import time
from typing import Any, Dict, List, Optional
from collections.abc import Mapping

import streamlit as st
from firebase_admin import credentials, initialize_app, _apps as firebase_apps
from google.cloud import firestore


# -------------------------------
# Настройки склада (нормы)
# -------------------------------
DEFAULT_CAPACITY: Dict[str, float] = {
    "beans": 2000.0,   # грамм
    "milk": 5000.0,    # мл
}


# -------------------------------
# Инициализация Firestore
# -------------------------------
def init_firestore() -> firestore.Client:
    """Читает ключ из secrets и возвращает firestore.Client."""
    project_id = (st.secrets.get("PROJECT_ID") or os.getenv("PROJECT_ID") or "").strip()
    svc_raw = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)

    # Диагностика
    st.sidebar.write("🔍 Secrets:")
    st.sidebar.write(f"- PROJECT_ID: {project_id or '❌ нет'}")
    st.sidebar.write(f"- FIREBASE_SERVICE_ACCOUNT type: {type(svc_raw).__name__}")

    if not project_id:
        st.error("❌ В secrets нет PROJECT_ID. Добавь `PROJECT_ID = \"gipsy-office\"`.")
        st.stop()

    if svc_raw is None:
        st.error("❌ В secrets нет FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    # Поддерживаем AttrDict, dict, JSON-строку
    if isinstance(svc_raw, Mapping):
        svc = dict(svc_raw)
    elif isinstance(svc_raw, str):
        s = svc_raw.strip()
        try:
            svc = json.loads(s)
        except Exception:
            st.error("❌ FIREBASE_SERVICE_ACCOUNT задан строкой, но это невалидный JSON.")
            st.stop()
    else:
        st.error(f"❌ FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML (mapping). Получено: {type(svc_raw).__name__}")
        st.stop()

    # Мини-проверка ключа
    st.sidebar.write(f"- has private_key: {bool(svc.get('private_key'))}")
    st.sidebar.write(f"- sa project_id: {svc.get('project_id', '—')}")

    # Firebase Admin init
    cred = credentials.Certificate(svc)
    if not firebase_apps:
        initialize_app(cred, {"projectId": project_id})

    try:
        db = firestore.Client(project=project_id, credentials=cred)
        return db
    except Exception as e:
        st.error(f"❌ Не удалось подключиться к Firestore: {e}")
        st.stop()


# Создаём подключение
db = init_firestore()


# -------------------------------
# Ссылки на коллекции
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
# Утилиты и статусы
# -------------------------------
def status_label(percent: float) -> str:
    if percent >= 75:
        return "🟢 Супер"
    if percent >= 50:
        return "🟡 Норм"
    if percent >= 25:
        return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"


# -------------------------------
# CRUD операции
# -------------------------------
def get_ingredients() -> List[Dict[str, Any]]:
    try:
        docs = _ingredients_ref().stream()
        return sorted([
            {
                "id": d.id,
                "name": d.to_dict().get("name", d.id),
                "stock_quantity": float(d.to_dict().get("stock_quantity", 0)),
                "unit": d.to_dict().get("unit", "g" if d.id == "beans" else "ml"),
                "capacity": float(d.to_dict().get("capacity", DEFAULT_CAPACITY.get(d.id, 0))),
            }
            for d in docs
        ], key=lambda x: x["id"])
    except Exception as e:
        st.error(f"⚠️ Ошибка Firestore (ingredients): {e.__class__.__name__}")
        st.stop()


def get_products() -> List[Dict[str, Any]]:
    try:
        docs = _products_ref().stream()
        return sorted([
            {
                "id": d.id,
                "name": d.to_dict().get("name", d.id),
                "price": float(d.to_dict().get("price", 0)),
            }
            for d in docs
        ], key=lambda x: x["name"].lower())
    except Exception as e:
        st.error(f"⚠️ Ошибка Firestore (products): {e.__class__.__name__}")
        st.stop()


def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    try:
        doc = _recipes_ref().document(product_id).get()
        if not doc.exists:
            return []
        return list(doc.to_dict().get("items", []))
    except Exception as e:
        st.error(f"⚠️ Ошибка Firestore (recipes): {e.__class__.__name__}")
        st.stop()


def adjust_stock(ingredient_id: str, delta: float) -> Optional[str]:
    try:
        ref = _ingredients_ref().document(ingredient_id)
        snap = ref.get()
        cur = float((snap.to_dict() or {}).get("stock_quantity", 0))
        new_val = cur + delta
        if new_val < 0:
            return "❌ Нельзя уйти в минус."
        ref.update({"stock_quantity": new_val})
        return None
    except Exception as e:
        return str(e)


def sell_product(product_id: str) -> Optional[str]:
    try:
        recipe = get_recipe(product_id)
        if not recipe:
            return "Нет рецепта для этой позиции."
        for it in recipe:
            err = adjust_stock(it["ingredientId"], -float(it["qtyPer"]))
            if err:
                return err
        _sales_ref().document().set({
            "product_id": product_id,
            "ts": firestore.SERVER_TIMESTAMP,
            "items": recipe,
        })
        return None
    except Exception as e:
        return str(e)


def undo_last_sale() -> Optional[str]:
    try:
        q = _sales_ref().order_by("ts", direction=firestore.Query.DESCENDING).limit(1).stream()
        last = next(q, None)
        if not last:
            return "Нет продаж для отмены."
        sale = last.to_dict() or {}
        for it in sale.get("items", []):
            adjust_stock(it["ingredientId"], float(it["qtyPer"]))
        last.reference.delete()
        return None
    except Exception as e:
        return str(e)


# -------------------------------
# UI Streamlit
# -------------------------------
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

# Первая настройка
with st.expander("⚙️ Первая настройка / создать тестовые данные"):
    if st.button("Создать тестовые данные"):
        try:
            _ingredients_ref().document("beans").set({"name": "Зёрна", "stock_quantity": 2000, "unit": "g", "capacity": 2000})
            _ingredients_ref().document("milk").set({"name": "Молоко", "stock_quantity": 5000, "unit": "ml", "capacity": 5000})
            _products_ref().document("cappuccino").set({"name": "Капучино", "price": 250})
            _recipes_ref().document("cappuccino").set({"items": [
                {"ingredientId": "beans", "qtyPer": 18},
                {"ingredientId": "milk", "qtyPer": 180}
            ]})
            st.success("✅ Тестовые данные созданы. Перезагрузи страницу.")
        except Exception as e:
            st.error(f"Ошибка создания: {e}")

tab1, tab2 = st.tabs(["Позиции", "Склад"])

# --- Позиции ---
with tab1:
    prods = get_products()
    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        for p in prods:
            c1, c2, c3 = st.columns([4, 2, 2])
            c1.write(f"**{p['name']}**")
            c2.write(f"{int(p['price'])} ₽")
            if c3.button("Списать", key=f"sell_{p['id']}"):
                err = sell_product(p["id"])
                if err:
                    st.error(err)
                else:
                    st.success("✅ Списано!")
                    st.rerun()
    st.divider()
    if st.button("↩️ Undo последней продажи"):
        err = undo_last_sale()
        if err:
            st.error(err)
        else:
            st.success("✅ Откат выполнен.")
            st.rerun()

# --- Склад ---
with tab2:
    ing = get_ingredients()
    if not ing:
        st.info("Нет ингредиентов. Создай тестовые данные выше.")
    else:
        left, right = st.columns([2, 1])
        with left:
            st.subheader("📦 Склад")
            for i in ing:
                cur = i["stock_quantity"]
                cap = i["capacity"] or DEFAULT_CAPACITY.get(i["id"], 1)
                pct = round(100 * cur / cap)
                st.markdown(f"**{i['name']}** — {pct}% ({int(cur)} / {int(cap)} {i['unit']}) — {status_label(pct)}")
                c1, c2, c3, c4 = st.columns(4)
                if c1.button("+50", key=f"p50_{i['id']}"): adjust_stock(i["id"], 50); st.rerun()
                if c2.button("-50", key=f"m50_{i['id']}"): adjust_stock(i["id"], -50); st.rerun()
                if c3.button("+100", key=f"p100_{i['id']}"): adjust_stock(i["id"], 100); st.rerun()
                if c4.button("-100", key=f"m100_{i['id']}"): adjust_stock(i["id"], -100); st.rerun()
                st.write("")
        with right:
            st.subheader("📉 Недостачи")
            low = [x for x in ing if x["stock_quantity"] / x["capacity"] < 0.5]
            if not low:
                st.success("Все позиции в норме!")
            else:
                for x in low:
                    p = round(100 * x["stock_quantity"] / x["capacity"])
                    st.warning(f"{x['name']}: {p}%")
