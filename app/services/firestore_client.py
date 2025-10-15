from typing import Any, Dict
import json
import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account


@st.cache_resource
def get_db() -> firestore.Client:
    project_id = st.secrets.get("PROJECT_ID")
    svc = st.secrets.get("FIREBASE_SERVICE_ACCOUNT")

    if not project_id:
        st.error("❌ В secrets отсутствует PROJECT_ID.")
        st.stop()
    if not svc:
        st.error("❌ В secrets отсутствует FIREBASE_SERVICE_ACCOUNT.")
        st.stop()

    try:
        info: Dict[str, Any]
        if isinstance(svc, str):
            info = json.loads(svc)
        else:
            info = dict(svc)

        creds = service_account.Credentials.from_service_account_info(info)
        db = firestore.Client(credentials=creds, project=project_id)
        # быстрый sanity-check
        _ = list(db.collections())
        return db
    except Exception as e:
        st.error(f"Не удалось инициализировать Firestore: {e}")
        st.stop()
