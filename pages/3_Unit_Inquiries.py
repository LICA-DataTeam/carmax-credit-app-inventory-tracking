import gspread
import pandas as pd
import streamlit as st
from shared import normalize_spreadsheet_id, load_service_account_info

def _safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""

@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)


def _exclude_junk_repair_block(df: pd.DataFrame) -> pd.DataFrame:
    units_lower = df["unit"].fillna("").astype(str).str.lower()
    start_matches = units_lower[units_lower.str.contains("2004 isuzu double cab", regex=False)].index.tolist()
    end_matches = units_lower[units_lower.str.contains("2009 subaru forester", regex=False)].index.tolist()

    if not start_matches or not end_matches:
        return df

    start_idx = start_matches[0]
    end_idx = next((idx for idx in end_matches if idx >= start_idx), end_matches[0])
    if end_idx < start_idx:
        return df

    return df.drop(index=range(start_idx, end_idx + 1)).reset_index(drop=True)


def render_page() -> None:
    st.title("Unit Inquiries (Client and Agent initiated)")
    source = st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]
    if not source:
        st.error("Missing spreadsheet ID.")
        return

    spreadsheet_id = normalize_spreadsheet_id(source)

    try:
        summary_df = load_summary_credit_view(spreadsheet_id)
    except Exception as e:
        st.error(f"Failed to load source data: {e}")
        st.stop()

    if summary_df.empty:
        st.warning("No summary data from masterlist available")
        return

    st.dataframe(summary_df)

@st.cache_data(ttl=300)
def load_summary_credit_view(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.worksheet(st.secrets["SHEET_NAME"])
    values = worksheet.get_all_values()

    if len(values) <= 1:
        return pd.DataFrame(
            columns=[
                "unit",
                "model",
                "acquisition_cost",
                "target_selling_price",
                "aging",
                "plate_number"
            ]
        )

    rows = values[1:]
    prepared_rows: list[dict] = []
    plate_col_index = 7  # H
    for row in rows:
        unit_base = " ".join([part for part in [_safe_cell(row, idx) for idx in st.secrets["UNIT_COL_INDEXES"]] if part])

        if not unit_base:
            continue

        prepared_rows.append(
            {
                "unit": unit_base,
                "model": _safe_cell(row, st.secrets["SUMMARY_MODEL_COL_INDEX"]),
                "acquisition_cost": _safe_cell(row, st.secrets["SUMMARY_ACQUISITION_COL_INDEX"]),
                "target_selling_price": _safe_cell(row, st.secrets["SUMMARY_TARGET_COL_INDEX"]),
                "aging": _safe_cell(row, st.secrets["SUMMARY_AGING_COL_INDEX"]),
                "plate_number": _safe_cell(row, plate_col_index),
            }
        )

    df = pd.DataFrame(prepared_rows)
    if df.empty:
        return df

    return _exclude_junk_repair_block(df).reset_index(drop=True)

def main() -> None:
    st.set_page_config(page_title="Unit Inquiries", layout="wide")
    render_page()

if __name__ == "__main__":
    main()
