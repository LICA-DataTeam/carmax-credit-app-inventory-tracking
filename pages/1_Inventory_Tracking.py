import re
import gspread
import difflib
import pandas as pd
import streamlit as st
from shared import normalize_spreadsheet_id, load_service_account_info

def _safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""


def _normalize_unit_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _normalize_plate_text(value: str) -> str:
    # Normalize plate text so minor format differences (spaces/hyphens/case) still map together.
    return re.sub(r"[^a-z0-9]+", "", value.lower()).strip()


def _token_jaccard(a: str, b: str) -> float:
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _extract_year_make(key: str) -> tuple[str, str]:
    tokens = key.split()
    year = tokens[0] if tokens and re.fullmatch(r"\d{4}", tokens[0]) else ""
    make = tokens[1] if len(tokens) > 1 else ""
    return year, make


def _fuzzy_match_unit_key(source_key: str, candidate_keys: list[str], threshold: float) -> str | None:
    if source_key in candidate_keys:
        return source_key

    source_year, source_make = _extract_year_make(source_key)
    narrowed = candidate_keys
    if source_year:
        narrowed = [k for k in narrowed if _extract_year_make(k)[0] == source_year] or narrowed
    if source_make:
        narrowed = [k for k in narrowed if _extract_year_make(k)[1] == source_make] or narrowed

    best_key = None
    best_score = -1.0
    for candidate in narrowed:
        seq = difflib.SequenceMatcher(None, source_key, candidate).ratio()
        jac = _token_jaccard(source_key, candidate)
        score = 0.7 * seq + 0.3 * jac
        if score > best_score:
            best_key = candidate
            best_score = score

    if best_key is None:
        return None
    return best_key if best_score >= threshold else None


def _build_app_counts_exact(reference_keys: pd.Series) -> tuple[pd.Series, int]:
    counts = reference_keys.value_counts()
    return counts, 0


def _build_app_counts_fuzzy(
    reference_keys: pd.Series, summary_keys: list[str], threshold: float
) -> tuple[pd.Series, int]:
    key_map: dict[str, str | None] = {}
    for key in reference_keys.unique():
        key_map[key] = _fuzzy_match_unit_key(key, summary_keys, threshold)

    mapped_keys = reference_keys.map(key_map)
    unmatched_count = int(mapped_keys.isna().sum())
    mapped_keys = mapped_keys.dropna()
    return mapped_keys.value_counts(), unmatched_count


@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)

@st.cache_data(ttl=300)
def load_summary_dataframe(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.worksheet(st.secrets["SHEET_NAME"])
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    header = values[0]
    rows = values[1:]

    other_headers = [header[i] if i < len(header) else f"COL_{i + 1}" for i in st.secrets["OTHER_COL_INDEXES"]]
    selected_headers = ["unit"] + other_headers
    selected_rows = []

    for row in rows:
        unit_parts = [_safe_cell(row, i) for i in st.secrets["UNIT_COL_INDEXES"] if _safe_cell(row, i)]
        unit_value = " ".join(unit_parts)
        other_values = [_safe_cell(row, i) for i in st.secrets["OTHER_COL_INDEXES"]]
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
    worksheet = workbook.worksheet(st.secrets["SHEET_NAME"])
    values = worksheet.get_all_values()

    if len(values) <= 1:
        return pd.DataFrame(
            columns=[
                "unit_base",
                "unit",
                "plate_number",
                "model",
                "acquisition_cost",
                "target_selling_price",
                "aging",
                "unit_key",
                "plate_key",
            ]
        )

    rows = values[1:]
    prepared_rows: list[dict] = []
    plate_col_index = 7  # H: plate number in SUMMARY / OTHER_COL_INDEXES
    for row in rows:
        unit_base = " ".join([part for part in [_safe_cell(row, idx) for idx in st.secrets["UNIT_COL_INDEXES"]] if part])
        unit_key = _normalize_unit_text(unit_base)
        plate_number = _safe_cell(row, plate_col_index)
        plate_key = _normalize_plate_text(plate_number)

        if not unit_key:
            continue

        prepared_rows.append(
            {
                "unit_base": unit_base,
                "unit": unit_base,
                "plate_number": plate_number,
                "model": _safe_cell(row, st.secrets["SUMMARY_MODEL_COL_INDEX"]),
                "acquisition_cost": _safe_cell(row, st.secrets["SUMMARY_ACQUISITION_COL_INDEX"]),
                "target_selling_price": _safe_cell(row, st.secrets["SUMMARY_TARGET_COL_INDEX"]),
                "aging": _safe_cell(row, st.secrets["SUMMARY_AGING_COL_INDEX"]),
                "unit_key": unit_key,
                "plate_key": plate_key,
            }
        )

    df = pd.DataFrame(prepared_rows)
    if df.empty:
        return df

    # Keep distinct inventory entries by unit + plate number.
    return df.drop_duplicates(subset=["unit_key", "plate_key"], keep="first").reset_index(drop=True)

def _find_reference_unit_applied_column(reference_df: pd.DataFrame) -> str:
    if reference_df.empty:
        return ""

    for column_name in reference_df.columns:
        normalized = column_name.lower().strip()
        if "unit applied for" in normalized or "unit applied" in normalized:
            return column_name

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

def render_page() -> None:
    st.title("CarMax Inventory Tracking & Credit Application Tracker")
    summary_spread_sheet_source = st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]
    if not summary_spread_sheet_source:
        st.error("Missing spreadsheet ID.")
        return

    summary_spreadsheet_id = normalize_spreadsheet_id(summary_spread_sheet_source)

    try:
        summary_df = load_summary_credit_view(summary_spreadsheet_id)
        reference_df = load_selected_columns_from_first_sheet(st.secrets["SECOND_SHEET_SPREADSHEET_ID"], st.secrets["SECOND_SHEET_COL_INDEXES"])
    except Exception as e:
        st.error(f"Failed to load source data: {e}")
        st.stop()

    if summary_df.empty:
        st.warning("No summary data available.")
        return

    unit_applied_column = _find_reference_unit_applied_column(reference_df)
    if not unit_applied_column:
        st.warning("Unable to dectec Unit Applied For column from reference data.")
        return

    fuzzy_enabled = st.toggle("Use fuzzy matching (temporary)", value=True)
    fuzzy_threshold = st.slider(
        "Fuzzy match threshold",
        min_value=0.70,
        max_value=0.98,
        value=0.85,
        step=0.01,
        disabled=not fuzzy_enabled,
        help="Higher = stricter matching. Lower = more permissive matching."
    )

    reference_keys = reference_df[unit_applied_column].fillna("").astype(str).map(_normalize_unit_text)
    reference_keys = reference_keys[reference_keys != ""]
    if fuzzy_enabled:
        summary_keys = summary_df["unit_key"].tolist()
        app_count_by_unit_key, unmatched_reference_rows = _build_app_counts_fuzzy(
            reference_keys, summary_keys, fuzzy_threshold
        )
        st.caption(
            f"Fuzzy matching enabled (threshold {fuzzy_threshold:.2f}). "
            f"Unmatched credit-app rows: {unmatched_reference_rows}"
        )
    else:
        app_count_by_unit_key, unmatched_reference_rows = _build_app_counts_exact(reference_keys)
        st.caption("Exact matching enabled.")

    all_units_df = summary_df.copy()
    all_units_df["number_of_credit_apps"] = all_units_df["unit_key"].map(app_count_by_unit_key).fillna(0).astype(int)

    unit_level_df = all_units_df.drop_duplicates(subset=["unit_key"], keep="first")
    metric_gt_3 = int((unit_level_df["number_of_credit_apps"] > 3).sum())
    metric_eq_2 = int((unit_level_df["number_of_credit_apps"] == 2).sum())
    metric_eq_1 = int((unit_level_df["number_of_credit_apps"] == 1).sum())
    metric_eq_0 = int((unit_level_df["number_of_credit_apps"] == 0).sum())
    total_units = len(unit_level_df)
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

    matched_df = matched_df.sort_values(
        by=["number_of_credit_apps", "unit", "plate_number"], ascending=[False, True, True]
    ).reset_index(drop=True)

    output_df = matched_df.rename(
        columns={
            "unit": "unit",
            "plate_number": "plate number",
            "model": "model",
            "acquisition_cost": "acquisition cost",
            "target_selling_price": "target selling price",
            "aging": "aging",
            "number_of_credit_apps": "number of credit apps",
        }
    )[["unit", "plate number", "model", "acquisition cost", "target selling price", "aging", "number of credit apps"]]
    unique_matched_units = matched_df.drop_duplicates(subset=["unit_key"], keep="first")

    c5, c6 = st.columns(2)
    c5.metric("Inventory rows with credit apps", len(output_df))
    c6.metric("Total credit applications", int(unique_matched_units["number_of_credit_apps"].sum()))

    st.dataframe(output_df, use_container_width=True)
    st.download_button(
        "Download CSV",
        output_df.to_csv(index=False).encode("utf-8"),
        file_name="units_with_credit_apps.csv",
        mime="text/csv",
    )

def main() -> None:
    st.set_page_config(page_title="Inventory Tracking", layout="wide")
    render_page()

if __name__ == "__main__":
    main()
