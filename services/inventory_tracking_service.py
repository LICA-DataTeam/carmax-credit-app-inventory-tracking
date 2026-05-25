import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import gspread
import pandas as pd

from shared.inventory_config import InventoryTrackingConfig

STATUS_COL_INDEX = 8  # STATUS
PLATE_COL_INDEX = 6  # PLATE NO.
REFERENCE_MONTH_COL_INDEX = 0  # A (first column)


@dataclass(frozen=True)
class InventoryComputationResult:
    all_units_df: pd.DataFrame
    below_goal_df: pd.DataFrame
    summary_df: pd.DataFrame
    reference_df: pd.DataFrame
    reference_df_filtered: pd.DataFrame
    metrics: dict[str, Any]
    qa_stats: dict[str, int]
    month_options: list[pd.Timestamp]
    month_source_column: str
    unit_applied_column: str
    plate_number_column: str
    model_options: list[str]
    selected_month_label: str


def safe_cell(row: list[str], index: int) -> str:
    if index < len(row):
        return row[index].strip()
    return ""


def normalize_unit_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def normalize_plate_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower()).strip()


def normalize_header_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def is_available_status(value: str) -> bool:
    return value.strip().lower() == "available"


def parse_aging_days(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def apply_table_controls(
    df: pd.DataFrame,
    search_text: str,
    model_filters: list[str],
    ca_bucket: str,
    aging_bucket: str,
) -> pd.DataFrame:
    filtered = df.copy()

    if search_text:
        query = search_text.lower().strip()
        filtered = filtered[
            filtered["unit"].fillna("").str.lower().str.contains(query)
            | filtered["plate_number"].fillna("").str.lower().str.contains(query)
            | filtered["model"].fillna("").str.lower().str.contains(query)
        ]

    if model_filters:
        filtered = filtered[filtered["model"].isin(model_filters)]

    if ca_bucket == "0":
        filtered = filtered[filtered["ca_and_cash"] == 0]
    elif ca_bucket == "1":
        filtered = filtered[filtered["ca_and_cash"] == 1]
    elif ca_bucket == "2+":
        filtered = filtered[filtered["ca_and_cash"] >= 2]

    if aging_bucket == "New (<=7)":
        filtered = filtered[filtered["aging_days"] <= 7]
    elif aging_bucket == "Old (>7)":
        filtered = filtered[filtered["aging_days"] > 7]

    return filtered


def get_gspread_client(config: InventoryTrackingConfig) -> gspread.Client:
    return gspread.service_account_from_dict(config.service_account_info)


def load_first_sheet_dataframe(client: gspread.Client, spreadsheet_id: str) -> pd.DataFrame:
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.get_worksheet(0)
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    headers = [header if header else f"COL_{i + 1}" for i, header in enumerate(values[0])]
    rows = values[1:]
    selected_rows = [[safe_cell(row, i) for i in range(len(headers))] for row in rows]
    return pd.DataFrame(selected_rows, columns=headers)


def load_summary_credit_view(client: gspread.Client, config: InventoryTrackingConfig) -> pd.DataFrame:
    workbook = client.open_by_key(config.google_sheets_spreadsheet_id)
    worksheet = workbook.worksheet(config.sheet_name)
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
    prepared_rows: list[dict[str, str]] = []
    for row in rows:
        if not is_available_status(safe_cell(row, STATUS_COL_INDEX)):
            continue

        unit_parts = [safe_cell(row, idx) for idx in config.unit_col_indexes if safe_cell(row, idx)]
        unit_value = " ".join(unit_parts)
        unit_key = normalize_unit_text(unit_value)
        if not unit_key:
            continue

        plate_number = safe_cell(row, PLATE_COL_INDEX)
        plate_key = normalize_plate_text(plate_number)

        prepared_rows.append(
            {
                "unit": unit_value,
                "plate_number": plate_number,
                "model": safe_cell(row, config.summary_model_col_index),
                "acquisition_cost": safe_cell(row, config.summary_acquisition_col_index),
                "target_selling_price": safe_cell(row, config.summary_target_col_index),
                "aging": safe_cell(row, config.summary_aging_col_index),
                "unit_key": unit_key,
                "plate_key": plate_key,
            }
        )

    df = pd.DataFrame(prepared_rows)
    if df.empty:
        return df
    return df.drop_duplicates(subset=["unit_key", "plate_key"], keep="first").reset_index(drop=True)


def find_reference_unit_applied_column(reference_df: pd.DataFrame) -> str:
    if reference_df.empty:
        return ""

    for column_name in reference_df.columns:
        normalized = column_name.lower().strip()
        if "unit applied for" in normalized or "unit applied" in normalized:
            return column_name

    if len(reference_df.columns) >= 3:
        return reference_df.columns[2]
    return reference_df.columns[0]


def find_reference_plate_column(reference_df: pd.DataFrame) -> str:
    if reference_df.empty:
        return ""

    for column_name in reference_df.columns:
        normalized = normalize_header_text(column_name)
        if normalized == "unit plate number":
            return column_name

    for column_name in reference_df.columns:
        normalized = normalize_header_text(column_name)
        if "plate number" in normalized:
            return column_name

    return ""


def build_reference_month_series(reference_df: pd.DataFrame) -> tuple[pd.Series, list[pd.Timestamp], str]:
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


def build_app_counts_hybrid_exact(
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
        unit_key = normalize_unit_text(raw_unit)
        plate_key = normalize_plate_text(raw_plate)

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


def _resolve_selected_month(
    selected_month: str | pd.Timestamp | None,
    month_options: list[pd.Timestamp],
) -> tuple[pd.Timestamp | None, str]:
    if selected_month is None or selected_month == "All months":
        return None, "All months"

    if isinstance(selected_month, pd.Timestamp):
        return selected_month.normalize(), selected_month.strftime("%Y-%m")

    value = str(selected_month).strip()
    if not value or value.lower() == "all":
        return None, "All months"

    parsed = pd.to_datetime(value, format="%Y-%m", errors="coerce")
    if pd.isna(parsed):
        raise ValueError("selected_month must be YYYY-MM, pandas Timestamp, or 'All months'.")
    normalized = parsed.to_period("M").to_timestamp()
    if month_options and normalized not in month_options:
        raise ValueError(f"selected_month '{value}' is not available in reference data.")
    return normalized, normalized.strftime("%Y-%m")


def compute_inventory_tracking_data(
    config: InventoryTrackingConfig,
    selected_month: str | pd.Timestamp | None = None,
) -> InventoryComputationResult:
    client = get_gspread_client(config)
    summary_df = load_summary_credit_view(client, config)
    reference_df = load_first_sheet_dataframe(client, config.second_sheet_spreadsheet_id)

    month_series, month_options, month_source_column = build_reference_month_series(reference_df)
    selected_month_value, selected_month_label = _resolve_selected_month(selected_month, month_options)
    if selected_month_value is not None:
        reference_df_filtered = reference_df[month_series == selected_month_value].copy()
    else:
        reference_df_filtered = reference_df

    unit_applied_column = find_reference_unit_applied_column(reference_df)
    plate_number_column = find_reference_plate_column(reference_df)

    if summary_df.empty:
        all_units_df = pd.DataFrame()
        below_goal_df = pd.DataFrame()
        qa_stats = {"total_apps": 0, "matched_by_plate": 0, "matched_by_unit_exact": 0, "unmatched": 0}
        metrics = {
            "total_units": 0,
            "total_new_units": 0,
            "total_old_units": 0,
            "units_with_credit_apps": 0,
            "units_with_credit_apps_pct": 0.0,
            "units_meeting_goal": 0,
            "goal_coverage_pct": 0.0,
            "new_units_breakdown": {"0": 0, "1": 0, "2_plus": 0},
            "old_units_breakdown": {"0": 0, "1": 0, "2_plus": 0},
            "old_units_with_apps": 0,
            "old_units_with_apps_pct": 0.0,
            "new_units_with_apps": 0,
            "new_units_with_apps_pct": 0.0,
        }
        return InventoryComputationResult(
            all_units_df=all_units_df,
            below_goal_df=below_goal_df,
            summary_df=summary_df,
            reference_df=reference_df,
            reference_df_filtered=reference_df_filtered,
            metrics=metrics,
            qa_stats=qa_stats,
            month_options=month_options,
            month_source_column=month_source_column,
            unit_applied_column=unit_applied_column,
            plate_number_column=plate_number_column,
            model_options=[],
            selected_month_label=selected_month_label,
        )

    app_count_by_unit_key, qa_stats = build_app_counts_hybrid_exact(
        reference_df=reference_df_filtered,
        summary_df=summary_df,
        unit_column=unit_applied_column,
        plate_column=plate_number_column,
    )

    all_units_df = summary_df.copy()
    all_units_df["ca_and_cash"] = all_units_df["unit_key"].map(app_count_by_unit_key).fillna(0).astype(int)
    all_units_df["aging_days"] = all_units_df["aging"].map(parse_aging_days)

    row_level_df = all_units_df.copy()
    total_units = len(row_level_df)
    new_units_df = row_level_df[row_level_df["aging_days"] <= 7].copy()
    old_units_df = row_level_df[row_level_df["aging_days"] > 7].copy()
    units_meeting_goal = int((row_level_df["ca_and_cash"] >= 2).sum())
    goal_coverage_pct = (units_meeting_goal / total_units * 100.0) if total_units else 0.0
    units_with_credit_apps = int((row_level_df["ca_and_cash"] > 0).sum())
    units_with_credit_apps_pct = (units_with_credit_apps / total_units * 100.0) if total_units else 0.0

    total_new_units = int(len(new_units_df))
    total_old_units = int(len(old_units_df))
    new_units_with_0 = int((new_units_df["ca_and_cash"] == 0).sum())
    new_units_with_1 = int((new_units_df["ca_and_cash"] == 1).sum())
    new_units_with_2_plus = int((new_units_df["ca_and_cash"] >= 2).sum())
    new_units_with_apps = int((new_units_df["ca_and_cash"] > 0).sum())
    new_units_with_apps_pct = (new_units_with_apps / total_new_units * 100.0) if total_new_units else 0.0
    old_units_with_0 = int((old_units_df["ca_and_cash"] == 0).sum())
    old_units_with_1 = int((old_units_df["ca_and_cash"] == 1).sum())
    old_units_with_2_plus = int((old_units_df["ca_and_cash"] >= 2).sum())
    old_units_with_apps = int((old_units_df["ca_and_cash"] > 0).sum())
    old_units_with_apps_pct = (old_units_with_apps / total_old_units * 100.0) if total_old_units else 0.0

    below_goal_df = all_units_df[(all_units_df["aging_days"] > 7) & (all_units_df["ca_and_cash"] < 2)].copy()
    below_goal_df = below_goal_df.sort_values(
        by=["ca_and_cash", "aging", "unit", "plate_number"],
        ascending=[True, False, True, True],
    ).reset_index(drop=True)

    metrics = {
        "total_units": total_units,
        "total_new_units": total_new_units,
        "total_old_units": total_old_units,
        "units_with_credit_apps": units_with_credit_apps,
        "units_with_credit_apps_pct": units_with_credit_apps_pct,
        "units_meeting_goal": units_meeting_goal,
        "goal_coverage_pct": goal_coverage_pct,
        "new_units_breakdown": {"0": new_units_with_0, "1": new_units_with_1, "2_plus": new_units_with_2_plus},
        "old_units_breakdown": {"0": old_units_with_0, "1": old_units_with_1, "2_plus": old_units_with_2_plus},
        "old_units_with_apps": old_units_with_apps,
        "old_units_with_apps_pct": old_units_with_apps_pct,
        "new_units_with_apps": new_units_with_apps,
        "new_units_with_apps_pct": new_units_with_apps_pct,
    }

    model_options = sorted([m for m in all_units_df["model"].dropna().unique().tolist() if str(m).strip()])

    return InventoryComputationResult(
        all_units_df=all_units_df,
        below_goal_df=below_goal_df,
        summary_df=summary_df,
        reference_df=reference_df,
        reference_df_filtered=reference_df_filtered,
        metrics=metrics,
        qa_stats=qa_stats,
        month_options=month_options,
        month_source_column=month_source_column,
        unit_applied_column=unit_applied_column,
        plate_number_column=plate_number_column,
        model_options=model_options,
        selected_month_label=selected_month_label,
    )


def format_units_output(units_df: pd.DataFrame) -> pd.DataFrame:
    if units_df.empty:
        return pd.DataFrame(
            columns=[
                "unit",
                "plate number",
                "model",
                "acquisition cost",
                "target selling price",
                "aging",
                "ca and cash",
            ]
        )

    return units_df.rename(
        columns={
            "plate_number": "plate number",
            "acquisition_cost": "acquisition cost",
            "target_selling_price": "target selling price",
            "ca_and_cash": "ca and cash",
        }
    )[
        [
            "unit",
            "plate number",
            "model",
            "acquisition cost",
            "target selling price",
            "aging",
            "ca and cash",
        ]
    ]
