import streamlit as st

def normalize_spreadsheet_id(value: str) -> str:
    if "/spreadsheets/d/" in value:
        return value.split("/spreadsheets/d/")[1].split("/")[0]
    return value.strip()

def load_service_account_info() -> dict:
    def _normalize_service_account_info(info: dict) -> dict:
        normalized = dict(info)
        private_key = normalized.get("private_key")
        if isinstance(private_key, str):
            # Streamlit secrets may contain literal "\n"; convert to real newlines.
            normalized["private_key"] = private_key.replace("\\n", "\n")
        return normalized

    if "gcp_service_account" in st.secrets:
        return _normalize_service_account_info(dict(st.secrets["gcp_service_account"]))

    raise RuntimeError(
        "Missing Google credentials. Add [gcp_service_account] to Streamlit secrets "
        "or set GOOGLE_SERVICE_ACCOUNT_JSON."
    )