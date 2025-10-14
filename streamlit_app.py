# streamlit_app.py
# gipsy office — продажи с выбором объёма напитков и корзиной

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import streamlit as st

# --- Firebase Admin / Firestore ---
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore  # type: ignore


# =========================
# Firestore + Secrets init
# =========================

def _read_firebase_service_account() -> Dict:
    """
    Берём сервисный ключ из st.secrets. Поддерживаем два варианта:

    1) TOML-таблица:
        [FIREBASE_SERVICE_ACCOUNT]
        type = "service_account"
        project_id = "gipsy-office"
        ...

    2) JSON-строка, целиком:
        FIREBASE_SERVICE_ACCOUNT = "{\"type\":\"service_account\", ... \"private_key\":\"-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n\"}"

    Возвращаем питоновский dict, корректируя перевод строки в private_key.
    """
    if "FIREBASE_SERVICE_ACCOUNT" not in st.secrets:
        raise RuntimeError("В Secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")

    svc = st.secrets["FIREBASE_SERVICE_ACCOUNT"]

    if isinstance(svc, str):
        # JSON-строка
        data = json.loads(svc)
    elif isinstance(svc, dict):
        # TOML-таблица
        data = dict(svc)
    else:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML.")

    # Нормализуем private_key: в JSON-строке внутри должны быть \n, в TOML — реальная новая строка.
    pk = data.get("private_key", "")
    # Если в ключе нет переноса строки, но есть секции BEGIN/END — добавим переводы
    if "\\n" in pk and "-----BEGIN" in pk:
        data["private_key"] = pk.replace("\\n", "\n")
    elif "-----BEGIN" in pk and "\n" not in pk.strip():
        # Редкий случай «одной строкой», стараемся починить
        data["private_key"] = pk.replace("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\n") \
                                .replace("-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----\n")

    return data


def init_firestore() -> firestore.Client:
    # PROJECT_ID (для клиента Firestore)
    project_id = st.secrets.get("PROJECT_ID") or st.secrets.get("PROJECT") or st.secrets.get("project_id")
    if not project_id:
        raise RuntimeError("В Secrets нет PROJECT_ID (или PROJECT / project_id).")

    data = _read_firebase_service_account()

    # Инициализируем firebase_admin один раз
    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)
    # Клиент Firestore
    return firestore.Client(project=project_id)


# =========================
# Модель корзины
# =========================

@dataclass
class CartItem:
    product_id: str
    product_name: str
    size_id: str
    size_name: str
    volume: Optional[float]
    price: float
    qty: int = 1


def cart_get() -> List[CartItem]:
    if "cart" not in st.session_state:
        st.session_state.cart = []
    return st.session_state.cart


def cart_add(item: CartItem) -> None:
    cart = cart_get()
    # Склеиваем одинаковые позиции (один и тот же продукт + размер)
    for it in cart:
        if it.product_id == item.product_id and it.size_id == item.size_id:
            it.qty += item.qty
            break
    else:
        cart.append(item)


def cart_clear() -> None:
    st.session_state.cart = []


def cart_total() -> float:
    return sum(i.price * i.qty for i in cart_get())


# =========================
# Firestore helpers
# =========================

def get_categories(db: firestore.Client) -> List[str]:
    # Берём категории из активных продуктов
    docs = db.collection("products").where("active", "==", True).stream()
    cats = { (d.to_dict().get("category") or "Прочее") for d in docs }
    return sorted(c for c in cats if c)


def get_products_by_category(db: firestore.Client, category: str) -> List[Tuple[str, Dict]]:
    # Возвращаем список (id, data)
    q = db.collection("products").where("active", "==", True)
    if category:
        q = q.where("category", "==", category)
    return [(d.id, d.to_dict()) for d in q.stream()]


def get_sizes_for_product(db: firestore.Client, product_id: str) -> List[Tuple[str, Dict]]:
    # Подколлекция sizes внутри products/{product_id}
    coll = db.collection("products").document(product_id).collection("sizes")
    sizes = [(d.id, d.to_dict()) for d in coll.stream()]
    # Если размеров нет — вернём один «универсальный» размер из самого продукта (price / volume)
    if not sizes:
        prod = db.collection("products").document(product_id).get()
        p = prod.to_dict() or {}
        sizes = [(
            "default",
            {
                "name": p.get("size_name") or "Стандарт",
                "price": p.get("price", 0),
                "volume": p.get("volume"),
                # Можно положить сюда size.recipe, если нужно
            },
        )]
    # Сортируем по price (если есть), иначе по имени
    return sorted(sizes, key=lambda x: (x[1].get("price", 0), x[1].get("name", "")))


def get_recipe_for_product_size(
    db: firestore.Client, product_id: str, size_id: str, size_payload: Dict
) -> Dict[str, float]:
    """
    Рецепт ищем так:
    1) Если в документе размера есть ключ "recipe" (dict ingredient_id -> float), используем его.
    2) Иначе берём базовый рецепт из коллекции recipes/{product_id}:
       формат: {"items": [{"ingredient":"beans","amount":18,"unit":"g"}, ...]}
    """
    if "recipe" in size_payload and isinstance(size_payload["recipe"], dict):
        # Прямой рецепт в размере
        return {str(k): float(v) for k, v in size_payload["recipe"].items()}

    rd = db.collection("recipes").document(product_id).get()
    if not rd.exists:
        return {}  # без рецепта просто ничего не спишем
    data = rd.to_dict() or {}
    items = data.get("items") or []
    # items: [{ingredient, amount, unit}]
    result: Dict[str, float] = {}
    for it in items:
        ing = it.get("ingredient")
        amt = it.get("amount")
        try:
            if ing and amt is not None:
                result[str(ing)] = float(amt)
        except Exception:
            pass
    return result


def adjust_stocks_transaction(
    db: firestore.Client,
    sale_items: List[CartItem],
) -> None:
    """
    Пишем документ в sales и списываем ингредиенты в транзакции.
    sale_items — окончательная корзина.
    """
    def _tx(transaction: firestore.Transaction):
        # Суммируем по ингредиентам, что надо списать
        to_decrease: Dict[str, float] = {}

        for item in sale_items:
            # Загружаем размер, чтобы достать его рецепт (или базовый)
            size_doc = (
                db.collection("products")
                .document(item.product_id)
                .collection("sizes")
                .document(item.size_id)
                .get(transaction=transaction)
            )
            size_payload = size_doc.to_dict() or {}
            recipe = get_recipe_for_product_size(db, item.product_id, item.size_id, size_payload)

            for ing_id, base_amt in recipe.items():
                total_amt = base_amt * item.qty
                to_decrease[ing_id] = to_decrease.get(ing_id, 0.0) + total_amt

        # Пробуем списать
        for ing_id, delta in to_decrease.items():
            ref = db.collection("ingredients").document(ing_id)
            snap = ref.get(transaction=transaction)
            cur = (snap.to_dict() or {}).get("stock_quantity", 0.0)
            new_val = float(cur) - float(delta)
            if new_val < 0:
                raise ValueError(f"Недостаточно '{ing_id}' на складе (есть {cur}, нужно {delta}).")
            transaction.update(ref, {"stock_quantity": new_val})

        # Запись продажи
        db.collection("sales").document().set(
            {
                "created_at": firestore.SERVER_TIMESTAMP,
                "items": [
                    {
                        "product_id": it.product_id,
                        "product_name": it.product_name,
                        "size_id": it.size_id,
                        "size_name": it.size_name,
                        "volume": it.volume,
                        "qty": it.qty,
                        "price": it.price,
                        "sum": it.price * it.qty,
                    }
                    for it in sale_items
                ],
                "total": sum(it.price * it.qty for it in sale_items),
            },
            merge=False,
        )

    db.transaction(_tx)  # type: ignore[attr-defined]


# =========================
# UI helpers / стили
# =========================

CSS = """
<style>
/* чуть симпатичных плиток */
.gx-card {
  border-radius: 14px;
  border: 1px solid #e8e8ef;
  padding: 12px 14px;
  background: #fff;
  transition: all .15s ease;
  box-shadow: 0 2px 6px rgba(35,35,62,.05);
}
.gx-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(35,35,62,.08); }
.gx-name { font-weight: 600; }
.gx-sub { color:#6b7280; font-size: .85rem; }
.gx-chip {
  display:inline-block; padding:6px 10px; border-radius:10px; border:1px solid #e5e7eb;
  margin-right:6px; margin-top:6px; cursor:pointer; background:#fafafa;
}
.gx-chip-active { background:#eaf3ff; border-color:#cfe6ff; }
.gx-badge {
  display:inline-flex; align-items:center; gap:6px; font-size:.9rem; color:#374151;
}
.gx-cart {
  border-radius: 16px; border:1px solid #e5e7eb; background:#fcfcff; padding:16px;
}
.gx-total {
  display:flex; justify-content:space-between; padding-top:10px; border-top:1px dashed #e5e7eb;
  margin-top:8px; font-weight:700;
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def chip(label: str, active: bool, key: str) -> bool:
    """Возвращает True, если кликнули."""
    cls = "gx-chip gx-chip-active" if active else "gx-chip"
    return st.button(f"<span class='{cls}'>{label}</span>", key=key, help=label, type="secondary")


# =========================
# Основной UI
# =========================

def page_sales(db: firestore.Client):
    st.title("gipsy office — продажи")

    st.info("Продажа проводится только при нажатии «Купить». До этого позиции лежат в корзине и остатки не меняются.")

    cart_col, _ = st.columns([1, 0.15])

    with st.sidebar:
        st.subheader("Навигация")
        st.write("• Продажи\n• Склад\n• Рецепты\n• Поставки (см. верхние вкладки, если реализуете позже)")

    # Левая часть — категория → напитки
    categories = get_categories(db)
    st.subheader("Категории")
    if "ui_category" not in st.session_state and categories:
        st.session_state.ui_category = categories[0]

    cat_cols = st.columns(min(4, max(1, len(categories))))
    for i, cat in enumerate(categories):
        active = st.session_state.ui_category == cat
        if cat_cols[i % len(cat_cols)].button(
            f"☕ {cat}", use_container_width=True, type=("primary" if active else "secondary")
        ):
            st.session_state.ui_category = cat

    st.write("---")
    st.subheader(f"Напитки — {st.session_state.ui_category}")

    prods = get_products_by_category(db, st.session_state.ui_category)
    grid_cols = st.columns(3)

    for idx, (pid, pdata) in enumerate(prods):
        col = grid_cols[idx % 3]
        with col:
            with st.container(border=True):
                st.markdown(f"<div class='gx-card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='gx-name'>{pdata.get('name','Без названия')}</div>", unsafe_allow_html=True)
                st.markdown(f"<div class='gx-sub'>ID: {pid}</div>", unsafe_allow_html=True)

                sizes = get_sizes_for_product(db, pid)
                st.caption("Выберите объём / цену")

                # локальное состояние выбранного размера для плитки
                sel_key = f"sel_size_{pid}"
                if sel_key not in st.session_state and sizes:
                    st.session_state[sel_key] = sizes[0][0]

                chip_row = st.container()
                with chip_row:
                    for sid, sdata in sizes:
                        label = f"{sdata.get('name','Размер')} — {int(sdata.get('volume',0))} мл • {int(sdata.get('price',0))} ₽"
                        if chip(label, st.session_state[sel_key] == sid, key=f"chip_{pid}_{sid}"):
                            st.session_state[sel_key] = sid

                st.write("")
                qty = st.number_input("Кол-во", 1, 50, 1, key=f"qty_{pid}", label_visibility="collapsed")
                add_ok = st.button("В корзину", use_container_width=True, key=f"add_{pid}", type="primary")

                if add_ok:
                    chosen_sid = st.session_state[sel_key]
                    sdata = next((d for (sid, d) in sizes if sid == chosen_sid), None) or {}
                    item = CartItem(
                        product_id=pid,
                        product_name=pdata.get("name", "Без названия"),
                        size_id=chosen_sid,
                        size_name=sdata.get("name", "Размер"),
                        volume=sdata.get("volume"),
                        price=float(sdata.get("price", 0)),
                        qty=int(qty),
                    )
                    cart_add(item)
                    st.success("Добавлено в корзину")

                st.markdown("</div>", unsafe_allow_html=True)

    # Правая колонка — корзина
    with cart_col:
        st.subheader("🧺 Корзина")
        cart = cart_get()
        if not cart:
            st.info("Корзина пуста. Добавьте напитки слева.")
        else:
            with st.container(border=True):
                for i, it in enumerate(cart):
                    left, right = st.columns([0.7, 0.3])
                    with left:
                        st.markdown(
                            f"**{it.product_name}** — {it.size_name}"
                            + (f" ({int(it.volume)} мл)" if it.volume else "")
                        )
                        st.caption(f"{it.qty} × {int(it.price)} ₽")
                    with right:
                        # Изменение количества
                        new_q = st.number_input(
                            "qty", 1, 99, it.qty, key=f"q_cart_{i}", label_visibility="collapsed"
                        )
                        it.qty = int(new_q)
                st.markdown(
                    f"<div class='gx-total'><span>Итого:</span><span>{int(cart_total())} ₽</span></div>",
                    unsafe_allow_html=True,
                )
            col_buy, col_clear = st.columns([0.6, 0.4])
            if col_buy.button("Купить", type="primary", use_container_width=True):
                try:
                    adjust_stocks_transaction(db, cart_get())
                except Exception as e:
                    st.error(f"Ошибка при списании: {e}")
                else:
                    st.success("Продажа проведена ✅")
                    cart_clear()
                    st.experimental_rerun()
            if col_clear.button("Очистить", use_container_width=True):
                cart_clear()
                st.experimental_rerun()


def sidebar_secrets_check():
    with st.sidebar:
        st.markdown("### 🔍 Secrets check")
        prj = bool(st.secrets.get("PROJECT_ID") or st.secrets.get("PROJECT") or st.secrets.get("project_id"))
        st.write("• PROJECT_ID present:", "✅" if prj else "❌")

        t = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")
        st.write("• FIREBASE_SERVICE_ACCOUNT type:", type(t).__name__)
        if isinstance(t, dict):
            pk = t.get("private_key", "")
        elif isinstance(t, str):
            try:
                pk = json.loads(t).get("private_key", "")
            except Exception:
                pk = ""
        else:
            pk = ""
        st.write("• private_key length:", len(pk))
        st.write("• starts with BEGIN:", "✅" if "BEGIN PRIVATE KEY" in pk else "❌")
        st.write("• contains \\n literal:", "✅" if "\\n" in (t if isinstance(t, str) else str(pk)) else "❌")


def main():
    st.set_page_config(page_title="gipsy office — учёт", page_icon="☕", layout="wide")
    sidebar_secrets_check()

    # Подключаемся к БД
    try:
        db = init_firestore()
    except Exception as e:
        st.error(f"❌ Не удалось инициализировать Firestore: {e}")
        st.stop()

    # Главная страница — продажи
    page_sales(db)


if __name__ == "__main__":
    main()
