import re
from collections import Counter

import gspread
import pandas as pd
import streamlit as st

from shared import load_service_account_info, normalize_spreadsheet_id

STATUS_COL_INDEX = 28  # AC
PLATE_COL_INDEX = 7  # H
REFERENCE_MONTH_COL_INDEX = 0  # A (first column) from SECOND_SHEET_SPREADSHEET_ID


def _safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""


def _normalize_unit_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _normalize_plate_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower()).strip()


def _normalize_header_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_available_status(value: str) -> bool:
    return value.strip().lower() == "available"


@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)


@st.cache_data(ttl=300)
def load_first_sheet_dataframe(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.get_worksheet(0)
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    headers = [header if header else f"COL_{i + 1}" for i, header in enumerate(values[0])]
    rows = values[1:]
    selected_rows = [[_safe_cell(row, i) for i in range(len(headers))] for row in rows]
    return pd.DataFrame(selected_rows, columns=headers)


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
    for row in rows:
        if not _is_available_status(_safe_cell(row, STATUS_COL_INDEX)):
            continue

        unit_parts = [_safe_cell(row, idx) for idx in st.secrets["UNIT_COL_INDEXES"] if _safe_cell(row, idx)]
        unit_value = " ".join(unit_parts)
        unit_key = _normalize_unit_text(unit_value)
        if not unit_key:
            continue

        plate_number = _safe_cell(row, PLATE_COL_INDEX)
        plate_key = _normalize_plate_text(plate_number)

        prepared_rows.append(
            {
                "unit": unit_value,
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


def _find_reference_plate_column(reference_df: pd.DataFrame) -> str:
    if reference_df.empty:
        return ""

    for column_name in reference_df.columns:
        normalized = _normalize_header_text(column_name)
        if normalized == "unit plate number":
            return column_name

    for column_name in reference_df.columns:
        normalized = _normalize_header_text(column_name)
        if "plate number" in normalized:
            return column_name

    return ""


def _build_reference_month_series(reference_df: pd.DataFrame) -> tuple[pd.Series, list[pd.Timestamp], str]:
    if reference_df.empty:
        return pd.Series(dtype="datetime64[ns]"), [], ""

    month_source_column = reference_df.columns[REFERENCE_MONTH_COL_INDEX]
    raw_values = reference_df[month_source_column].fillna("").astype(str).str.strip()
    # Credit form timestamps are in DD/MM/YYYY format, so parse day-first.
    parsed_dates = pd.to_datetime(raw_values, format="%d/%m/%Y %H:%M:%S", errors="coerce")
    parsed_dates = parsed_dates.fillna(pd.to_datetime(raw_values, format="%d/%m/%Y", errors="coerce"))
    parsed_dates = parsed_dates.fillna(pd.to_datetime(raw_values, dayfirst=True, errors="coerce"))
    month_series = parsed_dates.dt.to_period("M").dt.to_timestamp()
    month_options = sorted(month_series.dropna().unique().tolist(), reverse=True)
    return month_series, month_options, month_source_column


def _build_app_counts_hybrid_exact(
    reference_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    unit_column: str,
    plate_column: str,
) -> tuple[pd.Series, dict[str, int]]:
    summary_keys = summary_df["unit_key"].dropna().tolist()
    summary_key_set = set(summary_keys)
    plate_to_unit_key: dict[str, str] = {}
    for row in summary_df.itertuples(index=False):
        plate_key = getattr(row, "plate_key", "")
        if plate_key and plate_key not in plate_to_unit_key:
            plate_to_unit_key[plate_key] = getattr(row, "unit_key")

    app_counts: Counter[str] = Counter()
    qa_stats = {
        "total_apps": 0,
        "matched_by_plate": 0,
        "matched_by_unit_exact": 0,
        "unmatched": 0,
    }

    for _, row in reference_df.iterrows():
        raw_unit = str(row.get(unit_column, "") or "").strip()
        raw_plate = str(row.get(plate_column, "") or "").strip() if plate_column else ""
        unit_key = _normalize_unit_text(raw_unit)
        plate_key = _normalize_plate_text(raw_plate)

        if not unit_key and not plate_key:
            continue

        qa_stats["total_apps"] += 1
        matched_unit_key = None

        if plate_key:
            matched_unit_key = plate_to_unit_key.get(plate_key)
            if matched_unit_key:
                qa_stats["matched_by_plate"] += 1

        if not matched_unit_key and unit_key:
            matched_unit_key = unit_key if unit_key in summary_key_set else None
            if matched_unit_key:
                qa_stats["matched_by_unit_exact"] += 1

        if matched_unit_key:
            app_counts[matched_unit_key] += 1
        else:
            qa_stats["unmatched"] += 1

    return pd.Series(app_counts, dtype="int64"), qa_stats


def _inject_page_styles() -> None:
    st.markdown(
        """
        <style>
        .metric-card {
            border: 1px solid rgba(148, 163, 184, 0.35);
            border-radius: 12px;
            padding: 12px 14px;
            box-shadow: 0 3px 10px rgba(15, 23, 42, 0.08);
            min-height: 110px;
            margin-bottom: 0.6rem;
        }

        .metric-card.info {
            background: #dbeafe;
            border-color: #93c5fd;
        }

        .metric-card.good {
            background: #dcfce7;
            border-color: #86efac;
        }

        .metric-card.warn {
            background: #fef3c7;
            border-color: #fcd34d;
        }

        .metric-card.risk {
            background: #fee2e2;
            border-color: #fca5a5;
        }

        .metric-card-label {
            font-weight: 700;
            color: #0f172a;
            font-size: 0.84rem;
            margin-bottom: 0.35rem;
        }

        .metric-card-value {
            font-weight: 800;
            line-height: 1.1;
            color: #0f172a;
            font-size: 2rem;
            margin-bottom: 0.2rem;
        }

        .metric-card-sub {
            color: #334155;
            font-size: 0.84rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_stat_cards(items: list[dict], columns: int) -> None:
    cols = st.columns(columns)
    for i, item in enumerate(items):
        tone = item.get("tone", "info")
        label = item.get("label", "")
        value = item.get("value", "")
        sub = item.get("sub", "")
        with cols[i % columns]:
            st.markdown(
                f"""
                <div class="metric-card {tone}">
                    <div class="metric-card-label">{label}</div>
                    <div class="metric-card-value">{value}</div>
                    <div class="metric-card-sub">{sub}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_page() -> None:
    _inject_page_styles()

    summary_source = st.secrets["GOOGLE_SHEETS_SPREADSHEET_ID"]
    if not summary_source:
        st.error("Missing spreadsheet ID.")
        return

    summary_spreadsheet_id = normalize_spreadsheet_id(summary_source)

    try:
        summary_df = load_summary_credit_view(summary_spreadsheet_id)
        reference_df = load_first_sheet_dataframe(st.secrets["SECOND_SHEET_SPREADSHEET_ID"])
    except Exception as exc:
        st.error(f"Failed to load source data: {exc}")
        st.stop()

    if summary_df.empty:
        st.warning("No summary data available.")
        return

    month_series, month_options, month_source_column = _build_reference_month_series(reference_df)
    if month_options:
        month_filter_options: list[str | pd.Timestamp] = ["All months"] + month_options
        selected_month = st.selectbox(
            "Month",
            month_filter_options,
            index=0,
            format_func=lambda value: value if isinstance(value, str) else value.strftime("%b %Y"),
        )
        if isinstance(selected_month, pd.Timestamp):
            reference_df_filtered = reference_df[month_series == selected_month].copy()
        else:
            reference_df_filtered = reference_df

        st.caption(
            f"Month source column: {month_source_column}. "
            f"Rows used for matching: {len(reference_df_filtered)}"
        )
    else:
        reference_df_filtered = reference_df
        st.caption(
            f"Month filter unavailable: unable to parse dates from column {month_source_column or 'A'}. "
            f"Using all {len(reference_df_filtered)} credit application rows."
        )

    unit_applied_column = _find_reference_unit_applied_column(reference_df)
    if not unit_applied_column:
        st.warning("Unable to detect Unit Applied For column from reference data.")
        return

    plate_number_column = _find_reference_plate_column(reference_df)
    app_count_by_unit_key, qa_stats = _build_app_counts_hybrid_exact(
        reference_df=reference_df_filtered,
        summary_df=summary_df,
        unit_column=unit_applied_column,
        plate_column=plate_number_column,
    )

    plate_label = plate_number_column if plate_number_column else "none detected"
    st.title("CarMax Inventory Tracking")
    st.caption(
        "Revised view focused on lead coverage targets: each available unit should have at least 2 hot leads and no unit should remain at 0."
    )
    st.caption(
        f"Matching mode: plate-first + exact unit fallback | Unit column: {unit_applied_column} | Plate column: {plate_label}"
    )

    all_units_df = summary_df.copy()
    all_units_df["hot_leads"] = all_units_df["unit_key"].map(app_count_by_unit_key).fillna(0).astype(int)

    unit_level_df = all_units_df.drop_duplicates(subset=["unit_key"], keep="first").copy()
    total_units = len(unit_level_df)
    units_meeting_goal = int((unit_level_df["hot_leads"] >= 2).sum())
    units_with_1 = int((unit_level_df["hot_leads"] == 1).sum())
    units_with_0 = int((unit_level_df["hot_leads"] == 0).sum())
    units_with_credit_apps = int((unit_level_df["hot_leads"] > 0).sum())
    units_below_goal = int((unit_level_df["hot_leads"] < 2).sum())
    goal_coverage_pct = (units_meeting_goal / total_units * 100.0) if total_units else 0.0
    units_with_credit_apps_pct = (units_with_credit_apps / total_units * 100.0) if total_units else 0.0

    show_match_quality = st.toggle(
        "Show Match Quality (optional)",
        value=False,
        help="Show/hide data matching diagnostics. Keep hidden for management-focused view.",
    )
    if show_match_quality:
        total_apps = int(qa_stats.get("total_apps", 0))
        plate_pct = (qa_stats["matched_by_plate"] / total_apps * 100.0) if total_apps else 0.0
        fallback_pct = (qa_stats["matched_by_unit_exact"] / total_apps * 100.0) if total_apps else 0.0
        unmatched_pct = (qa_stats["unmatched"] / total_apps * 100.0) if total_apps else 0.0

        st.markdown("### Match Quality")
        _render_stat_cards(
            [
                {
                    "label": "Matched by Plate",
                    "value": qa_stats["matched_by_plate"],
                    "sub": f"{plate_pct:.1f}% of {total_apps} applications",
                    "tone": "good",
                },
                {
                    "label": "Matched by Unit (Exact)",
                    "value": qa_stats["matched_by_unit_exact"],
                    "sub": f"{fallback_pct:.1f}% of {total_apps} applications",
                    "tone": "warn",
                },
                {
                    "label": "Unmatched Applications",
                    "value": qa_stats["unmatched"],
                    "sub": f"{unmatched_pct:.1f}% of {total_apps} applications",
                    "tone": "risk",
                },
            ],
            columns=3,
        )

    st.markdown("### Lead Coverage Overview")
    _render_stat_cards(
        [
            {
                "label": "Total Available Inventory Units",
                "value": total_units,
                "sub": "All units currently tagged as Available",
                "tone": "info",
            },
            {
                "label": "Total Units with Credit Apps",
                "value": units_with_credit_apps,
                "sub": f"{units_with_credit_apps_pct:.1f}% of all available units",
                "tone": "info",
            },
            {
                "label": "Units Meeting Goal",
                "value": units_meeting_goal,
                "sub": "At least 2 hot leads",
                "tone": "good",
            },
            {
                "label": "Units Below Goal",
                "value": units_below_goal,
                "sub": "0 to 1 hot leads",
                "tone": "warn",
            },
            {
                "label": "Units with 0 Hot Leads",
                "value": units_with_0,
                "sub": "Priority follow-up list",
                "tone": "risk",
            },
            {
                "label": "Coverage Rate",
                "value": f"{goal_coverage_pct:.1f}%",
                "sub": "Units currently at >=2 hot leads",
                "tone": "info",
            },
        ],
        columns=3,
    )
    st.progress(goal_coverage_pct / 100.0, text=f"Coverage progress: {goal_coverage_pct:.1f}%")

    if units_with_0 == 0:
        st.success("Goal check: No inventory units are at 0 hot leads.")
    else:
        st.warning(f"Goal check: {units_with_0} units are still at 0 hot leads.")

    st.caption("Hot leads are currently proxied by matched credit-application rows.")

    st.markdown("### Lead Distribution")
    _render_stat_cards(
        [
            {
                "label": "Units with >3 Hot Leads",
                "value": int((unit_level_df["hot_leads"] > 3).sum()),
                "sub": "Strong demand signal",
                "tone": "good",
            },
            {
                "label": "Units with 2 Hot Leads",
                "value": int((unit_level_df["hot_leads"] == 2).sum()),
                "sub": "At goal threshold",
                "tone": "info",
            },
            {
                "label": "Units with 1 Hot Lead",
                "value": units_with_1,
                "sub": "Needs 1 more lead",
                "tone": "warn",
            },
            {
                "label": "Units with 0 Hot Leads",
                "value": units_with_0,
                "sub": "Critical gap",
                "tone": "risk",
            },
        ],
        columns=4,
    )

    below_goal_df = all_units_df[all_units_df["hot_leads"] < 2].copy()
    below_goal_df = below_goal_df.sort_values(
        by=["hot_leads", "aging", "unit", "plate_number"], ascending=[True, False, True, True]
    ).reset_index(drop=True)

    all_units_output = all_units_df.sort_values(
        by=["hot_leads", "unit", "plate_number"], ascending=[False, True, True]
    ).rename(
        columns={
            "plate_number": "plate number",
            "acquisition_cost": "acquisition cost",
            "target_selling_price": "target selling price",
        }
    )[
        [
            "unit",
            "plate number",
            "model",
            "acquisition cost",
            "target selling price",
            "aging",
            "hot_leads",
        ]
    ]

    tab1, tab2 = st.tabs(["Action List (Below Goal)", "All Available Inventory"])
    with tab1:
        if below_goal_df.empty:
            st.success("No units below goal. All inventory units have at least 2 hot leads.")
        else:
            st.markdown("#### Units Below Goal (0-1 hot leads)")
            below_goal_output = below_goal_df.rename(
                columns={
                    "plate_number": "plate number",
                    "acquisition_cost": "acquisition cost",
                    "target_selling_price": "target selling price",
                }
            )[
                [
                    "unit",
                    "plate number",
                    "model",
                    "acquisition cost",
                    "target selling price",
                    "aging",
                    "hot_leads",
                ]
            ]
            st.dataframe(
                below_goal_output,
                use_container_width=True,
                hide_index=True,
                column_config={"hot_leads": st.column_config.NumberColumn("hot leads")},
            )

    with tab2:
        st.dataframe(
            all_units_output,
            use_container_width=True,
            hide_index=True,
            column_config={"hot_leads": st.column_config.NumberColumn("hot leads")},
        )

    st.download_button(
        "Download CSV (All Units)",
        all_units_output.to_csv(index=False).encode("utf-8"),
        file_name="inventory_hot_leads_temp.csv",
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(page_title="Inventory Tracking Temp", layout="wide")
    render_page()


if __name__ == "__main__":
    main()


