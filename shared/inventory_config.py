import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from shared.common import normalize_spreadsheet_id


def _normalize_service_account_info(info: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        # Inputs may contain literal "\n"; convert to real newlines.
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


def _parse_int_list(value: Any, field_name: str) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"Missing required list for {field_name}.")
        if text.startswith("["):
            items = json.loads(text)
        else:
            items = [part.strip() for part in text.split(",") if part.strip()]
    else:
        raise ValueError(f"Invalid value for {field_name}.")

    try:
        parsed = tuple(int(item) for item in items)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid integer list for {field_name}.") from exc

    if not parsed:
        raise ValueError(f"Empty list is not allowed for {field_name}.")
    return parsed


def _require(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required config key: {key}")
    return mapping[key]


@dataclass(frozen=True)
class InventoryTrackingConfig:
    google_sheets_spreadsheet_id: str
    second_sheet_spreadsheet_id: str
    sheet_name: str
    unit_col_indexes: tuple[int, ...]
    summary_model_col_index: int
    summary_acquisition_col_index: int
    summary_target_col_index: int
    summary_aging_col_index: int
    service_account_info: dict[str, Any]

    @classmethod
    def from_streamlit_secrets(cls, secrets: Mapping[str, Any]) -> "InventoryTrackingConfig":
        service_account_raw = _require(secrets, "gcp_service_account")
        return cls(
            google_sheets_spreadsheet_id=normalize_spreadsheet_id(
                str(_require(secrets, "GOOGLE_SHEETS_SPREADSHEET_ID"))
            ),
            second_sheet_spreadsheet_id=normalize_spreadsheet_id(
                str(_require(secrets, "SECOND_SHEET_SPREADSHEET_ID"))
            ),
            sheet_name=str(_require(secrets, "SHEET_NAME")).strip(),
            unit_col_indexes=_parse_int_list(_require(secrets, "UNIT_COL_INDEXES"), "UNIT_COL_INDEXES"),
            summary_model_col_index=int(_require(secrets, "SUMMARY_MODEL_COL_INDEX")),
            summary_acquisition_col_index=int(_require(secrets, "SUMMARY_ACQUISITION_COL_INDEX")),
            summary_target_col_index=int(_require(secrets, "SUMMARY_TARGET_COL_INDEX")),
            summary_aging_col_index=int(_require(secrets, "SUMMARY_AGING_COL_INDEX")),
            service_account_info=_normalize_service_account_info(dict(service_account_raw)),
        )

    @classmethod
    def from_streamlit_runtime(cls) -> "InventoryTrackingConfig":
        import streamlit as st

        return cls.from_streamlit_secrets(st.secrets)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "InventoryTrackingConfig":
        source = env or os.environ
        service_account_json = source.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not service_account_json:
            raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_JSON environment variable.")

        try:
            service_account_info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be valid JSON.") from exc

        return cls(
            google_sheets_spreadsheet_id=normalize_spreadsheet_id(
                str(_require(source, "GOOGLE_SHEETS_SPREADSHEET_ID"))
            ),
            second_sheet_spreadsheet_id=normalize_spreadsheet_id(
                str(_require(source, "SECOND_SHEET_SPREADSHEET_ID"))
            ),
            sheet_name=str(_require(source, "SHEET_NAME")).strip(),
            unit_col_indexes=_parse_int_list(_require(source, "UNIT_COL_INDEXES"), "UNIT_COL_INDEXES"),
            summary_model_col_index=int(_require(source, "SUMMARY_MODEL_COL_INDEX")),
            summary_acquisition_col_index=int(_require(source, "SUMMARY_ACQUISITION_COL_INDEX")),
            summary_target_col_index=int(_require(source, "SUMMARY_TARGET_COL_INDEX")),
            summary_aging_col_index=int(_require(source, "SUMMARY_AGING_COL_INDEX")),
            service_account_info=_normalize_service_account_info(service_account_info),
        )
