import re

import gspread
import pandas as pd
import streamlit as st

from shared import load_service_account_info, normalize_spreadsheet_id

TEAM_JAP = {
    "Angelo Hernaez",
    "Philip Estoya",
    "Chris Soriano",
    "Mary Ann Magsino",
}
TEAM_GEORGE = {
    "Clark Casetorno",
    "Howard Tindan",
    "Cheska Pavia",
    "Louis Gab Du",
    "Mark Castro",
}

TEAM_BY_AGENT = {agent: "Team Jap" for agent in TEAM_JAP} | {
    agent: "Team George" for agent in TEAM_GEORGE
}

AGENT_ALIASES = {
    "angelo": "Angelo Hernaez",
    "philip": "Philip Estoya",
    "pj": "Philip Estoya",
    "chris": "Chris Soriano",
    "mary ann": "Mary Ann Magsino",
    "mean": "Mary Ann Magsino",
    "clark": "Clark Casetorno",
    "howard": "Howard Tindan",
    "cheska": "Cheska Pavia",
    "louis": "Louis Gab Du",
    "gab": "Louis Gab Du",
    "mark": "Mark Castro",
}

_KNOWN_AGENTS = TEAM_JAP | TEAM_GEORGE
AGENT_LOOKUP = {agent.lower(): agent for agent in _KNOWN_AGENTS} | AGENT_ALIASES


def _safe_cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index].strip()


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _find_column_index(headers: list[str], targets: list[str]) -> int:
    normalized_headers = [_normalize_header(header) for header in headers]
    normalized_targets = [_normalize_header(target) for target in targets]
    for i, header in enumerate(normalized_headers):
        if header in normalized_targets:
            return i
    return -1


def _normalize_agent(raw_agent: str) -> str:
    clean = re.sub(r"\s+", " ", raw_agent.strip())
    if not clean:
        return "Unassigned"

    key = clean.lower()
    canonical = AGENT_LOOKUP.get(key)
    if canonical:
        return canonical
    return clean


def _derive_status(approved_raw: str, declined_raw: str) -> str:
    if approved_raw:
        return "Approved"
    if declined_raw:
        return "Declined"
    return "Pending"


def _is_released(release_raw: str) -> bool:
    clean = release_raw.strip().lower()
    if not clean:
        return False

    false_values = {"no", "n", "false", "0", "none", "not yet", "pending"}
    return clean not in false_values


def _build_notes(
    status: str,
    approved_raw: str,
    declined_raw: str,
    reason_raw: str,
    addtl_reqs_raw: str,
    initial_eval_raw: str,
    release_raw: str,
) -> str:
    notes: list[str] = []

    if status == "Approved" and approved_raw:
        notes.append(f"Approved via {approved_raw}")
    elif status == "Declined" and declined_raw:
        notes.append(f"Declined: {declined_raw}")

    for value in [reason_raw, addtl_reqs_raw, initial_eval_raw]:
        if value and value not in notes:
            notes.append(value)

    if release_raw and release_raw.lower() not in {"yes", "y", "true"}:
        notes.append(f"Release detail: {release_raw}")

    return " | ".join(notes)


@st.cache_resource
def _get_gspread_client() -> gspread.Client:
    service_account_info = load_service_account_info()
    return gspread.service_account_from_dict(service_account_info)


@st.cache_data(ttl=300)
def load_credit_application_status_df(spreadsheet_id: str) -> pd.DataFrame:
    client = _get_gspread_client()
    workbook = client.open_by_key(spreadsheet_id)
    worksheet = workbook.get_worksheet(0)
    values = worksheet.get_all_values()

    if len(values) <= 1:
        return pd.DataFrame(
            columns=[
                "Applicant Name",
                "Approved/Declined",
                "Released",
                "Date",
                "Notes/Remarks",
                "Agent Assigned",
                "Month",
                "Team",
            ]
        )

    header = values[0]
    rows = values[1:]

    timestamp_idx = _find_column_index(header, ["Timestamp"])
    applicant_idx = _find_column_index(header, ["Principal Name"])
    agent_idx = _find_column_index(header, ["AGENT ASSIGNED"])
    approved_idx = _find_column_index(header, ["APPROVED"])
    declined_idx = _find_column_index(header, ["DECLINED"])
    reason_idx = _find_column_index(header, ["REASON"])
    addtl_reqs_idx = _find_column_index(header, ["ADDTL REQS NEEDED"])
    initial_eval_idx = _find_column_index(header, ["INITIAL EVAL RESULTS"])
    release_idx = _find_column_index(header, ["RELEASE"])

    prepared_rows: list[dict] = []
    for row in rows:
        applicant_name = _safe_cell(row, applicant_idx)
        if not applicant_name:
            continue

        approved_raw = _safe_cell(row, approved_idx)
        declined_raw = _safe_cell(row, declined_idx)
        release_raw = _safe_cell(row, release_idx)
        reason_raw = _safe_cell(row, reason_idx)
        addtl_reqs_raw = _safe_cell(row, addtl_reqs_idx)
        initial_eval_raw = _safe_cell(row, initial_eval_idx)

        status = _derive_status(approved_raw, declined_raw)
        released = _is_released(release_raw)

        timestamp_value = _safe_cell(row, timestamp_idx)
        parsed_date = pd.to_datetime(timestamp_value, dayfirst=True, errors="coerce")
        date_value = parsed_date.strftime("%Y-%m-%d") if pd.notna(parsed_date) else ""
        month_value = parsed_date.strftime("%Y-%m") if pd.notna(parsed_date) else "Unknown"

        agent_assigned = _normalize_agent(_safe_cell(row, agent_idx))
        team = TEAM_BY_AGENT.get(agent_assigned, "Unassigned")

        notes = _build_notes(
            status=status,
            approved_raw=approved_raw,
            declined_raw=declined_raw,
            reason_raw=reason_raw,
            addtl_reqs_raw=addtl_reqs_raw,
            initial_eval_raw=initial_eval_raw,
            release_raw=release_raw,
        )

        prepared_rows.append(
            {
                "Applicant Name": applicant_name,
                "Approved/Declined": status,
                "Released": released,
                "Date": date_value,
                "Notes/Remarks": notes,
                "Agent Assigned": agent_assigned,
                "Month": month_value,
                "Team": team,
            }
        )

    if not prepared_rows:
        return pd.DataFrame(
            columns=[
                "Applicant Name",
                "Approved/Declined",
                "Released",
                "Date",
                "Notes/Remarks",
                "Agent Assigned",
                "Month",
                "Team",
            ]
        )

    df = pd.DataFrame(prepared_rows)
    return df.sort_values(by=["Date", "Applicant Name"], ascending=[False, True]).reset_index(drop=True)


def render() -> None:
    st.title("CarMax Credit Application Status")

    try:
        spreadsheet_id = normalize_spreadsheet_id(st.secrets["SECOND_SHEET_SPREADSHEET_ID"])
        df = load_credit_application_status_df(spreadsheet_id)
    except Exception as exc:
        st.error(f"Failed to load credit application responses: {exc}")
        st.stop()

    if df.empty:
        st.info("No credit application records found.")
        return

    teams = ["All", "Team George", "Team Jap", "Unassigned"]
    months = ["All"] + sorted([month for month in df["Month"].unique() if month], reverse=True)
    statuses = ["All", "Approved", "Declined"]

    c1, c2, c3, c4 = st.columns(4)
    selected_team = c1.selectbox("Team", teams, index=0)
    selected_month = c2.selectbox("Month Selection", months, index=0)
    selected_status = c3.selectbox("Approved/Declined", statuses, index=0)

    team_filtered_df = df.copy()
    if selected_team != "All":
        team_filtered_df = team_filtered_df[team_filtered_df["Team"] == selected_team]

    agent_options = ["All"] + sorted(team_filtered_df["Agent Assigned"].dropna().unique().tolist())
    selected_agent = c4.selectbox("Agent Assigned", agent_options, index=0)

    filtered_df = team_filtered_df.copy()
    if selected_month != "All":
        filtered_df = filtered_df[filtered_df["Month"] == selected_month]
    if selected_status != "All":
        filtered_df = filtered_df[filtered_df["Approved/Declined"] == selected_status]
    if selected_agent != "All":
        filtered_df = filtered_df[filtered_df["Agent Assigned"] == selected_agent]

    total_apps = int(len(filtered_df))
    total_approved = int((filtered_df["Approved/Declined"] == "Approved").sum())
    total_denied = int((filtered_df["Approved/Declined"] == "Declined").sum())
    total_released = int(filtered_df["Released"].sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total No. of Applications", total_apps)
    m2.metric("No. of Approved", total_approved)
    m3.metric("No. of Denied", total_denied)
    m4.metric("No. of Released", total_released)

    output_columns = [
        "Applicant Name",
        "Approved/Declined",
        "Released",
        "Date",
        "Notes/Remarks",
        "Agent Assigned",
    ]
    output_df = filtered_df[output_columns].reset_index(drop=True)

    st.dataframe(
        output_df,
        use_container_width=True,
        column_config={
            "Released": st.column_config.CheckboxColumn(
                "Released",
                help="Checked when release status is marked in the source form response row.",
            )
        },
    )

    st.download_button(
        "Download CSV",
        output_df.to_csv(index=False).encode("utf-8"),
        file_name="credit_application_status.csv",
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(page_title="Credit Application Status", layout="wide")
    render()


if __name__ == "__main__":
    main()
