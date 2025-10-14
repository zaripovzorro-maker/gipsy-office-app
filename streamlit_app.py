# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore, google-auth creds + цветной UI + поставки)

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import Mapping

import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account
import pandas as pd

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

    if not project_id:
        st.error('❌ В secrets нет PROJECT_ID. Добавь строку: PROJECT_ID = "gipsy-office"')
        st.stop()
    if svc_raw is None:
        st.error("❌ В secrets нет FIREBASE_SERVICE_ACCOUNT (таблица TOML или JSON-строка).")
        st.stop()

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

    try:
        creds = service_account.Credentials.from_service_account_info(svc)
        db = firestore.Client(project=project_id, credentials=creds)
        return db
    except Exception as e:
        st.error(f"❌ Не удалось создать Firestore client: {e}")
        st.info("Проверь формат секрета: [FIREBASE_SERVICE_ACCOUNT] с многострочным private_key и PROJECT_ID снаружи.")
        st.stop()

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

def _deliveries_ref():
    return db.collection("deliveries")

# ──────────────────────────────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────────────────────────────
def status_label(percent: float) -> str:
    if percent >= 75: return "🟢 Супер"
    if percent >= 50: return "🟡 Норм"
    if percent >= 25: return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"

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

# ───────────── Поставки ─────────────
def register_delivery(ingredient_id: str, qty: float, supplier: str, note: str) -> Optional[str]:
    """Записывает поставку и увеличивает остаток."""
    try:
        now = firestore.SERVER_TIMESTAMP
        _deliveries_ref().document().set({
            "ingredientId": ingredient_id,
            "qty": float(qty),
            "supplier": supplier.strip(),
            "note": note.strip(),
            "ts": now,
        })
        return adjust_stock(ingredient_id, float(qty))
    except Exception as e:
        return str(e)

def get_deliveries_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = dt_from.astimezone(timezone.utc)
    dt_to_utc = dt_to.astimezone(timezone.utc)
    q = (_deliveries_ref()
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
# Отчёты (продажи)
# ──────────────────────────────────────────────────────────────────────────────
def get_sales_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = dt_from.astimezone(timezone.utc)
    dt_to_utc = dt_to.astimezone(timezone.utc)
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
# UI — Настройки страницы + стили
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")

# Цветной «тёплый» UI: крупные кнопки, карточки, сетка
st.markdown("""
<style>
:root {
  --go-primary: #6C47FF;   /* фиолетовый акцент */
  --go-green:  #22c55e;
  --go-amber:  #f59e0b;
  --go-red:    #ef4444;
  --go-bg:     #0b0b0c;
  --go-card:   #151518;
  --go-border: rgba(255,255,255,0.08);
  --go-text:   #f2f2f3;
  --go-sub:    #b7b7c0;
}
html, body, [data-testid="stAppViewContainer"] { background: var(--go-bg) !important; color: var(--go-text) !important;}
h1,h2,h3,h4 { color: var(--go-text) !important; }
hr { border-color: var(--go-border) !important; }

.stButton>button {
  width: 100%;
  border-radius: 14px;
  padding: 14px 16px;
  font-weight: 700;
  border: 1px solid var(--go-border);
  background: linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
  color: var(--go-text);
}
.stButton>button:hover { border-color: rgba(255,255,255,0.18); }

.go-card {
  border: 1px solid var(--go-border);
  border-radius: 16px;
  padding: 12px 14px;
  background: var(--go-card);
  margin-bottom: 10px;
}

.go-pill {
  display:inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  border:1px solid var(--go-border);
  color: var(--go-sub);
}

.go-grid {
  display:grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
@media (max-width: 900px) {
  .go-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 600px) {
  .go-grid { grid-template-columns: 1fr; }
}

/* Кнопки позиций — большие цветные */
.go-tile button {
  height: 90px;
  font-size: 20px;
  background: linear-gradient(180deg, rgba(108,71,255,0.25), rgba(108,71,255,0.08)) !important;
  border: 1px solid rgba(108,71,255,0.45) !important;
}
.go-tile .sub { font-size: 12px; opacity: .85}

.go-tile.active button {
  background: linear-gradient(180deg, rgba(34,197,94,0.28), rgba(34,197,94,0.08)) !important;
  border: 1px solid rgba(34,197,94,0.55) !important;
}

/* Таблицы */
[data-testid="stDataFrame"] { background: var(--go-card) !important; border-radius: 12px; }
/* Инпуты */
input, textarea, select { background: #121214 !important; color: var(--go-text) !important; border-radius: 8px !important; border:1px solid var(--go-border) !important;}
</style>
""", unsafe_allow_html=True)

st.title("☕ gipsy-office — учёт списаний")

# ──────────────────────────────────────────────────────────────────────────────
# Первая настройка (демо-данные)
# ──────────────────────────────────────────────────────────────────────────────
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
tab1, tab2, tab3, tab4, tab5 = st.tabs(["Позиции", "Склад", "Рецепты", "Отчёты", "Поставки"])

# ──────────────────────────────────────────────────────────────────────────────
# Позиции (быстрые большие плитки + состав + подсветка последнего клика)
# ──────────────────────────────────────────────────────────────────────────────
with tab1:
    last_sale_name = st.session_state.get("last_sale_name")
    last_sale_id = st.session_state.get("last_sale_id")
    if last_sale_name:
        st.markdown(f'<span class="go-pill">Списано: {last_sale_name}</span>', unsafe_allow_html=True)
        st.write("")

    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Добавь продукты в Firestore.")
    else:
        # сетка плиток
        st.markdown('<div class="go-grid">', unsafe_allow_html=True)
        for p in prods:
            recipe = get_recipe(p["id"])
            is_last = (p["id"] == last_sale_id)
            tile_class = "go-tile active" if is_last else "go-tile"
            st.markdown(f'<div class="go-card {tile_class}">', unsafe_allow_html=True)
            # Крупная кнопка
            col_btn = st.columns(1)[0]
            if col_btn.button(f"{p['name']} — {int(p['price'])} ₽", key=f"sell_tile_{p['id']}"):
                err = sell_product(p["id"])
                if err:
                    st.error(err)
                else:
                    st.session_state["last_sale_name"] = p["name"]
                    st.session_state["last_sale_id"] = p["id"]
                    st.rerun()
            # Состав мелким шрифтом
            if recipe:
                lines = [format_recipe_line(it, ing_map) for it in recipe]
                st.markdown(f'<div class="sub" style="margin-top:6px; color: var(--go-sub)">{ "<br/>".join(lines) }</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="sub" style="margin-top:6px; color: var(--go-sub)">Состав не задан</div>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        st.write("")
        if st.button("↩️ Undo последней продажи"):
            err = undo_last_sale()
            if err:
                st.error(err)
            else:
                st.success("✅ Откат выполнен.")
                st.session_state["last_sale_name"] = None
                st.session_state["last_sale_id"] = None
                st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# Склад
# ──────────────────────────────────────────────────────────────────────────────
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
                st.markdown(f"""
                <div class="go-card">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                      <div><b>{i['name']}</b> — {pct}% ({int(cur)} / {int(cap)} {i['unit']})</div>
                      <span class="go-pill">{status_label(pct)}</span>
                    </div>
                """, unsafe_allow_html=True)
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
                st.markdown("</div>", unsafe_allow_html=True)
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

# ──────────────────────────────────────────────────────────────────────────────
# Рецепты (редактор + цена + дубликатор)
# ──────────────────────────────────────────────────────────────────────────────
with tab3:
    prods = get_products()
    ing_map = get_ingredients_map()
    if not prods:
        st.info("Нет продуктов. Добавь документы в `products`.")
    else:
        st.caption("Редактируй состав напитков, цены и дублируй рецепты между продуктами.")

        # Дубликатор рецептов
        st.subheader("🧬 Дублировать рецепт")
        names = [p["name"] for p in prods]
        id_by_name = {p["name"]: p["id"] for p in prods}
        col_a, col_b, col_btn = st.columns([4,4,2])
        src_name = col_a.selectbox("Источник", names, key="dup_src")
        dst_name = col_b.selectbox("Цель", [n for n in names if n != src_name], key="dup_dst")
        if col_btn.button("Копировать состав"):
            src_id = id_by_name[src_name]
            dst_id = id_by_name[dst_name]
            items = get_recipe(src_id)
            err = set_recipe(dst_id, items)
            if err: st.error(err)
            else: st.success(f"Состав {src_name} → {dst_name} скопирован."); st.rerun()

        st.divider()

        # Редактор по каждому продукту
        for p in prods:
            with st.expander(f"{p['name']} — рецепт и цена", expanded=False):
                # цена
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

                # Добавить новую строку
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

# ──────────────────────────────────────────────────────────────────────────────
# Отчёты
# ──────────────────────────────────────────────────────────────────────────────
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
                st.markdown('<div class="go-card"><b>Продажи по позициям</b></div>', unsafe_allow_html=True)
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
                st.markdown('<div class="go-card"><b>Суммарные списания ингредиентов</b></div>', unsafe_allow_html=True)
                st.dataframe(df_ing[["ingredient_name", "qty", "unit"]].rename(columns={"ingredient_name": "Ингредиент", "qty": "Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button(
                    "Скачать CSV (ингредиенты)",
                    data=df_ing.to_csv(index=False).encode("utf-8"),
                    file_name=f"ingredients_usage_{d_from}_{d_to}.csv",
                    mime="text/csv",
                )

# ──────────────────────────────────────────────────────────────────────────────
# Поставки (приход на склад + журнал)
# ──────────────────────────────────────────────────────────────────────────────
with tab5:
    st.subheader("📦 Поставки (приход)")

    ing_map = get_ingredients_map()
    ing_choices = sorted([(v["name"], k) for k, v in ing_map.items()], key=lambda x: x[0].lower())
    name_to_id = {name: _id for name, _id in ing_choices}

    c1, c2, c3, c4 = st.columns([3,2,3,2])
    sel_name = c1.selectbox("Ингредиент", [n for n, _ in ing_choices], key="dlv_sel")
    sel_id = name_to_id.get(sel_name)
    unit = ing_map.get(sel_id, {}).get("unit", "")
    qty = c2.number_input(f"Количество ({unit})", min_value=0.0, step=10.0, key="dlv_qty")
    supplier = c3.text_input("Поставщик / чек №", "")
    note = c4.text_input("Комментарий", "")

    col_ok, col_fast = st.columns([2,3])
    if col_ok.button("✅ Принять поставку"):
        if not sel_id or qty <= 0:
            st.error("Выбери ингредиент и укажи количество > 0.")
        else:
            err = register_delivery(sel_id, float(qty), supplier, note)
            if err: st.error(err)
            else: st.success("Поставка записана, склад пополнен."); st.rerun()

    # Быстрые кнопки (шаблоны)
    with col_fast:
        st.caption("Быстро:")
        f1, f2, f3, f4 = st.columns(4)
        if f1.button("+1000 g Зёрна"): register_delivery("beans", 1000, "—", "шаблон"); st.rerun()
        if f2.button("+2000 g Зёрна"): register_delivery("beans", 2000, "—", "шаблон"); st.rerun()
        if f3.button("+1000 ml Молоко"): register_delivery("milk", 1000, "—", "шаблон"); st.rerun()
        if f4.button("+2000 ml Молоко"): register_delivery("milk", 2000, "—", "шаблон"); st.rerun()

    st.markdown("---")
    st.subheader("📒 Журнал поставок")
    today = datetime.now().date()
    col_from, col_to, col_btn = st.columns([3,3,2])
    d_from = col_from.date_input("С", value=today - timedelta(days=7))
    d_to = col_to.date_input("По (включительно)", value=today)
    start_dt = datetime.combine(d_from, datetime.min.time()).astimezone()
    end_dt = datetime.combine(d_to, datetime.min.time()).astimezone() + timedelta(days=1)

    if col_btn.button("Показать"):
        rows = get_deliveries_between(start_dt, end_dt)
        if not rows:
            st.info("За период поставок нет.")
        else:
            # Преобразуем для показа
            show = []
            for r in rows:
                name = ing_map.get(r.get("ingredientId"), {}).get("name", r.get("ingredientId"))
                unit = ing_map.get(r.get("ingredientId"), {}).get("unit", "")
                qty = r.get("qty", 0)
                supplier = r.get("supplier", "")
                note = r.get("note", "")
                ts = r.get("ts")
                # ts может быть Timestamp — приведём к локальному времени
                if hasattr(ts, "to_datetime"):
                    ts = ts.to_datetime().astimezone()
                show.append({
                    "Дата/время": ts,
                    "Ингредиент": name,
                    "Кол-во": qty,
                    "Ед.": unit,
                    "Поставщик": supplier,
                    "Комментарий": note,
                })
            df = pd.DataFrame(show).sort_values("Дата/время", ascending=False)
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.download_button(
                "Скачать CSV (поставки)",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"deliveries_{d_from}_{d_to}.csv",
                mime="text/csv",
            )
