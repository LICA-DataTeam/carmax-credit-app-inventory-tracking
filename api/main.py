import os
from functools import lru_cache
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query

from api.schemas import HealthResponse, OverviewResponse, UnitsResponse
from services import apply_table_controls, compute_inventory_tracking_data
from shared import InventoryTrackingConfig

SortBy = Literal["ca_and_cash", "aging_days", "unit", "plate_number", "model"]
CaBucket = Literal["All", "0", "1", "2+"]
AgingBucket = Literal["All", "New (<=7)", "Old (>7)"]

app = FastAPI(title="CarMax Inventory Tracking API", version="0.1.0")


@lru_cache(maxsize=1)
def _load_config() -> InventoryTrackingConfig:
    try:
        if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
            return InventoryTrackingConfig.from_env()
        return InventoryTrackingConfig.from_streamlit_runtime()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unable to load inventory config: {exc}") from exc


def _month_param_to_selected_month(month: str | None) -> str | None:
    if month is None:
        return None
    value = month.strip()
    if not value or value.lower() in {"all", "all months"}:
        return None
    return value


def _run_inventory_compute(month: str | None):
    selected_month = _month_param_to_selected_month(month)
    try:
        config = _load_config()
        return compute_inventory_tracking_data(config=config, selected_month=selected_month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load inventory data: {exc}") from exc


def _month_options_to_text(month_options: list[pd.Timestamp]) -> list[str]:
    return [month.strftime("%Y-%m") for month in month_options]


def _to_json_record(record: dict) -> dict:
    result = {}
    for key, value in record.items():
        if pd.isna(value):
            result[key] = None
        elif isinstance(value, (int, float, str, bool)) or value is None:
            result[key] = value
        else:
            # Handle numpy/pandas scalars safely.
            if hasattr(value, "item"):
                result[key] = value.item()
            else:
                result[key] = str(value)
    if result.get("ca_and_cash") is not None:
        result["ca_and_cash"] = int(result["ca_and_cash"])
    if result.get("aging_days") is not None:
        result["aging_days"] = float(result["aging_days"])
    return result


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/inventory/overview", response_model=OverviewResponse)
def inventory_overview(
    month: str | None = Query(default=None, description="Use YYYY-MM, 'all', or omit for all months."),
    include_match_quality: bool = Query(default=False),
) -> OverviewResponse:
    result = _run_inventory_compute(month=month)
    match_quality = result.qa_stats if include_match_quality else None
    return OverviewResponse(
        selected_month=result.selected_month_label,
        month_options=_month_options_to_text(result.month_options),
        month_source_column=result.month_source_column,
        reference_rows_used=len(result.reference_df_filtered),
        summary_rows_used=len(result.summary_df),
        metrics=result.metrics,
        match_quality=match_quality,
    )


@app.get("/inventory/units", response_model=UnitsResponse)
def inventory_units(
    month: str | None = Query(default=None, description="Use YYYY-MM, 'all', or omit for all months."),
    search: str = Query(default=""),
    models: list[str] = Query(default=[]),
    ca_bucket: CaBucket = Query(default="All"),
    aging_bucket: AgingBucket = Query(default="All"),
    sort_by: SortBy = Query(default="ca_and_cash"),
    desc: bool = Query(default=True),
    below_goal_only: bool = Query(default=False),
) -> UnitsResponse:
    result = _run_inventory_compute(month=month)

    base_df = result.below_goal_df if below_goal_only else result.all_units_df
    filtered = apply_table_controls(
        base_df,
        search_text=search,
        model_filters=models,
        ca_bucket=ca_bucket,
        aging_bucket=aging_bucket,
    )
    sorted_df = filtered.sort_values(
        by=[sort_by, "unit", "plate_number"],
        ascending=[not desc, True, True],
    )

    rows = [_to_json_record(record) for record in sorted_df.to_dict(orient="records")]

    return UnitsResponse(
        selected_month=result.selected_month_label,
        month_options=_month_options_to_text(result.month_options),
        total_count=len(rows),
        filters={
            "search": search,
            "models": models,
            "ca_bucket": ca_bucket,
            "aging_bucket": aging_bucket,
            "sort_by": sort_by,
            "desc": desc,
            "below_goal_only": below_goal_only,
        },
        rows=rows,
    )
