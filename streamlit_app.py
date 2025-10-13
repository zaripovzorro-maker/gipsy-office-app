# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore, google-auth creds)

import os
import json
import time
from typing import Any, Dict, List, Optional
from collections.abc import Mapping

import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account

# Нормы склада (для процентов)
DEFAULT_CAPACITY: Dict[str, float] = {
    "beans": 2000.0,   # грамм
    "milk": 5000.0,    # мл
}

# ──────────────────────────────────────────────────────────────────────────────
# Firestore init — читаем secrets и создаём google-auth креды
# ──────────────────────────────────────────────────────────────────────────────
def init_firestore() -> firestore.Client:
    project_id = (st.secrets.get("PROJECT_ID") or os.getenv("PROJECT_ID") or "").strip()
    svc_raw: Any = st.secrets.get("FIREBASE_SERVICE_ACCOUNT", None)

    # Диагностика (без утечек)
    st.sidebar.write("🔍 Secrets:")
    st.sidebar.write(f"- PROJECT_ID: {project_id or '❌ нет'}")
    st.sidebar.write(f"- FIREBASE_SERVICE_ACCOUNT type: {type(svc_raw).__name__}")

    if not project_id:
        st.error('❌ В secrets нет PROJECT_ID. Добавь строку: PROJECT_ID = "gipsy-office"')
        st.stop()
    if svc_raw is None:
        st.error("❌ В secrets нет FIREBASE_SERVICE_ACCOUNT (таблица TOML или JSON-строка).")
        st.stop()

    # Превращаем в dict (поддерживаем AttrDict, dict, str(JSON))
    if isinstance(svc_raw, Mapping):
        svc = dict(svc_raw)
    elif isinstance(svc_raw, str):
        try:
            svc = json.loads(svc_raw.strip())
        except Exception:
            st.error("❌ FIREBASE_SERVICE_ACCOUNT задан строкой, но это невалидный JSON.")
            st.stop()
    else:
        st.error(f"❌ FIREBASE_SERVICE_ACCOUNT должен быть mapping или JSON-строкой, получено: {type(svc_raw).__name__}")
        st.stop()

    # Быстрые флаги
    st.sidebar.write(f"- has private_key: {bool(svc.get('private_key'))}")
    st.sidebar.write(f"- sa project_id: {svc.get('project_id', '—')}")

    # Создаём google-auth креды из service account info
    try:
        creds = service_account.Credentials.from_service_account_info(svc)
        db = firestore.Client(project=project_id, credentials=creds)
        return db
    except Exception as e:
        st.error(f"❌ Не удалось создать Firestore client: {e}")
        st.info("Проверь формат секрета: [FIREBASE_SERVICE_ACCOUNT] с многострочным private_key в тройных кавычках и PROJECT_ID снаружи.")
        st.stop()

# Глобальный клиент БД
db = init_firestore()

# ──────────────────────────────────────────────────────────────────────────────
# Коллекции
# ──────────────────────────────────────────────────────────────────────────────
def _ingredients_ref():
    return db.collection("ingredients")

def _products_ref():
    return db.collection("products")

def _recipes_ref():
    return db.collection("recipes")

def _sales_ref():
    return db.collection("sales")

# ──────────────────────────────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────────────────────────────
def status_label(percent: float) -> str:
    if percent >= 75: return "🟢 Супер"
    if percent >= 50: return "🟡 Норм"
    if percent >= 25: return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"

def get_ingredients_map() -> Dict[str, Dict[str, Any]]:
    """Карта ингредиентов для форматирования рецептов."""
    docs = _ingredients_ref().stream()
    m: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        data = d.to_dict() or {}
        m[d.id] = {
            "name": data.get("name", d.id),
            "unit": data.get("unit", "g" if d.id == "beans" else "ml"),
            "capacity": float(data.get("capacity", DEFAULT_CAPACITY.get(d.id, 0))),
            "stock_quantity": float(data.get("stock_quantity", 0)),
        }
    return m

def format_recipe_line(recipe_item: Dict[str, Any], ing_map: Dict[str, Dict[str, Any]]) -> str:
    ing_id = recipe_item.get("ingredientId")
    qty = float(recipe_item.get("qtyPer", 0))
    meta = ing_map.get(ing_id, {"name": ing_id, "unit": ""})
    unit = meta.get("unit", "")
    name = meta.get("name", ing_id)
    amount = int(qty) if qty.is_integer() else qty
    return f"- {name}: {amount} {unit}".strip()

# ──────────────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────────────
def get_ingredients() -> List[Dict[str, Any]]:
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
        })
    return sorted(items, key=lambda x: x["id"])

def get_products() -> List[Dict[str, Any]]:
    docs = _products_ref().stream()
    items: List[Dict[str, Any]] = []
    for d in docs:
        # игнорируем служебные документы, например "capacity"
        if d.id.lower() in {"capacity", "_meta", "_settings"}:
            continue
        data = d.to_dict() or {}
        items.append({
            "id": d.id,
            "name": data.get("name", d.id),
            "price": float(data.get("price", 0)),
        })
    return sorted(items, key=lambda x: x["name"].lower())

def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    doc = _recipes_ref().document(product_id).get()
    if not doc.exists:
        return []
    data = doc.to_dict() or {}
    return list(data.get("items", []))

def set_recipe(product_id: str, items: List[Dict[str, Any]]) -> Optional[str]:
    try:
        _recipes_ref().document(product_id).set({"items": items})
        return None
    except Exception as e:
        return str(e)

def adjust_stock(ingredient_id: str, delta: float) -> Optional[str]:
    try:
        ref = _ingredients_ref().document(ingredient_id)
        snap = ref.get()
        cur = float((snap.to_dict() or {}).get("stock_quantity", 0))
        new_val = cur + delta
        if new_val < 0:
            return "❌ Нельзя увести остаток в минус."
        ref.update({"stock_quantity": new_val})
        return None
    except Exception as e:
        return str(e)

def sell_product(product_id: str) -> Optional[str]:
    try:
        recipe = get_recipe(product_id)
        if not recipe:
            return "Нет рецепта для этой позиции."
        # проверяем достаточность и списываем
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

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

# Первая настройка
with st.expander("⚙️ Первая настройка / создать тестовые данные"):
    if st.button("Создать тестовые данные"):
        try:
            _ingredients_ref().document("beans").set({"name": "Зёрна", "stock_quantity": 2000, "unit": "g", "capacity": 2000})
            _ingredients_ref().document("milk").set({"name": "Молоко", "stock_quantity": 5000, "unit": "ml", "capacity": 5000})
            _products_ref().document("cappuccino").set({"name": "Капучино", "price": 250})
            _products_ref().document("espresso").set({"name": "Эспрессо", "price": 150})
            _recipes_ref().document("cappuccino").set({"items": [
                {"ingredientId": "beans", "qtyPer": 18},
                {"ingredientId": "milk",  "qtyPer": 180},
            ]})
            _recipes_ref().document("espresso").set({"items": [
                {"ingredientId": "beans", "qtyPer": 18},
            ]})
            st.success("✅ Стартовые данные созданы. Обнови страницу.")
        except Exception as e:
            st.error(f"Ошибка создания: {e}")

# вкладки
tab1, tab2, tab3 = st.tabs(["Позиции", "Склад", "Рецепты"])

# --- Позиции (с подсветкой последней нажатой и составом) ---
with tab1:
    # бейдж наверху — что списали только что
    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.success(f"Списано: {last_sale_name}", icon="✅")

    prods = get_products()
    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        ing_map = get_ingredients_map()
        for p in prods:
            recipe = get_recipe(p["id"])
            # контейнер с рамкой, если это последняя нажатая позиция
            is_last = (p["id"] == last_sale_id)
            border = "2px solid #22c55e" if is_last else "1px solid rgba(0,0,0,0.08)"
            bg = "rgba(34,197,94,0.06)" if is_last else "rgba(0,0,0,0.02)"
            st.markdown(
                f"""
                <div style="border:{border};background:{bg};border-radius:12px;padding:10px;margin-bottom:8px;">
                """,
                unsafe_allow_html=True
            )
            c1, c2, c3 = st.columns([5, 2, 2])
            c1.write(f"**{p['name']}**" + ("  🟩" if is_last else ""))
            c2.write(f"{int(p['price'])} ₽")

            # состав
            if recipe:
                lines = [format_recipe_line(it, ing_map) for it in recipe]
                c1.caption("Состав:\n" + "\n".join(lines))
            else:
                c1.caption("Состав не задан")

            # кнопка списания
            if c3.button("Списать", key=f"sell_{p['id']}"):
                err = sell_product(p["id"])
                if err:
                    st.error(err)
                else:
                    st.session_state["last_sale_name"] = p["name"]
                    st.session_state["last_sale_id"] = p["id"]
                    st.rerun()

            st.markdown("</div>", unsafe_allow_html=True)

        st.divider()
        if st.button("↩️ Undo последней продажи"):
            err = undo_last_sale()
            if err:
                st.error(err)
            else:
                st.success("✅ Откат выполнен.")
                st.session_state["last_sale_name"] = None
                st.session_state["last_sale_id"] = None
                st.rerun()

# --- Склад ---
def percent(cur: float, cap: float) -> int:
    cap = cap or 1
    return int(round(100 * cur / cap))

with tab2:
    ings = get_ingredients()
    if not ings:
        st.info("Нет ингредиентов. Создай тестовые данные выше.")
    else:
        left, right = st.columns([2, 1])
        with left:
            st.subheader("📦 Склад")
            for i in ings:
                cur = i["stock_quantity"]
                cap = i["capacity"] or DEFAULT_CAPACITY.get(i["id"], 1)
                pct = percent(cur, cap)
                st.markdown(f"**{i['name']}** — {pct}% ({int(cur)} / {int(cap)} {i['unit']}) — {status_label(pct)}")
                c1, c2, c3, c4, c5 = st.columns(5)
                step_small = 10 if i["unit"] == "g" else 50
                step_big   = 100 if i["unit"] == "g" else 100
                if c1.button(f"+{step_small}", key=f"p_s_{i['id']}"):  adjust_stock(i["id"], step_small);  st.rerun()
                if c2.button(f"-{step_small}", key=f"m_s_{i['id']}"):  adjust_stock(i["id"], -step_small); st.rerun()
                if c3.button(f"+{step_big}", key=f"p_b_{i['id']}"):    adjust_stock(i["id"], step_big);    st.rerun()
                if c4.button(f"-{step_big}", key=f"m_b_{i['id']}"):    adjust_stock(i["id"], -step_big);   st.rerun()
                delta = c5.number_input("±", value=0.0, step=1.0, key=f"delta_{i['id']}")
                if st.button("Применить", key=f"apply_{i['id']}"):
                    if delta != 0:
                        err = adjust_stock(i["id"], float(delta))
                        if err: st.error(err)
                        else: st.success("Готово"); st.rerun()
                st.write("")
        with right:
            st.subheader("📉 Недостачи")
            low25 = []
            low50 = []
            for x in ings:
                cap = x["capacity"] or DEFAULT_CAPACITY.get(x["id"], 0) or 1
                cur = x["stock_quantity"]
                p = (cur / cap) * 100
                if p < 25:
                    low25.append(f"{x['name']}: {int(cur)}/{int(cap)} ({p:.0f}%)")
                elif p < 50:
                    low50.append(f"{x['name']}: {int(cur)}/{int(cap)} ({p:.0f}%)")
            if st.button("Показать список <25%"):
                st.code("\n".join(low25) or "Все позиции ≥ 25% 👍")
            if st.button("Показать список <50%"):
                st.code("\n".join(low50) or "Все позиции ≥ 50% 👍")

# --- Рецепты (редактор) ---
with tab3:
    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Нет продуктов. Добавь документы в `products`.")
    else:
        st.caption("Здесь можно быстро редактировать состав каждого напитка: добавлять ингредиенты, менять дозировки, удалять строки.")
        for p in prods:
            with st.expander(f"{p['name']} — рецепт", expanded=False):
                cur_recipe = get_recipe(p["id"])

                # таблица текущих позиций с возможностью изменить qty или удалить
                if cur_recipe:
                    st.markdown("**Текущий состав:**")
                    for idx, it in enumerate(cur_recipe):
                        ing_id = it.get("ingredientId")
                        qty = float(it.get("qtyPer", 0))
                        meta = ing_map.get(ing_id, {"name": ing_id, "unit": ""})
                        cols = st.columns([5, 3, 2, 2])
                        cols[0].write(meta["name"])
                        new_qty = cols[1].number_input("qty", key=f"qty_{p['id']}_{idx}", value=qty, step=1.0)
                        # Сохранить изменение qty
                        if cols[2].button("💾 Сохранить", key=f"save_{p['id']}_{idx}"):
                            cur_recipe[idx]["qtyPer"] = float(new_qty)
                            err = set_recipe(p["id"], cur_recipe)
                            if err: st.error(err)
                            else: st.success("Сохранено"); st.rerun()
                        # Удалить строку
                        if cols[3].button("🗑 Удалить", key=f"del_{p['id']}_{idx}"):
                            new_list = [r for i, r in enumerate(cur_recipe) if i != idx]
                            err = set_recipe(p["id"], new_list)
                            if err: st.error(err)
                            else: st.success("Удалено"); st.rerun()
                else:
                    st.info("Состав пока не задан.")

                st.markdown("---")

                # Добавить новую строку в рецепт
                st.markdown("**Добавить ингредиент:**")
                # список всех ингредиентов
                ing_choices = sorted([(v["name"], k) for k, v in ing_map.items()], key=lambda x: x[0].lower())
                name_to_id = {name: _id for name, _id in ing_choices}
                select_name = st.selectbox("Ингредиент", [n for n, _ in ing_choices], key=f"add_sel_{p['id']}")
                add_id = name_to_id.get(select_name)
                default_unit = ing_map.get(add_id, {}).get("unit", "")
                add_qty = st.number_input(f"Количество ({default_unit})", min_value=0.0, step=1.0, key=f"add_qty_{p['id']}")
                if st.button("➕ Добавить в рецепт", key=f"add_btn_{p['id']}"):
                    new_items = list(cur_recipe) if cur_recipe else []
                    # если ингредиент уже есть — просто обновим qty
                    for item in new_items:
                        if item.get("ingredientId") == add_id:
                            item["qtyPer"] = float(add_qty)
                            break
                    else:
                        new_items.append({"ingredientId": add_id, "qtyPer": float(add_qty)})
                    err = set_recipe(p["id"], new_items)
                    if err: st.error(err)
                    else: st.success("Добавлено"); st.rerun()
