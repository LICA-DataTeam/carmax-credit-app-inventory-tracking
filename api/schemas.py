from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"


class OverviewResponse(BaseModel):
    selected_month: str
    month_options: list[str]
    month_source_column: str
    reference_rows_used: int
    summary_rows_used: int
    metrics: dict[str, Any]
    match_quality: dict[str, int] | None = None


class UnitsResponse(BaseModel):
    selected_month: str
    month_options: list[str]
    total_count: int
    filters: dict[str, Any]
    rows: list[dict[str, Any]] = Field(default_factory=list)
