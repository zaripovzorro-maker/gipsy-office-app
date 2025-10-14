# -*- coding: utf-8 -*-
# gipsy-office — учёт товаров (Streamlit + Firestore, google-auth creds)
import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import Mapping
from urllib.parse import urlencode, quote

import streamlit as st
import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account

# =========================
# 1) Firestore init
# =========================
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
        svc = json.loads(svc_raw.strip())
    else:
        st.error(f"❌ FIREBASE_SERVICE_ACCOUNT должен быть mapping или JSON-строкой, получено: {type(svc_raw).__name__}")
        st.stop()

    creds = service_account.Credentials.from_service_account_info(svc)
    return firestore.Client(project=project_id, credentials=creds)

db = init_firestore()

# =========================
# 2) Collections & helpers
# =========================
DEFAULT_CAPACITY: Dict[str, float] = {"beans": 2000.0, "milk": 5000.0}
def _ingredients_ref(): return db.collection("ingredients")
def _products_ref():    return db.collection("products")
def _recipes_ref():     return db.collection("recipes")
def _sales_ref():       return db.collection("sales")
def _supplies_ref():    return db.collection("supplies")

def status_label(percent: float) -> str:
    if percent >= 75: return "🟢 Супер"
    if percent >= 50: return "🟡 Норм"
    if percent >= 25: return "🟠 Готовиться к закупке"
    return "🔴 Срочно докупить"

def percent(cur: float, cap: float) -> int:
    cap = cap or 1
    return int(round(100 * cur / cap))

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
    return sorted(items, key=lambda x: x["name"].lower())

def get_ingredients_map() -> Dict[str, Dict[str, Any]]:
    m = {}
    for i in get_ingredients():
        m[i["id"]] = {
            "name": i["name"], "unit": i["unit"],
            "capacity": i["capacity"], "stock_quantity": i["stock_quantity"]
        }
    return m

def get_products() -> List[Dict[str, Any]]:
    docs = _products_ref().stream()
    items: List[Dict[str, Any]] = []
    for d in docs:
        if d.id.lower() in {"capacity", "_meta", "_settings"}:  # игнор служебных
            continue
        data = d.to_dict() or {}
        items.append({"id": d.id, "name": data.get("name", d.id), "price": float(data.get("price", 0))})
    return sorted(items, key=lambda x: x["name"].lower())

def get_recipe(product_id: str) -> List[Dict[str, Any]]:
    doc = _recipes_ref().document(product_id).get()
    if not doc.exists:
        return []
    return list((doc.to_dict() or {}).get("items", []))

def set_recipe(product_id: str, items: List[Dict[str, Any]]) -> Optional[str]:
    try:
        _recipes_ref().document(product_id).set({"items": items})
        return None
    except Exception as e:
        return str(e)

def set_product_price(product_id: str, new_price: float) -> Optional[str]:
    try: _products_ref().document(product_id).set({"price": float(new_price)}, merge=True); return None
    except Exception as e: return str(e)

def adjust_stock(ingredient_id: str, delta: float) -> Optional[str]:
    try:
        ref = _ingredients_ref().document(ingredient_id)
        snap = ref.get()
        cur = float((snap.to_dict() or {}).get("stock_quantity", 0))
        new_val = cur + delta
        if new_val < 0: return "❌ Нельзя увести остаток в минус."
        ref.update({"stock_quantity": new_val})
        return None
    except Exception as e:
        return str(e)

def add_supply(ingredient_id: str, quantity: float) -> Optional[str]:
    """Фиксируем поставку: увеличиваем остаток + пишем в supplies."""
    try:
        if quantity <= 0:
            return "Укажи положительный объём поставки."
        err = adjust_stock(ingredient_id, quantity)
        if err: return err
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
        if not recipe: return "Нет рецепта для этой позиции."
        for it in recipe:
            err = adjust_stock(it["ingredientId"], -float(it["qtyPer"]))
            if err: return err
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
        if not last: return "Нет продаж для отмены."
        sale = last.to_dict() or {}
        for it in sale.get("items", []):
            adjust_stock(it["ingredientId"], float(it["qtyPer"]))
        last.reference.delete()
        return None
    except Exception as e:
        return str(e)

def get_sales_between(dt_from: datetime, dt_to: datetime) -> List[Dict[str, Any]]:
    dt_from_utc = dt_from.astimezone(timezone.utc)
    dt_to_utc   = dt_to.astimezone(timezone.utc)
    q = (_sales_ref().where("ts", ">=", dt_from_utc).where("ts", "<", dt_to_utc).order_by("ts"))
    docs = q.stream()
    out = []
    for d in docs:
        row = d.to_dict() or {}
        row["id"] = d.id
        out.append(row)
    return out

def aggregate_sales(sales: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    by_product, by_ingredient = {}, {}
    for s in sales:
        pid = s.get("product_id", "unknown")
        by_product[pid] = by_product.get(pid, 0) + 1
        for it in s.get("items", []):
            ing = it.get("ingredientId"); qty = float(it.get("qtyPer", 0))
            by_ingredient[ing] = by_ingredient.get(ing, 0.0) + qty
    df_prod = pd.DataFrame([{"product_id": k, "count": v} for k, v in by_product.items()]).sort_values("count", ascending=False) if by_product else pd.DataFrame(columns=["product_id","count"])
    df_ing  = pd.DataFrame([{"ingredient_id": k, "qty": v} for k, v in by_ingredient.items()]).sort_values("qty", ascending=False) if by_ingredient else pd.DataFrame(columns=["ingredient_id","qty"])
    return df_prod, df_ing

# =========================
# 3) Styling (light theme)
# =========================
st.set_page_config(page_title="gipsy-office — учёт", page_icon="☕", layout="wide")
st.markdown("""
<style>
:root {
  --card-bg:#ffffff; --card-border:#e5e7eb; --muted:#6b7280;
  --accent:#22c55e; --accent-soft: rgba(34,197,94,.08);
  --warn:#f59e0b; --danger:#ef4444;
}
body { background:#f8fafc; }
.big-btn button[kind="secondary"] {
  width:100%; padding:16px 18px; font-size:18px; border-radius:12px;
}
.card {
  background:var(--card-bg); border:1px solid var(--card-border);
  border-radius:14px; padding:14px; margin-bottom:12px;
}
.card.highlight { border:2px solid var(--accent); background:var(--accent-soft); }
.price { color:#111827; font-weight:600; }
.caption { color:var(--muted); white-space:pre-line; }
.progress {
  width:100%; height:14px; border-radius: 999px; background:#e5e7eb; overflow:hidden;
}
.progress > div { height:100%; }
.progress.green  > div { background:#22c55e; }
.progress.yellow > div { background:#f59e0b; }
.progress.red    > div { background:#ef4444; }
.badge { display:inline-block; padding:4px 8px; border-radius:999px; background:#ecfeff; color:#0e7490; font-size:12px; }
.qr-card { display:flex; gap:16px; align-items:center; }
.qr-card img { width:120px; height:120px; border:1px solid #e5e7eb; border-radius:8px; }
</style>
""", unsafe_allow_html=True)

st.title("☕ gipsy-office — учёт списаний")

# =========================
# 4) Deep-link (QR → supply mode)
# =========================
# Streamlit 1.50: st.query_params; на старых версиях можно st.experimental_get_query_params()
try:
    qp = st.query_params  # type: ignore[attr-defined]
except Exception:
    qp = st.experimental_get_query_params()

deeplink_mode = (qp.get("mode",[None])[0] if isinstance(qp, dict) else None)
deeplink_ingredient = (qp.get("ingredient",[None])[0] if isinstance(qp, dict) else None)

if deeplink_mode == "supply" and deeplink_ingredient:
    st.info(f"🔗 Режим поставки по QR: выбран ингредиент **{deeplink_ingredient}**. Вкладка «Склад» → форма поставки уже заполнена.", icon="🔗")

# =========================
# 5) First-run helper
# =========================
with st.expander("⚙️ Первая настройка / создать тестовые данные"):
    if st.button("Создать тестовые данные"):
        try:
            _ingredients_ref().document("beans").set({"name":"Зёрна","stock_quantity":2000,"unit":"g","capacity":2000})
            _ingredients_ref().document("milk").set({"name":"Молоко","stock_quantity":5000,"unit":"ml","capacity":5000})
            _products_ref().document("cappuccino").set({"name":"Капучино","price":250})
            _products_ref().document("espresso").set({"name":"Эспрессо","price":150})
            _recipes_ref().document("cappuccino").set({"items":[{"ingredientId":"beans","qtyPer":18},{"ingredientId":"milk","qtyPer":180}]})
            _recipes_ref().document("espresso").set({"items":[{"ingredientId":"beans","qtyPer":18}]})
            st.success("✅ Стартовые данные созданы. Обнови страницу.")
        except Exception as e:
            st.error(f"Ошибка создания: {e}")

# =========================
# 6) Tabs
# =========================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["Позиции", "Склад", "Рецепты", "Отчёты", "QR-коды"])

# -------------------------
# TAB 1 — Позиции (крупные карточки, ярко)
# -------------------------
with tab1:
    # режим отображения: карточки / плитки
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
                        st.write(f"**{p['name']}** " + ("<span class='badge'>только что</span>" if is_last else ""), unsafe_allow_html=True)
                        st.write(f"<span class='price'>{int(p['price'])} ₽</span>", unsafe_allow_html=True)

                        # состав
                        if recipe:
                            lines = []
                            for it in recipe:
                                ing = ing_map.get(it["ingredientId"], {"name": it["ingredientId"], "unit": ""})
                                qty = float(it.get("qtyPer", 0))
                                qty_text = str(int(qty)) if qty.is_integer() else f"{qty}"
                                lines.append(f"• {ing['name']}: {qty_text} {ing['unit']}")
                            st.write("<div class='caption'>" + "\n".join(lines) + "</div>", unsafe_allow_html=True)
                        else:
                            st.write("<div class='caption'>Состав не задан</div>", unsafe_allow_html=True)

                        # растягиваем вверх, кнопку — вниз
                        st.markdown('<div class="grow"></div>', unsafe_allow_html=True)
                        st.markdown('<div class="big-btn">', unsafe_allow_html=True)
                        if st.button("Списать", key=f"sell_{p['id']}", use_container_width=True):
                            err = sell_product(p["id"])
                            if err: st.error(err)
                            else:
                                st.session_state["last_sale_name"] = p["name"]
                                st.session_state["last_sale_id"] = p["id"]
                                st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)
                        st.markdown('</div>', unsafe_allow_html=True)

        else:  # ===== ПЛИТКИ =====
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

# -------------------------
# TAB 2 — Склад (цветные полосы + Поставка)
# -------------------------
with tab2:
    ings = get_ingredients()
    if not ings:
        st.info("Нет ингредиентов. Создай тестовые данные выше.")
    else:
        left, right = st.columns([2, 1])

        # форма поставки сверху (учитывает deep-link)
        with right:
            st.subheader("➕ Поставка (пополнение)")
            ing_choices = {i["name"]: i["id"] for i in ings}
            default_name = None
            if deeplink_mode == "supply" and deeplink_ingredient:
                # попробуем сопоставить id → name
                for nm, _id in ing_choices.items():
                    if _id == deeplink_ingredient:
                        default_name = nm; break
            sel_name = st.selectbox("Ингредиент", list(ing_choices.keys()), index=(list(ing_choices.keys()).index(default_name) if default_name in ing_choices else 0))
            sel_id = ing_choices[sel_name]
            qty = st.number_input("Объём поставки", min_value=0.0, step=50.0, value=0.0)
            if st.button("Добавить поставку"):
                err = add_supply(sel_id, float(qty))
                if err: st.error(err)
                else: st.success("Поставка учтена"); st.rerun()

        with left:
            st.subheader("📦 Склад")
            for i in ings:
                cur = i["stock_quantity"]; cap = i["capacity"] or DEFAULT_CAPACITY.get(i["id"], 1)
                pct = percent(cur, cap)
                # цвет прогресс-бара
                cls = "green" if pct>=75 else "yellow" if pct>=50 else "red" if pct<25 else "yellow"
                st.markdown(f"**{i['name']}** — {pct}% ({int(cur)} / {int(cap)} {i['unit']}) — {status_label(pct)}")
                st.markdown(f"""
                    <div class="progress {cls}">
                      <div style="width:{pct}%;"></div>
                    </div>
                """, unsafe_allow_html=True)
                # быстрые шаги
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

# -------------------------
# TAB 3 — Рецепты (редактор + цена + дубликатор)
# -------------------------
with tab3:
    prods = get_products(); ing_map = get_ingredients_map()
    if not prods:
        st.info("Нет продуктов. Добавь документы в `products`.")
    else:
        st.caption("Редактируй состав, цены и дублируй рецепты между продуктами.")
        # Дубликатор
        names = [p["name"] for p in prods]; id_by_name = {p["name"]: p["id"] for p in prods}
        col_a, col_b, col_btn = st.columns([4,4,2])
        src_name = col_a.selectbox("Источник", names, key="dup_src")
        dst_name = col_b.selectbox("Цель", [n for n in names if n != src_name], key="dup_dst")
        if col_btn.button("Копировать состав"):
            err = set_recipe(id_by_name[src_name], get_recipe(id_by_name[src_name]))
            if err: st.error(err)
            else:
                items = get_recipe(id_by_name[src_name])
                err2 = set_recipe(id_by_name[dst_name], items)
                st.success(f"Состав {src_name} → {dst_name} скопирован.") if not err2 else st.error(err2)

        st.divider()

        for p in prods:
            with st.expander(f"{p['name']} — рецепт и цена", expanded=False):
                price_col, save_col = st.columns([3,1])
                new_price = price_col.number_input("Цена, ₽", min_value=0.0, step=10.0, value=float(p["price"]), key=f"price_{p['id']}")
                if save_col.button("💾 Сохранить цену", key=f"save_price_{p['id']}"):
                    err = set_product_price(p["id"], new_price)
                    st.success("Цена обновлена") if not err else st.error(err)

                cur_recipe = get_recipe(p["id"])
                st.markdown("**Текущий состав:**")
                if cur_recipe:
                    for idx, it in enumerate(cur_recipe):
                        ing_id = it.get("ingredientId"); qty = float(it.get("qtyPer", 0))
                        meta = ing_map.get(ing_id, {"name": ing_id, "unit": ""})
                        cols = st.columns([5, 3, 2, 2])
                        cols[0].write(meta["name"])
                        new_qty = cols[1].number_input("qty", key=f"qty_{p['id']}_{idx}", value=qty, step=1.0)
                        if cols[2].button("💾 Сохранить", key=f"save_{p['id']}_{idx}"):
                            cur_recipe[idx]["qtyPer"] = float(new_qty)
                            err = set_recipe(p["id"], cur_recipe)
                            st.success("Сохранено") if not err else st.error(err)
                        if cols[3].button("🗑 Удалить", key=f"del_{p['id']}_{idx}"):
                            new_list = [r for i, r in enumerate(cur_recipe) if i != idx]
                            err = set_recipe(p["id"], new_list)
                            st.success("Удалено") if not err else st.error(err)
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
                            item["qtyPer"] = float(add_qty); break
                    else:
                        new_items.append({"ingredientId": add_id, "qtyPer": float(add_qty)})
                    err = set_recipe(p["id"], new_items)
                    st.success("Добавлено") if not err else st.error(err)

# -------------------------
# TAB 4 — Отчёты
# -------------------------
with tab4:
    st.subheader("📊 Отчёты по продажам")
    today = datetime.now().date()
    col_from, col_to, col_btn = st.columns([3,3,2])
    d_from = col_from.date_input("С", value=today)
    d_to   = col_to.date_input("По (включительно)", value=today)
    start_dt = datetime.combine(d_from, datetime.min.time()).astimezone()
    end_dt   = datetime.combine(d_to, datetime.min.time()).astimezone() + timedelta(days=1)
    if col_btn.button("Сформировать"):
        sales = get_sales_between(start_dt, end_dt)
        if not sales: st.info("Продаж за период нет.")
        else:
            df_prod, df_ing = aggregate_sales(sales)
            prods_map = {p["id"]: p["name"] for p in get_products()}
            ings_map  = get_ingredients_map()

            if not df_prod.empty:
                df_prod["product_name"] = df_prod["product_id"].map(lambda x: prods_map.get(x, x))
                st.markdown("**Продажи по позициям**")
                st.dataframe(df_prod[["product_name","count"]].rename(columns={"product_name":"Позиция","count":"Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button("Скачать CSV (позиции)", data=df_prod.to_csv(index=False).encode("utf-8"), file_name=f"sales_by_product_{d_from}_{d_to}.csv", mime="text/csv")
            if not df_ing.empty:
                df_ing["ingredient_name"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("name", x))
                df_ing["unit"] = df_ing["ingredient_id"].map(lambda x: ings_map.get(x, {}).get("unit", ""))
                st.markdown("**Суммарные списания ингредиентов**")
                st.dataframe(df_ing[["ingredient_name","qty","unit"]].rename(columns={"ingredient_name":"Ингредиент","qty":"Кол-во"}), hide_index=True, use_container_width=True)
                st.download_button("Скачать CSV (ингредиенты)", data=df_ing.to_csv(index=False).encode("utf-8"), file_name=f"ingredients_usage_{d_from}_{d_to}.csv", mime="text/csv")

# -------------------------
# TAB 5 — QR-коды (для ингредиентов → режим поставки)
# -------------------------
with tab5:
    st.subheader("📱 QR-коды для пополнения склада")
    st.caption("Сканируй QR → открывается приложение в режиме «Поставка» с выбранным ингредиентом.")

    # Базовый адрес приложения: можно положить в secrets как APP_BASE_URL, либо ввести тут
    base_url_secret = st.secrets.get("APP_BASE_URL", "")
    base_url = st.text_input("Базовый URL приложения", value=base_url_secret or st.session_state.get("base_url",""))
    st.session_state["base_url"] = base_url

    if not base_url:
        st.warning("Укажи базовый URL (например: https://gipsy-office-app.streamlit.app). Тогда сгенерируются QR-коды.")
    else:
        ings = get_ingredients()
        for i in ings:
            ing_id = i["id"]; ing_name = i["name"]
            params = {"mode":"supply","ingredient":ing_id}
            target = f"{base_url}/?{urlencode(params)}"
            # Генерируем QR без дополнительных библиотек (через Google Chart API)
            qr_url = f"https://chart.googleapis.com/chart?cht=qr&chs=300x300&chl={quote(target)}&chld=L|0"
            with st.container():
                st.markdown('<div class="card qr-card">', unsafe_allow_html=True)
                st.image(qr_url, caption=None, use_column_width=False)
                st.write(f"**{ing_name}**  \n{target}")
                st.markdown('</div>', unsafe_allow_html=True)
