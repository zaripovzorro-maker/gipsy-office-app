# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore + QR-пополнения)
import os
import json
import io
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import Mapping

import streamlit as st
import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account

# QR
import qrcode
from PIL import Image

# ──────────────────────────────────────────────────────────────────────────────
# Визуальные настройки (светлая тема + кастомные карточки/полосы)
# ──────────────────────────────────────────────────────────────────────────────
LIGHT_CSS = """
<style>
/* общие отступы и фон карточек */
.gy-card {
  border: 1px solid rgba(0,0,0,0.08);
  background: #ffffff;
  border-radius: 14px;
  padding: 14px;
  margin-bottom: 10px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}

/* подсветка последней нажатой позиции */
.gy-card.last {
  border: 2px solid #22c55e;
  background: #f0fff4;
}

/* цветная полоска прогресса */
.gy-bar {
  width: 100%;
  height: 12px;
  border-radius: 999px;
  background: #f0f2f6;
  overflow: hidden;
  border: 1px solid rgba(0,0,0,0.06);
}
.gy-bar > div {
  height: 100%;
  transition: width .3s ease;
}

/* зел/желт/оранж/крас для процентов */
.gy-pct-green { background: linear-gradient(90deg, #86efac, #22c55e); }
.gy-pct-yellow { background: linear-gradient(90deg, #fde68a, #f59e0b); }
.gy-pct-orange { background: linear-gradient(90deg, #fdba74, #f97316); }
.gy-pct-red { background: linear-gradient(90deg, #fca5a5, #ef4444); }

/* большие удобные кнопки */
button[kind="secondary"] { padding: 10px 12px; }
div.stButton > button {
  border-radius: 10px;
  padding: 10px 14px;
  font-weight: 600;
}
</style>
"""
st.markdown(LIGHT_CSS, unsafe_allow_html=True)

# Нормы склада (для процентов, по умолчанию)
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
        st.info("Проверь формат секрета: [FIREBASE_SERVICE_ACCOUNT] с многострочным private_key и PROJECT_ID снаружи.")
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
def _supplies_ref():
    return db.collection("supplies")   # история поставок

# ──────────────────────────────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────────────────────────────
def status_label(percent: float) -> str:
    if percent >= 75: return "🟢 Супер"
    if percent >= 50: return "🟡 Норм"
    if percent >= 25: return "🟠 Готовиться"
    return "🔴 Срочно"

def pct_class(p: float) -> str:
    if p >= 75: return "gy-pct-green"
    if p >= 50: return "gy-pct-yellow"
    if p >= 25: return "gy-pct-orange"
    return "gy-pct-red"

def get_ingredients_map() -> Dict[str, Dict[str, Any]]:
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

def percent(cur: float, cap: float) -> int:
    cap = cap or 1
    return int(round(100 * cur / cap))

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

def set_product_price(product_id: str, new_price: float) -> Optional[str]:
    try:
        _products_ref().document(product_id).set({"price": float(new_price)}, merge=True)
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

def record_supply(ingredient_id: str, quantity: float) -> Optional[str]:
    """Пополнение склада: +quantity к ингредиенту и запись в коллекцию supplies"""
    try:
        err = adjust_stock(ingredient_id, float(quantity))
        if err:
            return err
        _supplies_ref().document().set({
            "ingredient_id": ingredient_id,
            "quantity": float(quantity),
            "ts": firestore.SERVER_TIMESTAMP,
        })
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

# ──────────────────────────────────────────────────────────────────────────────
# Отчёты
# ──────────────────────────────────────────────────────────────────────────────
def get_sales_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = dt_from.astimezone(timezone.utc)
    dt_to_utc = dt_to.astimezone(timezone.utc)
    q = (_sales_ref()
         .where("ts", ">=", dt_from_utc)
         .where("ts", "<", dt_to_utc)
         .order_by("ts"))
    return [dict(d.to_dict() or {}, id=d.id) for d in q.stream()]

def aggregate_sales(sales: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    by_product: Dict[str, int] = {}
    by_ingredient: Dict[str, float] = {}
    for s in sales:
        pid = s.get("product_id", "unknown")
        by_product[pid] = by_product.get(pid, 0) + 1
        for it in s.get("items", []):
            ing = it.get("ingredientId")
            qty = float(it.get("qtyPer", 0))
            by_ingredient[ing] = by_ingredient.get(ing, 0.0) + qty
    df_prod = pd.DataFrame([{"product_id": k, "count": v} for k, v in by_product.items()]).sort_values("count", ascending=False) if by_product else pd.DataFrame(columns=["product_id", "count"])
    df_ing = pd.DataFrame([{"ingredient_id": k, "qty": v} for k, v in by_ingredient.items()]).sort_values("qty", ascending=False) if by_ingredient else pd.DataFrame(columns=["ingredient_id", "qty"])
    return df_prod, df_ing

# ──────────────────────────────────────────────────────────────────────────────
# Query params (быстрые пополнения по QR)
# ──────────────────────────────────────────────────────────────────────────────
def get_query_params() -> Dict[str, List[str]]:
    # Streamlit >=1.30: st.query_params, fallback на experimental
    try:
        return dict(st.query_params)  # type: ignore[attr-defined]
    except Exception:
        return st.experimental_get_query_params()

def base_app_url_input() -> str:
    """URL приложения для QR (можно сохранить в secrets как BASE_URL)."""
    default = st.secrets.get("BASE_URL", "")
    return st.text_input("Базовый URL приложения (для QR)", value=default, placeholder="https://your-app.streamlit.app")

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.title("☕ gipsy-office — учёт списаний")

# Быстрое пополнение, если пришли из QR: ?restock=milk&qty=500
qp = get_query_params()
if "restock" in qp and "qty" in qp:
    ing_id = qp["restock"][0]
    try:
        qty = float(qp["qty"][0])
    except Exception:
        qty = 0.0
    if qty > 0:
        st.info(f"QR-пополнение: {ing_id} +{qty}")
        if st.button("Подтвердить пополнение"):
            err = record_supply(ing_id, qty)
            if err: st.error(err)
            else: st.success("Пополнено ✅"); st.rerun()

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
tab1, tab2, tab3, tab4, tab5 = st.tabs(["Позиции", "Склад", "Рецепты", "Отчёты", "QR-коды"])

# --- Позиции (цветные карточки, состав, подсветка нажатой) ---
with tab1:
    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.success(f"Списано: {last_sale_name}", icon="✅")

    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        cols = st.columns(3)
        for i, p in enumerate(prods):
            recipe = get_recipe(p["id"])
            is_last = (p["id"] == last_sale_id)
            with cols[i % 3]:
                st.markdown(f'<div class="gy-card {"last" if is_last else ""}">', unsafe_allow_html=True)
                st.markdown(f"**{p['name']}** — {int(p['price'])} ₽")
                if recipe:
                    lines = [format_recipe_line(it, ing_map) for it in recipe]
                    st.caption("Состав:\n" + "\n".join(lines))
                else:
                    st.caption("Состав не задан")
                if st.button("Списать", key=f"sell_{p['id']}"):
                    err = sell_product(p["id"])
                    if err: st.error(err)
                    else:
                        st.session_state["last_sale_name"] = p["name"]
                        st.session_state["last_sale_id"] = p["id"]
                        st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

        st.divider()
        if st.button("↩️ Undo последней продажи"):
            err = undo_last_sale()
            if err: st.error(err)
            else:
                st.success("✅ Откат выполнен.")
                st.session_state["last_sale_name"] = None
                st.session_state["last_sale_id"] = None
                st.rerun()

# --- Склад (цветные прогресс-полосы + крупные пополнения) ---
with tab2:
    ings = get_ingredients()
    if not ings:
        st.info("Нет ингредиентов. Создай тестовые данные выше.")
    else:
        for ing in ings:
            cur = ing["stock_quantity"]
            cap = ing["capacity"] or DEFAULT_CAPACITY.get(ing["id"], 1)
            pct = percent(cur, cap)
            unit = ing["unit"]
            st.markdown('<div class="gy-card">', unsafe_allow_html=True)
            st.markdown(f"**{ing['name']}** — {pct}% ({int(cur)} / {int(cap)} {unit}) — {status_label(pct)}")

            # цветная полоса
            st.markdown(
                f"""
                <div class="gy-bar">
                  <div class="{pct_class(pct)}" style="width:{pct}%"></div>
                </div>
                """,
                unsafe_allow_html=True
            )
            c1, c2, c3, c4, c5 = st.columns(5)
            # крупные шаги для пополнений (зависит от unit)
            bigs = (200, 500, 1000, 2000) if unit == "ml" else (100, 250, 500, 1000)
            labels = [f"+{b} {unit}" for b in bigs]
            for idx, b in enumerate(bigs[:4]):
                if [c1, c2, c3, c4][idx].button(labels[idx], key=f"repl_{ing['id']}_{b}"):
                    err = record_supply(ing["id"], b)
                    if err: st.error(err)
                    else: st.success("Пополнено ✅"); st.rerun()

            # произвольное пополнение (тоже как поставка)
            qty = c5.number_input("±", value=0.0, step=1.0, key=f"supply_{ing['id']}")
            if st.button("Применить", key=f"supply_apply_{ing['id']}"):
                if qty != 0:
                    err = record_supply(ing["id"], float(qty))
                    if err: st.error(err)
                    else: st.success("Готово"); st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

# --- Рецепты (редактор + цена) ---
with tab3:
    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Нет продуктов. Добавь документы в `products`.")
    else:
        st.caption("Редактируй состав и цены.")
        for p in prods:
            with st.expander(f"{p['name']} — рецепт и цена", expanded=False):
                price_col, save_col = st.columns([3,1])
                new_price = price_col.number_input("Цена, ₽", min_value=0.0, step=10.0, value=float(p["price"]), key=f"price_{p['id']}")
                if save_col.button("💾 Сохранить цену", key=f"save_price_{p['id']}"):
                    err = set_product_price(p["id"], new_price)
                    if err: st.error(err)
                    else: st.success("Цена обновлена"); st.rerun()

                cur_recipe = get_recipe(p["id"])
                st.markdown("**Текущий состав:**")
                if cur_recipe:
                    for idx, it in enumerate(cur_recipe):
                        ing_id = it.get("ingredientId")
                        qty = float(it.get("qtyPer", 0))
                        meta = ing_map.get(ing_id, {"name": ing_id, "unit": ""})
                        cols = st.columns([5, 3, 2, 2])
                        cols[0].write(meta["name"])
                        new_qty = cols[1].number_input("qty", key=f"qty_{p['id']}_{idx}", value=qty, step=1.0)
                        if cols[2].button("💾 Сохранить", key=f"save_{p['id']}_{idx}"):
                            cur_recipe[idx]["qtyPer"] = float(new_qty)
                            err = set_recipe(p["id"], cur_recipe)
                            if err: st.error(err)
                            else: st.success("Сохранено"); st.rerun()
                        if cols[3].button("🗑 Удалить", key=f"del_{p['id']}_{idx}"):
                            new_list = [r for i, r in enumerate(cur_recipe) if i != idx]
                            err = set_recipe(p["id"], new_list)
                            if err: st.error(err)
                            else: st.success("Удалено"); st.rerun()
                else:
                    st.info("Состав пока не задан.")

                st.markdown("---")
                st.markdown("**Добавить ингредиент:**")
                ing_choices = sorted([(v["name"], k) for k, v in ing_map.items()], key=lambda x: x[0].lower())
                name_to_id = {name: _id for name, _id in ing_choices}
                select_name = st.selectbox("Ингредиент", [n for n, _ in ing_choices], key=f"add_sel_{p['id']}")
                add_id = name_to_id.get(select_name)
                default_unit = ing_map.get(add_id, {}).get("unit", "")
                add_qty = st.number_input(f"Количество ({default_unit})", min_value=0.0, step=1.0, key=f"add_qty_{p['id']}")
                if st.button("➕ Добавить в рецепт", key=f"add_btn_{p['id']}"):
                    new_items = list(cur_recipe) if cur_recipe else []
                    for item in new_items:
                        if item.get("ingredientId") == add_id:
                            item["qtyPer"] = float(add_qty)
                            break
                    else:
                        new_items.append({"ingredientId": add_id, "qtyPer": float(add_qty)})
                    err = set_recipe(p["id"], new_items)
                    if err: st.error(err)
                    else: st.success("Добавлено"); st.rerun()

# --- Отчёты ---
with tab4:
    st.subheader("📊 Отчёты по продажам")
    today = datetime.now().date()
    col_from, col_to, col_btn = st.columns([3,3,2])
    d_from = col_from.date_input("С", value=today)
    d_to = col_to.date_input("По (включительно)", value=today)
    start_dt = datetime.combine(d_from, datetime.min.time()).astimezone()
    end_dt = datetime.combine(d_to, datetime.min.time()).astimezone() + timedelta(days=1)
    if col_btn.button("Сформировать"):
        sales = get_sales_between(start_dt, end_dt)
        if not sales:
            st.info("Продаж за период нет.")
        else:
            df_prod, df_ing = aggregate_sales(sales)
            prods_map = {p["id"]: p["name"] for p in get_products()}
            ings_map = get_ingredients_map()

            if not df_prod.empty:
                df_prod["product_name"] = df_prod["product_id"].map(lambda x: prods_map.get(x, x))
                st.markdown("**Продажи по позициям**")
                st.dataframe(df_prod[["product_name", "count"]].rename(columns={"product_name": "Позиция", "count": "Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button(
                    "Скачать CSV (позиции)",
                    data=df_prod.to_csv(index=False).encode("utf-8"),
                    file_name=f"sales_by_product_{d_from}_{d_to}.csv",
                    mime="text/csv",
                )

            if not df_ing.empty:
                df_ing["ingredient_name"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("name", x))
                df_ing["unit"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("unit", ""))
                st.markdown("**Суммарные списания ингредиентов**")
                st.dataframe(df_ing[["ingredient_name", "qty", "unit"]].rename(columns={"ingredient_name": "Ингредиент", "qty": "Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button(
                    "Скачать CSV (ингредиенты)",
                    data=df_ing.to_csv(index=False).encode("utf-8"),
                    file_name=f"ingredients_usage_{d_from}_{d_to}.csv",
                    mime="text/csv",
                )

# --- QR-коды для пополнений ингредиентов ---
with tab5:
    st.subheader("📱 QR-коды для пополнений (ингредиенты)")
    st.caption("Сканируешь — открывается страница с готовым пополнением. Нужен базовый URL твоего приложения (ниже).")

    base_url = base_app_url_input()
    ings_map = get_ingredients_map()
    if not base_url:
        st.info("Введи базовый URL приложения сверху (можно сохранить в secrets как BASE_URL).")
    else:
        # выбор ингредиента и пресеты объёмов
        ing_names = sorted([(v["name"], k) for k, v in ings_map.items()], key=lambda x: x[0].lower())
        name_to_id = {n: i for n, i in ing_names}
        col_i, col_p = st.columns([2, 2])
        ing_name = col_i.selectbox("Ингредиент", [n for n, _ in ing_names])
        ing_id = name_to_id[ing_name]
        unit = ings_map.get(ing_id, {}).get("unit", "ml")
        presets = (200, 500, 1000, 2000) if unit == "ml" else (100, 250, 500, 1000)

        st.markdown("**Сгенерируй QR для пресетов:**")
        qr_cols = st.columns(4)
        for idx, q in enumerate(presets):
            url = f"{base_url}?restock={ing_id}&qty={q}"
            # генерим QR
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_cols[idx].image(buf.getvalue(), caption=f"+{q} {unit}", use_column_width=True)
            qr_cols[idx].download_button(
                label=f"Скачать +{q}{unit}",
                data=buf.getvalue(),
                file_name=f"qr_{ing_id}_{q}{unit}.png",
                mime="image/png",
                key=f"dl_qr_{ing_id}_{q}"
            )

        st.markdown("---")
        st.markdown("**Или свой объём:**")
        custom_qty = st.number_input(f"Количество ({unit})", min_value=1.0, step=1.0, value=float(presets[0]))
        url = f"{base_url}?restock={ing_id}&qty={int(custom_qty) if custom_qty.is_integer() else custom_qty}"
        img = qrcode.make(url)
        buf2 = io.BytesIO(); img.save(buf2, format="PNG")
        st.image(buf2.getvalue(), caption=f"QR: +{custom_qty} {unit} для {ing_name}", use_column_width=False)
        st.code(url, language="text")
        st.download_button("Скачать QR (кастом)", data=buf2.getvalue(), file_name=f"qr_{ing_id}_{int(custom_qty)}{unit}.png", mime="image/png")
