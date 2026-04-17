import os
import re

import gspread
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

SHEET_NAME = "SUMMARY"
UNIT_COL_INDEXES = [2, 4, 5, 6]  # C, E, F, G
OTHER_COL_INDEXES = [3, 7, 17, 28, 29, 30]  # D, H, R, AC, AD, AE
SUMMARY_ACQUISITION_COL_INDEX = 17  # R
SUMMARY_AGING_COL_INDEX = 29  # AD
SUMMARY_TARGET_COL_INDEX = 30  # AE
SUMMARY_MODEL_COL_INDEX = 5  # F
SECOND_SHEET_SPREADSHEET_ID = "1oysu88ykH2_L2GYBTiSiWUx2VSkHEHgrxsQQNW0BeVo"
SECOND_SHEET_COL_INDEXES = [1, 2, 57, 60]  # B, C, BF, BI
REFERENCE_UNIT_APPLIED_COL_INDEX = 57  # BF


def _normalize_spreadsheet_id(value: str) -> str:
    if "/spreadsheets/d/" in value:
        return value.split("/spreadsheets/d/")[1].split("/")[0]
    return value.strip()


def _load_service_account_info() -> dict:
    if "gcp_service_account" in st.secrets:
        return dict(st.secrets["gcp_service_account"])

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        import json

        return json.loads(raw_json)

    raise RuntimeError(
        "Missing Google credentials. Add [gcp_service_account] to Streamlit secrets "
        "or set GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def _safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""


def _normalize_unit_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = _load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)


@st.cache_data(ttl=300)
def load_summary_dataframe(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.worksheet(SHEET_NAME)
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    header = values[0]
    rows = values[1:]

    other_headers = [header[i] if i < len(header) else f"COL_{i + 1}" for i in OTHER_COL_INDEXES]
    selected_headers = ["unit"] + other_headers
    selected_rows = []

    for row in rows:
        unit_parts = [_safe_cell(row, i) for i in UNIT_COL_INDEXES if _safe_cell(row, i)]
        unit_value = " ".join(unit_parts)
        other_values = [_safe_cell(row, i) for i in OTHER_COL_INDEXES]
        selected_rows.append([unit_value] + other_values)

    return pd.DataFrame(selected_rows, columns=selected_headers)


@st.cache_data(ttl=300)
def load_selected_columns_from_first_sheet(spreadsheet_id: str, column_indexes: list[int]) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.get_worksheet(0)
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    header = values[0]
    rows = values[1:]
    selected_headers = [header[i] if i < len(header) else f"COL_{i + 1}" for i in column_indexes]
    selected_rows = [[_safe_cell(row, i) for i in column_indexes] for row in rows]
    return pd.DataFrame(selected_rows, columns=selected_headers)


@st.cache_data(ttl=300)
def load_summary_credit_view(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.worksheet(SHEET_NAME)
    values = worksheet.get_all_values()

    if len(values) <= 1:
        return pd.DataFrame(
            columns=[
                "unit_base",
                "unit",
                "model",
                "acquisition_cost",
                "target_selling_price",
                "aging",
                "unit_key",
            ]
        )

    rows = values[1:]
    prepared_rows: list[dict] = []
    for row in rows:
        unit_base = " ".join([part for part in [_safe_cell(row, idx) for idx in UNIT_COL_INDEXES] if part])
        unit_key = _normalize_unit_text(unit_base)

        if not unit_key:
            continue

        prepared_rows.append(
            {
                "unit_base": unit_base,
                "unit": unit_base,
                "model": _safe_cell(row, SUMMARY_MODEL_COL_INDEX),
                "acquisition_cost": _safe_cell(row, SUMMARY_ACQUISITION_COL_INDEX),
                "target_selling_price": _safe_cell(row, SUMMARY_TARGET_COL_INDEX),
                "aging": _safe_cell(row, SUMMARY_AGING_COL_INDEX),
                "unit_key": unit_key,
            }
        )

    df = pd.DataFrame(prepared_rows)
    if df.empty:
        return df

    return df.drop_duplicates(subset=["unit_key"], keep="first").reset_index(drop=True)


def _find_reference_unit_applied_column(reference_df: pd.DataFrame) -> str:
    if reference_df.empty:
        return ""

    for column_name in reference_df.columns:
        normalized = column_name.lower().strip()
        if "unit applied for" in normalized or "unit applied" in normalized:
            return column_name

    # Fallback to BF among selected columns B, C, BF, BI
    if len(reference_df.columns) >= 3:
        return reference_df.columns[2]
    return reference_df.columns[0]


def _render_distribution_cards(total_units: int, card_items: list[dict]) -> None:
    st.markdown(
        """
        <style>
        .ca-dist-card {
            border-radius: 14px;
            padding: 14px 16px;
            box-shadow: 0 6px 18px rgba(15, 23, 42, 0.10);
            min-height: 120px;
        }
        .ca-dist-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 8px;
        }
        .ca-dist-value {
            font-size: 2rem;
            font-weight: 800;
            line-height: 1.1;
            color: #111827;
            margin-bottom: 4px;
        }
        .ca-dist-sub {
            font-size: 0.82rem;
            color: #374151;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    cols = st.columns(len(card_items))
    for col, item in zip(cols, card_items):
        count = int(item["count"])
        pct = (count / total_units * 100) if total_units else 0.0
        col.markdown(
            f"""
            <div class="ca-dist-card" style="background:{item['background']}; border:1px solid {item['border']};">
                <div class="ca-dist-title">{item['title']}</div>
                <div class="ca-dist-value">{count}</div>
                <div class="ca-dist-sub">{pct:.1f}% of {total_units} units</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_summary_page() -> None:
    st.title("Masterlist Inventory - Summary")

    default_spreadsheet = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    spreadsheet_input = st.text_input(
        "Google Spreadsheet URL or ID",
        value=default_spreadsheet,
        placeholder="https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit",
    )

    if not spreadsheet_input:
        st.info("Enter a spreadsheet URL/ID (or set GOOGLE_SHEETS_SPREADSHEET_ID).")
        return

    spreadsheet_id = _normalize_spreadsheet_id(spreadsheet_input)

    try:
        df = load_summary_dataframe(spreadsheet_id)
    except Exception as exc:
        st.error(f"Failed to load sheet data: {exc}")
        st.stop()

    if df.empty:
        st.warning("No data found in SUMMARY sheet.")
        return

    st.dataframe(df, use_container_width=True)
    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name="summary_selected_columns.csv",
        mime="text/csv",
    )


def render_reference_page() -> None:
    st.title("Reference Sheet - B, C, BF, BI")
    st.caption("Source: fixed spreadsheet ID from the shared link (first worksheet).")

    try:
        df = load_selected_columns_from_first_sheet(SECOND_SHEET_SPREADSHEET_ID, SECOND_SHEET_COL_INDEXES)
    except Exception as exc:
        st.error(f"Failed to load reference sheet data: {exc}")
        st.stop()

    if df.empty:
        st.warning("No data found in the reference sheet.")
        return

    st.dataframe(df, use_container_width=True)
    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name="reference_sheet_selected_columns.csv",
        mime="text/csv",
    )


def render_credit_apps_page() -> None:
    st.title("CarMax Inventory Tracking & Credit Application Tracker")
    st.caption("Matches SUMMARY units against Unit Applied For (ignores color and counts by unit name only).")

    summary_spreadsheet_source = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not summary_spreadsheet_source:
        st.error("Missing GOOGLE_SHEETS_SPREADSHEET_ID in environment.")
        return

    summary_spreadsheet_id = _normalize_spreadsheet_id(summary_spreadsheet_source)
    summary_spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{summary_spreadsheet_id}/edit"
    reference_spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SECOND_SHEET_SPREADSHEET_ID}/edit"

    # st.markdown(f"Masterlist source: [Open SUMMARY sheet]({summary_spreadsheet_url})")
    # st.markdown(f"Credit App source: [Open responses sheet]({reference_spreadsheet_url})")

    try:
        summary_df = load_summary_credit_view(summary_spreadsheet_id)
        reference_df = load_selected_columns_from_first_sheet(SECOND_SHEET_SPREADSHEET_ID, SECOND_SHEET_COL_INDEXES)
    except Exception as exc:
        st.error(f"Failed to load source data: {exc}")
        st.stop()

    if summary_df.empty:
        st.warning("No summary data available to compare.")
        return

    if reference_df.empty:
        st.warning("No reference credit-application data available.")
        return

    unit_applied_column = _find_reference_unit_applied_column(reference_df)
    if not unit_applied_column:
        st.warning("Unable to detect Unit Applied For column from reference data.")
        return

    reference_keys = reference_df[unit_applied_column].fillna("").astype(str).map(_normalize_unit_text)
    reference_keys = reference_keys[reference_keys != ""]
    app_count_by_unit_key = reference_keys.value_counts()

    all_units_df = summary_df.copy()
    all_units_df["number_of_credit_apps"] = all_units_df["unit_key"].map(app_count_by_unit_key).fillna(0).astype(int)

    metric_gt_3 = int((all_units_df["number_of_credit_apps"] > 3).sum())
    metric_eq_2 = int((all_units_df["number_of_credit_apps"] == 2).sum())
    metric_eq_1 = int((all_units_df["number_of_credit_apps"] == 1).sum())
    metric_eq_0 = int((all_units_df["number_of_credit_apps"] == 0).sum())
    total_units = len(all_units_df)
    _render_distribution_cards(
        total_units=total_units,
        card_items=[
            {
                "title": "Units with >3 CAs",
                "count": metric_gt_3,
                "background": "linear-gradient(135deg, #fee2e2, #fecaca)",
                "border": "#f87171",
            },
            {
                "title": "Units with 2 CAs",
                "count": metric_eq_2,
                "background": "linear-gradient(135deg, #ffedd5, #fed7aa)",
                "border": "#fb923c",
            },
            {
                "title": "Units with 1 CA",
                "count": metric_eq_1,
                "background": "linear-gradient(135deg, #dcfce7, #bbf7d0)",
                "border": "#4ade80",
            },
            {
                "title": "Units with 0 CAs",
                "count": metric_eq_0,
                "background": "linear-gradient(135deg, #e0f2fe, #bae6fd)",
                "border": "#38bdf8",
            },
        ],
    )

    matched_df = all_units_df[all_units_df["number_of_credit_apps"] > 0].copy()
    if matched_df.empty:
        st.info("No matching units found between masterlist and credit applications.")
        return

    matched_df = matched_df.sort_values(by=["number_of_credit_apps", "unit"], ascending=[False, True]).reset_index(drop=True)

    output_df = matched_df.rename(
        columns={
            "unit": "unit",
            "model": "model",
            "acquisition_cost": "acquisition cost",
            "target_selling_price": "target selling price",
            "aging": "aging",
            "number_of_credit_apps": "number of credit apps",
        }
    )[["unit", "model", "acquisition cost", "target selling price", "aging", "number of credit apps"]]

    c5, c6 = st.columns(2)
    c5.metric("Units with credit apps", len(output_df))
    c6.metric("Total credit applications", int(output_df["number of credit apps"].sum()))

    st.dataframe(output_df, use_container_width=True)
    st.download_button(
        "Download CSV",
        output_df.to_csv(index=False).encode("utf-8"),
        file_name="units_with_credit_apps.csv",
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(page_title="Inventory Tracking", layout="wide")
    render_credit_apps_page()


if __name__ == "__main__":
    main()
