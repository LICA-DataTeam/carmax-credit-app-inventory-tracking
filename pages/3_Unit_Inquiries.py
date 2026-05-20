from calendar import monthrange
from datetime import date
import difflib
import pandas as pd
import streamlit as st
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
from google.oauth2 import service_account

import re
import gspread

from shared import load_service_account_info, normalize_spreadsheet_id

BQ_VIEW = st.secrets["BQ_VIEW"]
STATUS_COL_INDEX = 8  # STATUS


def _month_label(value: date) -> str:
    return value.strftime("%b %Y")


def _safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""


def _normalize_unit_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _is_available_status(value: str) -> bool:
    return value.strip().lower() == "available"


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


def _candidate_locations() -> list[str]:
    configured = str(st.secrets.get("BIGQUERY_LOCATION", "")).strip()
    candidates = [configured] if configured else []
    candidates.extend(["asia-southeast1", "US", "asia-east1", "asia-northeast1", "EU"])

    deduped: list[str] = []
    for value in candidates:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


@st.cache_resource
def _get_bigquery_client() -> bigquery.Client:
    service_account_info = load_service_account_info()
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    project_id = service_account_info.get("project_id", "carmax-ph")
    return bigquery.Client(project=project_id, credentials=credentials)


@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)


@st.cache_data(ttl=600)
def load_masterlist_unit_status_map() -> dict[str, str]:
    source = st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]
    spreadsheet_id = normalize_spreadsheet_id(source)

    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.worksheet(st.secrets["SHEET_NAME"])
    values = worksheet.get_all_values()

    if len(values) <= 1:
        return {}

    rows = values[1:]
    status_map: dict[str, str] = {}
    for row in rows:
        unit_parts = [_safe_cell(row, idx) for idx in st.secrets["UNIT_COL_INDEXES"] if _safe_cell(row, idx)]
        unit_value = " ".join(unit_parts)
        unit_key = _normalize_unit_text(unit_value)
        if not unit_key:
            continue

        status_value = _safe_cell(row, STATUS_COL_INDEX) or "Unknown"
        # Keep first seen status for stable mapping when duplicates exist.
        if unit_key not in status_map:
            status_map[unit_key] = status_value
    return status_map


@st.cache_data(ttl=600)
def load_credit_application_counts_map() -> dict[str, int]:
    summary_source = st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]
    summary_spreadsheet_id = normalize_spreadsheet_id(summary_source)
    reference_spreadsheet_id = normalize_spreadsheet_id(st.secrets["SECOND_SHEET_SPREADSHEET_ID"])

    client = _get_gspread_client()

    summary_workbook = client.open_by_key(summary_spreadsheet_id)
    summary_sheet = summary_workbook.worksheet(st.secrets["SHEET_NAME"])
    summary_values = summary_sheet.get_all_values()
    if len(summary_values) <= 1:
        return {}

    summary_rows = summary_values[1:]
    summary_keys: list[str] = []
    for row in summary_rows:
        if not _is_available_status(_safe_cell(row, STATUS_COL_INDEX)):
            continue
        unit_parts = [_safe_cell(row, idx) for idx in st.secrets["UNIT_COL_INDEXES"] if _safe_cell(row, idx)]
        unit_value = " ".join(unit_parts)
        unit_key = _normalize_unit_text(unit_value)
        if unit_key:
            summary_keys.append(unit_key)

    summary_keys = list(dict.fromkeys(summary_keys))
    if not summary_keys:
        return {}

    ref_workbook = client.open_by_key(reference_spreadsheet_id)
    ref_sheet = ref_workbook.get_worksheet(0)
    ref_values = ref_sheet.get_all_values()
    if len(ref_values) <= 1:
        return {}

    ref_rows = ref_values[1:]
    unit_applied_idx = int(st.secrets["REFERENCE_UNIT_APPLIED_COL_INDEX"])
    reference_keys = [
        _normalize_unit_text(_safe_cell(row, unit_applied_idx))
        for row in ref_rows
        if _safe_cell(row, unit_applied_idx)
    ]
    reference_keys = [key for key in reference_keys if key]
    if not reference_keys:
        return {}

    fuzzy_threshold = float(st.secrets.get("FUZZY_MATCH_THRESHOLD", 0.85))
    counts: dict[str, int] = {}
    for key in reference_keys:
        matched_key = _fuzzy_match_unit_key(key, summary_keys, fuzzy_threshold)
        if not matched_key:
            continue
        counts[matched_key] = counts.get(matched_key, 0) + 1

    return counts


def _query_result(
    query: str, job_config: bigquery.QueryJobConfig | None = None
) -> bigquery.table.RowIterator:
    client = _get_bigquery_client()
    errors: list[str] = []
    locations = _candidate_locations()

    # Try explicitly configured/fallback locations first.
    for location in locations:
        try:
            return client.query(query, job_config=job_config, location=location).result()
        except NotFound as exc:
            errors.append(f"{location}: {exc}")

    # Try without forcing location as a final fallback.
    try:
        return client.query(query, job_config=job_config).result()
    except NotFound as exc:
        errors.append(f"default: {exc}")
        hint = (
            "Unable to locate BigQuery view/table in available locations. "
            "Set BIGQUERY_LOCATION in .streamlit/secrets.toml to your dataset region "
            "(e.g., asia-southeast1 or US). "
            f"Attempted: {', '.join(locations + ['default'])}"
        )
        raise RuntimeError(f"{hint}\nDetails: {' | '.join(errors)}") from exc


def _query_to_dataframe(
    query: str, job_config: bigquery.QueryJobConfig | None = None
) -> pd.DataFrame:
    result = _query_result(query=query, job_config=job_config)
    rows = [dict(row.items()) for row in result]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


@st.cache_data(ttl=600)
def load_filter_dimensions() -> tuple[list[date], pd.DataFrame]:
    months_query = f"""
        SELECT DISTINCT DATE_TRUNC(inquiry_date, MONTH) AS month_start
        FROM `{BQ_VIEW}`
        WHERE inquiry_date IS NOT NULL
        ORDER BY month_start
    """
    month_rows = _query_result(months_query)
    month_values = [row["month_start"] for row in month_rows]

    make_model_query = f"""
        SELECT DISTINCT
            COALESCE(NULLIF(TRIM(make_name), ''), 'Unknown') AS make_name,
            COALESCE(NULLIF(TRIM(unit_alias), ''), 'Unknown') AS model_name
        FROM `{BQ_VIEW}`
        ORDER BY make_name, model_name
    """
    make_model_df = _query_to_dataframe(make_model_query)
    return month_values, make_model_df


@st.cache_data(ttl=600)
def load_unit_inquiries(
    start_date: date,
    end_date: date,
    selected_makes: tuple[str, ...],
    selected_models: tuple[str, ...],
) -> pd.DataFrame:
    query = f"""
        SELECT
            unit_full,
            unit_alias,
            model_year,
            COALESCE(NULLIF(TRIM(make_name), ''), 'Unknown') AS make_name,
            SUM(COALESCE(client_mentions, 0)) AS client_initiated_mentions,
            SUM(COALESCE(agent_mentions, 0)) AS agent_initiated_mentions,
            SUM(COALESCE(strict_total_mentions, 0)) AS total_mentions
        FROM `{BQ_VIEW}`
        WHERE inquiry_date BETWEEN @start_date AND @end_date
          AND (@use_make_filter = FALSE OR COALESCE(NULLIF(TRIM(make_name), ''), 'Unknown') IN UNNEST(@make_names))
          AND (@use_model_filter = FALSE OR COALESCE(NULLIF(TRIM(unit_alias), ''), 'Unknown') IN UNNEST(@model_names))
        GROUP BY unit_full, unit_alias, model_year, make_name
        ORDER BY total_mentions DESC, client_initiated_mentions DESC, unit_full
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            bigquery.ScalarQueryParameter("use_make_filter", "BOOL", bool(selected_makes)),
            bigquery.ArrayQueryParameter("make_names", "STRING", list(selected_makes)),
            bigquery.ScalarQueryParameter("use_model_filter", "BOOL", bool(selected_models)),
            bigquery.ArrayQueryParameter("model_names", "STRING", list(selected_models)),
        ]
    )
    return _query_to_dataframe(query=query, job_config=job_config)


def render_page() -> None:
    st.title("Unit Inquiries (Client and Agent initiated)")

    try:
        month_options, make_model_df = load_filter_dimensions()
        unit_status_map = load_masterlist_unit_status_map()
        unit_credit_app_counts = load_credit_application_counts_map()
    except Exception as exc:
        st.error(f"Failed to load filters from BigQuery view: {exc}")
        st.stop()

    if not month_options:
        st.warning("No data found in inquiry view.")
        return

    month_options = sorted(month_options)
    default_start_index = max(0, len(month_options) - 3)
    default_end_index = len(month_options) - 1

    status_options = sorted(set(unit_status_map.values())) if unit_status_map else ["Unknown"]
    status_filter_options = ["All"] + status_options

    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.1, 1.1, 1.2, 1.5, 0.8, 1.0, 1.3])
    selected_start_month = c1.selectbox(
        "Start Month",
        month_options,
        index=default_start_index,
        format_func=_month_label,
    )
    selected_end_month = c2.selectbox(
        "End Month",
        month_options,
        index=default_end_index,
        format_func=_month_label,
    )

    make_options = sorted(make_model_df["make_name"].dropna().unique().tolist())
    selected_makes = c3.multiselect("Make", make_options, default=[])

    if selected_makes:
        model_source = make_model_df[make_model_df["make_name"].isin(selected_makes)]
    else:
        model_source = make_model_df
    model_options = sorted(model_source["model_name"].dropna().unique().tolist())
    selected_models = c4.multiselect("Model", model_options, default=[])

    top_n = int(c5.number_input("Top N", min_value=1, max_value=500, value=30, step=1))
    selected_status = c6.selectbox("Status", status_filter_options, index=0)
    selected_ca_filter = c7.selectbox(
        "Credit Applications",
        ["All", "With Credit Applications", "Without Credit Applications"],
        index=0,
    )

    if selected_start_month > selected_end_month:
        st.error("Start Month cannot be later than End Month.")
        return

    start_date = date(selected_start_month.year, selected_start_month.month, 1)
    end_day = monthrange(selected_end_month.year, selected_end_month.month)[1]
    end_date = date(selected_end_month.year, selected_end_month.month, end_day)

    try:
        inquiry_df = load_unit_inquiries(
            start_date=start_date,
            end_date=end_date,
            selected_makes=tuple(selected_makes),
            selected_models=tuple(selected_models),
        )
    except Exception as exc:
        st.error(f"Failed to query inquiry data from BigQuery view: {exc}")
        st.stop()

    if inquiry_df.empty:
        st.info("No inquiry results for the selected filters.")
        return

    inquiry_df["unit_key"] = inquiry_df["unit_full"].fillna("").map(_normalize_unit_text)
    inquiry_df["status"] = inquiry_df["unit_key"].map(unit_status_map).fillna("Unknown")
    inquiry_df["credit_applications"] = (
        inquiry_df["unit_key"].map(unit_credit_app_counts).fillna(0).astype(int)
    )
    if selected_status != "All":
        inquiry_df = inquiry_df[inquiry_df["status"] == selected_status].copy()
    if selected_ca_filter == "With Credit Applications":
        inquiry_df = inquiry_df[inquiry_df["credit_applications"] > 0].copy()
    elif selected_ca_filter == "Without Credit Applications":
        inquiry_df = inquiry_df[inquiry_df["credit_applications"] == 0].copy()

    if inquiry_df.empty:
        st.info("No inquiry results for the selected filters.")
        return

    total_client_mentions = int(inquiry_df["client_initiated_mentions"].sum())
    total_agent_mentions = int(inquiry_df["agent_initiated_mentions"].sum())
    total_mentions = int(inquiry_df["total_mentions"].sum())

    st.subheader("Totals for Selected Period")
    m1, m2, m3 = st.columns(3)
    m1.metric("Client-Initiated Mentions", total_client_mentions)
    m2.metric("Agent-Initiated Mentions", total_agent_mentions)
    m3.metric("Total Mentions", total_mentions)

    top_df = inquiry_df.head(top_n).copy()
    top_df.insert(0, "rank", range(1, len(top_df) + 1))

    display_df = top_df.rename(
        columns={
            "unit_full": "unit",
            "unit_alias": "model",
            "model_year": "year",
            "make_name": "make",
            "client_initiated_mentions": "client_initiated_mentions",
            "agent_initiated_mentions": "agent_initiated_mentions",
            "total_mentions": "total_mentions",
            "status": "status",
            "credit_applications": "credit_applications",
        }
    )[
        [
            "rank",
            "unit",
            "make",
            "model",
            "year",
            "status",
            "credit_applications",
            "client_initiated_mentions",
            "agent_initiated_mentions",
            "total_mentions",
        ]
    ]

    st.caption(
        f"Showing top {len(display_df)} units from {_month_label(selected_start_month)} to "
        f"{_month_label(selected_end_month)}."
    )
    st.dataframe(
        display_df,
        use_container_width=True,
        column_config={
            "credit_applications": st.column_config.NumberColumn("Credit Applications"),
            "client_initiated_mentions": st.column_config.NumberColumn("Client-Initiated Mentions"),
            "agent_initiated_mentions": st.column_config.NumberColumn("Agent-Initiated Mentions"),
            "total_mentions": st.column_config.NumberColumn("Total Mentions"),
        },
    )


def main() -> None:
    st.set_page_config(page_title="Unit Inquiries", layout="wide")
    render_page()


if __name__ == "__main__":
    main()
