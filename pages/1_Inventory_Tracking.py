import pandas as pd
import streamlit as st

from shared import InventoryTrackingConfig
from services import apply_table_controls, compute_inventory_tracking_data, format_units_output
from services.inventory_tracking_service import InventoryComputationResult


@st.cache_data(ttl=300)
def _load_inventory_snapshot(selected_month: str | None) -> InventoryComputationResult:
    config = InventoryTrackingConfig.from_streamlit_runtime()
    return compute_inventory_tracking_data(config=config, selected_month=selected_month)


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

    try:
        base_result = _load_inventory_snapshot(None)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to load source data: {exc}")
        st.stop()

    if base_result.summary_df.empty:
        st.warning("No summary data available.")
        return

    month_options = base_result.month_options
    result = base_result

    if month_options:
        month_filter_options: list[str | pd.Timestamp] = ["All months"] + month_options
        selected_month = st.selectbox(
            "Month",
            month_filter_options,
            index=0,
            format_func=lambda value: value if isinstance(value, str) else value.strftime("%b %Y"),
        )

        if isinstance(selected_month, pd.Timestamp):
            selected_month_key = selected_month.strftime("%Y-%m")
            try:
                result = _load_inventory_snapshot(selected_month_key)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Failed to load source data: {exc}")
                st.stop()
    else:
        st.caption(
            f"Month filter unavailable: unable to parse dates from column {base_result.month_source_column or 'A'}. "
            f"Using all {len(base_result.reference_df_filtered)} credit application rows."
        )

    if not result.unit_applied_column:
        st.warning("Unable to detect Unit Applied For column from reference data.")
        return

    st.title("CarMax Inventory Tracking")

    all_units_df = result.all_units_df.copy()
    below_goal_df = result.below_goal_df.copy()
    qa_stats = result.qa_stats
    metrics = result.metrics

    total_units = int(metrics["total_units"])
    total_new_units = int(metrics["total_new_units"])
    total_old_units = int(metrics["total_old_units"])
    units_with_credit_apps = int(metrics["units_with_credit_apps"])
    units_with_credit_apps_pct = float(metrics["units_with_credit_apps_pct"])
    units_meeting_goal = int(metrics["units_meeting_goal"])
    goal_coverage_pct = float(metrics["goal_coverage_pct"])
    new_units_breakdown = metrics["new_units_breakdown"]
    old_units_breakdown = metrics["old_units_breakdown"]
    old_units_with_apps = int(metrics["old_units_with_apps"])
    old_units_with_apps_pct = float(metrics["old_units_with_apps_pct"])
    new_units_with_apps = int(metrics["new_units_with_apps"])
    new_units_with_apps_pct = float(metrics["new_units_with_apps_pct"])

    old_units_with_0 = int(old_units_breakdown["0"])
    old_units_with_1 = int(old_units_breakdown["1"])
    old_units_with_2_plus = int(old_units_breakdown["2_plus"])
    new_units_with_0 = int(new_units_breakdown["0"])
    new_units_with_1 = int(new_units_breakdown["1"])
    new_units_with_2_plus = int(new_units_breakdown["2_plus"])

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

    st.markdown("## Inventory Overview")
    _render_stat_cards(
        [
            {
                "label": "Total Available Inventory Units",
                "value": total_units,
                "sub": "Unique available inventory units",
                "tone": "info",
            },
            {
                "label": "New Units",
                "value": total_new_units,
                "sub": "Aging <= 7 days",
                "tone": "info",
            },
            {
                "label": "Old Units",
                "value": old_units_with_0 + old_units_with_1 + old_units_with_2_plus,
                "sub": f"Should equal old units: {total_old_units}",
                "tone": "risk",
            },
        ],
        columns=3,
    )

    st.markdown("### CA Coverage Overview")
    _render_stat_cards(
        [
            {
                "label": "Units with Credit Apps",
                "value": units_with_credit_apps,
                "sub": f"{units_with_credit_apps_pct:.1f}% of all available units",
                "tone": "info",
            },
            {
                "label": "Coverage Rate",
                "value": f"{goal_coverage_pct:.1f}%",
                "sub": "Units currently at >= 2 CAs",
                "tone": "info",
            },
            {
                "label": "Old Units with CA",
                "value": old_units_with_apps,
                "sub": f"{old_units_with_apps_pct:.1f}% of old units",
                "tone": "warn",
            },
            {
                "label": "New Units with CA",
                "value": new_units_with_apps,
                "sub": f"{new_units_with_apps_pct:.1f}% of new units",
                "tone": "warn",
            },
        ],
        columns=2,
    )

    st.caption("CA and Cash counts are currently proxied by matched credit-application rows.")

    st.markdown("### New Units Breakdown")
    _render_stat_cards(
        [
            {
                "label": "New Units with 0 CA",
                "value": new_units_with_0,
                "sub": "Aging <= 7 and no applications",
                "tone": "warn",
            },
            {
                "label": "New Units with 1 CA",
                "value": new_units_with_1,
                "sub": "Aging <=7 and one application",
                "tone": "warn",
            },
            {
                "label": "New Units with 2+ CA",
                "value": new_units_with_2_plus,
                "sub": "Aging <= 7 and at least two applications",
                "tone": "good",
            },
        ],
        columns=3,
    )

    st.markdown("### Old Units Breakdown")
    _render_stat_cards(
        [
            {
                "label": "Old Units with 0 CA",
                "value": old_units_with_0,
                "sub": "Aging > 7 and no applications",
                "tone": "warn",
            },
            {
                "label": "Old Units with 1 CA",
                "value": old_units_with_1,
                "sub": "Aging > 7 and one application",
                "tone": "info",
            },
            {
                "label": "Old Units with 2+ CA",
                "value": old_units_with_2_plus,
                "sub": "Aging > 7 and at least two applications",
                "tone": "good",
            },
        ],
        columns=3,
    )

    model_options = result.model_options

    st.markdown("### Table Controls")
    f1, f2, f3, f4 = st.columns([1.3, 1.2, 1.0, 1.0])
    search_text = f1.text_input("Search unit/plate/model", value="")
    selected_models = f2.multiselect("Model", model_options, default=[])
    selected_ca_bucket = f3.selectbox("CA and Cash", ["All", "0", "1", "2+"], index=0)
    selected_aging_bucket = f4.selectbox("Aging Bucket", ["All", "New (<=7)", "Old (>7)"], index=0)

    s1, s2 = st.columns([1.2, 0.9])
    sort_map = {
        "CA and Cash": "ca_and_cash",
        "Aging": "aging_days",
        "Unit": "unit",
        "Plate Number": "plate_number",
        "Model": "model",
    }
    selected_sort_label = s1.selectbox("Sort By", list(sort_map.keys()), index=0)
    sort_desc = s2.toggle("Descending", value=True)
    sort_column = sort_map[selected_sort_label]

    all_units_filtered = apply_table_controls(
        all_units_df,
        search_text=search_text,
        model_filters=selected_models,
        ca_bucket=selected_ca_bucket,
        aging_bucket=selected_aging_bucket,
    )
    below_goal_filtered = apply_table_controls(
        below_goal_df,
        search_text=search_text,
        model_filters=selected_models,
        ca_bucket=selected_ca_bucket,
        aging_bucket=selected_aging_bucket,
    )

    all_units_output = format_units_output(
        all_units_filtered.sort_values(
            by=[sort_column, "unit", "plate_number"],
            ascending=[not sort_desc, True, True],
        )
    )

    tab1, tab2 = st.tabs(["Action List (Below Goal)", "All Available Inventory"])
    with tab1:
        if below_goal_filtered.empty:
            st.success("No old units below goal. All old units have at least 2 CA and Cash.")
        else:
            st.markdown("#### Old Units Below Goal (0-1 CA and Cash, aging > 7)")
            below_goal_output = format_units_output(
                below_goal_filtered.sort_values(
                    by=[sort_column, "unit", "plate_number"],
                    ascending=[not sort_desc, True, True],
                )
            )
            st.dataframe(
                below_goal_output,
                use_container_width=True,
                hide_index=True,
                column_config={"ca and cash": st.column_config.NumberColumn("ca and cash")},
            )

    with tab2:
        st.dataframe(
            all_units_output,
            use_container_width=True,
            hide_index=True,
            column_config={"ca and cash": st.column_config.NumberColumn("ca and cash")},
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
