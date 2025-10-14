# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore, google-auth creds)
# ВЕРСИЯ: красивый UI, учёт поставок, QR-коды для ингредиентов, быстрый режим по ссылке

import os
import io
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import Mapping

import streamlit as st
import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account

# ============== опционально для QR ==============
try:
    import qrcode
    from PIL import Image
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False
# ================================================

# Нормы склада (для процентов/цветов)
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

def _supplies_ref():
    return db.collection("supplies")  # журнал поставок

# ──────────────────────────────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────────────────────────────
def status_label(percent: float) -> str:
    if percent >= 75: return "🟢 Супер"
    if percent >= 50: return "🟡 Норм"
    if percent >= 25: return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"

def color_for_percent(percent: float) -> str:
    if percent >= 75: return "#22c55e"  # green
    if percent >= 50: return "#eab308"  # yellow
    if percent >= 25: return "#f97316"  # orange
    return "#ef4444"                    # red

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

def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.astimezone()  # локальная -> aware
    return dt_local.astimezone(timezone.utc)

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

# ---- Поставки ----
def record_supply(ingredient_id: str, quantity: float, ts_local: datetime) -> Optional[str]:
    """Создаёт запись в supplies и пополняет остаток."""
    try:
        ts_utc = to_utc(ts_local)
        _supplies_ref().document().set({
            "ingredient_id": ingredient_id,
            "quantity": float(quantity),
            "ts": ts_utc,
        })
        err = adjust_stock(ingredient_id, float(quantity))
        return err
    except Exception as e:
        return str(e)

def get_supplies_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = to_utc(dt_from)
    dt_to_utc = to_utc(dt_to)
    q = (_supplies_ref()
         .where("ts", ">=", dt_from_utc)
         .where("ts", "<", dt_to_utc)
         .order_by("ts"))
    docs = q.stream()
    out: List[Dict[str, Any]] = []
    for d in docs:
        row = d.to_dict() or {}
        row["id"] = d.id
        out.append(row)
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Отчёты
# ──────────────────────────────────────────────────────────────────────────────
def get_sales_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = to_utc(dt_from)
    dt_to_utc = to_utc(dt_to)
    q = (_sales_ref()
         .where("ts", ">=", dt_from_utc)
         .where("ts", "<", dt_to_utc)
         .order_by("ts"))
    docs = q.stream()
    out: List[Dict[str, Any]] = []
    for d in docs:
        row = d.to_dict() or {}
        row["id"] = d.id
        out.append(row)
    return out

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
# UI — базовая настройка + стиль
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.markdown("""
<style>
:root { --brand:#0ea5e9; --bg:#f8fafc; --card:#ffffff; --muted:#475569; }
html, body, [data-testid="stAppViewContainer"] { background: var(--bg); }
.big-card { background: var(--card); border:1px solid rgba(0,0,0,0.08); border-radius:16px; padding:16px; }
.big-btn { display:block; width:100%; font-size:18px; padding:14px 16px; border-radius:12px; border:none; background:var(--brand); color:white; cursor:pointer;}
.big-btn:hover { filter:brightness(1.05);}
.pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#dcfce7; color:#166534; font-weight:600; }
.progress { width:100%; height:12px; background:#e2e8f0; border-radius:999px; overflow:hidden; }
.progress > div { height:100%; }
.hint { color:var(--muted); font-size:13px;}
.card-border-green { border:2px solid #22c55e; background: #e8fbe9; }
.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap:12px; }
</style>
""", unsafe_allow_html=True)

st.title("☕ gipsy-office — учёт списаний")

# ── Быстрый режим по ссылке (?ingredient=milk&quick=supply)
def get_query_params():
    try:
        return st.query_params  # new API
    except Exception:
        return st.experimental_get_query_params()

qp = get_query_params() or {}
quick_ing = (qp.get("ingredient") or [""])[0]
quick_mode = (qp.get("quick") or [""])[0]  # "supply"

if quick_ing:
    with st.sidebar:
        st.subheader("⚡ Быстрый режим")
        st.write(f"Ингредиент: **{quick_ing}**")
        c1, c2, c3 = st.columns(3)
        if c1.button("+50"):  adjust_stock(quick_ing, 50);  st.rerun()
        if c2.button("+100"): adjust_stock(quick_ing, 100); st.rerun()
        if c3.button("+500"): adjust_stock(quick_ing, 500); st.rerun()
        st.write("Добавить поставку (сейчас):")
        qty = st.number_input("Объем", min_value=0.0, step=10.0, key="qs_qty")
        if st.button("➕ Поставка сейчас"):
            err = record_supply(quick_ing, qty, datetime.now())
            st.success("Поставка учтена") if not err else st.error(err)
            st.rerun()

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

# --- Позиции (карточки, подсветка, состав) ---
with tab1:
    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.markdown(f"<span class='pill'>Списано: {last_sale_name}</span>", unsafe_allow_html=True)
        st.write("")

    prods = get_products()
    ing_map = get_ingredients_map()

    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        st.markdown("<div class='grid'>", unsafe_allow_html=True)
        for p in prods:
            recipe = get_recipe(p["id"])
            is_last = (p["id"] == last_sale_id)
            wrapper_class = "big-card card-border-green" if is_last else "big-card"
            st.markdown(f"<div class='{wrapper_class}'>", unsafe_allow_html=True)
            st.markdown(f"**{p['name']}**")
            st.caption(f"Цена: {int(p['price'])} ₽")

            # состав
            if recipe:
                lines = [format_recipe_line(it, ing_map) for it in recipe]
                st.markdown("<div class='hint'>Состав:<br/>" + "<br/>".join(lines) + "</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='hint'>Состав не задан</div>", unsafe_allow_html=True)

            # большая кнопка списания
            if st.button("Списать", key=f"sell_{p['id']}"):
                err = sell_product(p["id"])
                if err: st.error(err)
                else:
                    st.session_state["last_sale_name"] = p["name"]
                    st.session_state["last_sale_id"] = p["id"]
                    st.rerun()

            st.markdown("</div>", unsafe_allow_html=True)
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

# --- Склад (цветные прогресс-бары + глобальная форма поставки) ---
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
                bar_color = color_for_percent(pct)

                st.markdown(f"**{i['name']}** — {pct}% ({int(cur)}/{int(cap)} {i['unit']}) — {status_label(pct)}")
                st.markdown(f"""
                    <div class="progress">
                        <div style="width:{pct}%; background:{bar_color};"></div>
                    </div>
                """, unsafe_allow_html=True)

                # быстрые ± кнопки
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
            st.subheader("➕ Поставка (глобально)")
            ing_map = get_ingredients_map()
            choices = sorted([(v["name"], k) for k, v in ing_map.items()], key=lambda x: x[0].lower())
            name_to_id = {name: _id for name, _id in choices}
            sel_name = st.selectbox("Ингредиент", [n for n, _ in choices])
            sel_id = name_to_id[sel_name]
            qty = st.number_input("Объем поставки", min_value=0.0, step=10.0, value=0.0)
            d = st.date_input("Дата", value=datetime.now().date())
            t = st.time_input("Время", value=datetime.now().time().replace(second=0, microsecond=0))
            dt_local = datetime.combine(d, t)
            if st.button("Учесть поставку"):
                err = record_supply(sel_id, qty, dt_local)
                st.success("Поставка учтена") if not err else st.error(err)
                if not err: st.rerun()

            st.divider()
            st.subheader("📉 Недостачи")
            low25, low50 = [], []
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

# --- Рецепты (редактор + цены) ---
with tab3:
    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Нет продуктов. Добавь документы в `products`.")
    else:
        st.caption("Редактируй состав и цены")
        for p in prods:
            with st.expander(f"{p['name']} — рецепт и цена", expanded=False):
                # цена
                price_col, save_col = st.columns([3,1])
                new_price = price_col.number_input("Цена, ₽", min_value=0.0, step=10.0, value=float(p["price"]), key=f"price_{p['id']}")
                if save_col.button("💾 Сохранить цену", key=f"save_price_{p['id']}"):
                    err = set_product_price(p["id"], new_price)
                    st.success("Цена обновлена") if not err else st.error(err)
                    if not err: st.rerun()

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
                            st.success("Сохранено") if not err else st.error(err)
                            if not err: st.rerun()
                        if cols[3].button("🗑 Удалить", key=f"del_{p['id']}_{idx}"):
                            new_list = [r for i, r in enumerate(cur_recipe) if i != idx]
                            err = set_recipe(p["id"], new_list)
                            st.success("Удалено") if not err else st.error(err)
                            if not err: st.rerun()
                else:
                    st.info("Состав пока не задан.")

                st.markdown("---")
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
                    st.success("Добавлено") if not err else st.error(err)
                    if not err: st.rerun()

# --- Отчёты (продажи + суммарные списания + поставки) ---
with tab4:
    st.subheader("📊 Отчёты")
    today = datetime.now().date()
    col_from, col_to, col_btn = st.columns([3,3,2])
    d_from = col_from.date_input("С", value= today)
    d_to = col_to.date_input("По (включительно)", value= today)
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
                st.download_button("Скачать CSV (позиции)", data=df_prod.to_csv(index=False).encode("utf-8"), file_name=f"sales_by_product_{d_from}_{d_to}.csv", mime="text/csv")

            if not df_ing.empty:
                df_ing["ingredient_name"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("name", x))
                df_ing["unit"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("unit", ""))
                st.markdown("**Суммарные списания ингредиентов**")
                st.dataframe(df_ing[["ingredient_name", "qty", "unit"]].rename(columns={"ingredient_name": "Ингредиент", "qty": "Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button("Скачать CSV (ингредиенты)", data=df_ing.to_csv(index=False).encode("utf-8"), file_name=f"ingredients_usage_{d_from}_{d_to}.csv", mime="text/csv")

        # Поставки
        st.markdown("---")
        st.markdown("**Поставки за период**")
        supplies = get_supplies_between(start_dt, end_dt)
        if not supplies:
            st.info("Поставок за период нет.")
        else:
            ing_map = get_ingredients_map()
            rows = []
            for s in supplies:
                iid = s.get("ingredient_id")
                rows.append({
                    "Дата/время (UTC)": s.get("ts"),
                    "Ингредиент": ing_map.get(iid, {}).get("name", iid),
                    "Кол-во": s.get("quantity"),
                    "Ед.": ing_map.get(iid, {}).get("unit", ""),
                })
            df_sup = pd.DataFrame(rows)
            st.dataframe(df_sup, hide_index=True, use_container_width=True)
            st.download_button("Скачать CSV (поставки)", data=df_sup.to_csv(index=False).encode("utf-8"), file_name=f"supplies_{d_from}_{d_to}.csv", mime="text/csv")

# --- QR-коды (ингредиенты) ---
with tab5:
    st.subheader("🔳 QR-коды для ингредиентов")
    st.caption("Сканируй с телефона; ссылка откроет быстрый режим пополнения для конкретного ингредиента.")
    base_url = st.secrets.get("PUBLIC_APP_URL") or ""  # можно задать в secrets, иначе возьмём текущий URL
    if not base_url:
        st.info("Совет: добавь в secrets строку `PUBLIC_APP_URL = \"https://<твой-deploy>.streamlit.app\"`, чтобы QR ссылался на публичный адрес.")
    ings_map = get_ingredients_map()
    for ing_id, meta in ings_map.items():
        name = meta.get("name", ing_id)
        # соберём ссылку: ?ingredient=<id>&quick=supply
        if base_url:
            url = f"{base_url}?ingredient={ing_id}&quick=supply"
        else:
            # fallback — покажем относительную ссылку
            url = f"?ingredient={ing_id}&quick=supply"

        st.markdown(f"**{name}**  \n{url}")
        if QR_AVAILABLE and base_url:
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=140)
            st.download_button("Скачать QR (PNG)", data=buf.getvalue(), file_name=f"qr_{ing_id}.png", mime="image/png")
        st.write("")
