from calendar import monthrange
from datetime import date

import pandas as pd
import streamlit as st
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
from google.oauth2 import service_account

from shared import load_service_account_info

BQ_VIEW = st.secrets["BQ_VIEW"]


def _month_label(value: date) -> str:
    return value.strftime("%b %Y")


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
    except Exception as exc:
        st.error(f"Failed to load filters from BigQuery view: {exc}")
        st.stop()

    if not month_options:
        st.warning("No data found in inquiry view.")
        return

    month_options = sorted(month_options)
    default_start_index = max(0, len(month_options) - 3)
    default_end_index = len(month_options) - 1

    c1, c2, c3, c4, c5 = st.columns([1.1, 1.1, 1.2, 1.5, 0.8])
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
        }
    )[
        [
            "rank",
            "unit",
            "make",
            "model",
            "year",
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
