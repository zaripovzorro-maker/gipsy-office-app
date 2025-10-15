from __future__ import annotations
import json
import streamlit as st
import firebase_admin
from firebase_admin import credentials
from google.cloud import firestore
from typing import Dict, Any

def _read_service_account() -> Dict[str, Any]:
    if "FIREBASE_SERVICE_ACCOUNT" not in st.secrets:
        raise RuntimeError("В Secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")
    raw = st.secrets["FIREBASE_SERVICE_ACCOUNT"]

    # JSON-строка
    if isinstance(raw, str):
        data = json.loads(raw)
        pk = data.get("private_key", "")
        if "\\n" in pk and "\n" not in pk:
            data["private_key"] = pk.replace("\\n", "\n")
        return data

    # TOML-таблица (dict)
    if isinstance(raw, dict):
        data = dict(raw)
        pk = data.get("private_key", "")
        if not str(pk).startswith("-----BEGIN PRIVATE KEY-----"):
            raise RuntimeError("private_key (TOML) должен начинаться с -----BEGIN PRIVATE KEY-----")
        return data

    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT должен быть JSON-строкой или таблицей TOML.")

@st.cache_resource(show_spinner=False)
def get_db() -> firestore.Client:
    project_id = st.secrets.get("PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError("В Secrets нет PROJECT_ID.")
    data = _read_service_account()

    if not firebase_admin._apps:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred, {"projectId": project_id})

    return firestore.Client(project=project_id)

def secrets_diag():
    st.markdown("### 🔍 Secrets")
    ok1 = bool(st.secrets.get("PROJECT_ID"))
    st.write("• PROJECT_ID:", "✅" if ok1 else "❌")
    exist = "FIREBASE_SERVICE_ACCOUNT" in st.secrets
    st.write("• FIREBASE_SERVICE_ACCOUNT:", "✅" if exist else "❌")
    if exist:
        raw = st.secrets["FIREBASE_SERVICE_ACCOUNT"]
        t = type(raw).__name__
        st.write("• type:", t)
        try:
            if isinstance(raw, str):
                d = json.loads(raw)
            else:
                d = dict(raw)
            pk = d.get("private_key", "")
            st.write(f"• private_key length: {len(pk)}")
            st.write("• starts with BEGIN:", "✅" if str(pk).startswith("-----BEGIN PRIVATE KEY-----") else "❌")
            st.write("• contains \\n literal:", "✅" if isinstance(raw, str) and "\\n" in raw else "➖")
        except Exception:
            st.write("• private_key: (не разобран)")

