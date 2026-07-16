from __future__ import annotations

from datetime import date, timedelta
import hashlib

import pandas as pd
import streamlit as st

from reconciliation_engine import (
    auto_assign_uploaded_files,
    build_excel_report,
    exceptions_dataframe,
    run_all_reconciliations,
    summary_dataframe,
)

APP_SCHEMA_VERSION = "2.3"

# Clear only generated results when the dashboard structure changes. Uploaded
# files remain available in Streamlit's uploader widgets, while stale summary
# dictionaries cannot trigger KeyError after a deployment update.
if st.session_state.get("_app_schema_version") != APP_SCHEMA_VERSION:
    for state_key in (
        "recon_results",
        "file_audit",
        "recon_signature",
        "recon_date",
        "upload_mapping",
        "assigned_slots",
    ):
        st.session_state.pop(state_key, None)
    st.session_state["_app_schema_version"] = APP_SCHEMA_VERSION

st.set_page_config(
    page_title="Payment Reconciliation Dashboard",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .block-container {padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1600px;}
    .main-title {font-size: 2.05rem; font-weight: 750; color: #17365D; margin-bottom: .1rem;}
    .subtitle {color: #64748B; margin-bottom: 1.25rem;}
    .status-full {background:#E2F0D9; color:#375623; padding:.25rem .6rem; border-radius:999px; font-weight:700;}
    .status-review {background:#FCE4D6; color:#C00000; padding:.25rem .6rem; border-radius:999px; font-weight:700;}
    .status-variance {background:#FFF2CC; color:#7F6000; padding:.25rem .6rem; border-radius:999px; font-weight:700;}
    .status-empty {background:#E2E8F0; color:#475569; padding:.25rem .6rem; border-radius:999px; font-weight:700;}
    div[data-testid="stMetric"] {background:#F8FAFC; border:1px solid #E2E8F0; border-radius:12px; padding:12px;}
    .small-note {font-size:.88rem; color:#64748B;}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown('<div class="main-title">Payment Reconciliation Dashboard</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">PSP → Orchestrator reconciliation standardized to GMT+6, with exception review and downloadable evidence.</div>',
    unsafe_allow_html=True,
)


with st.sidebar:
    st.header("Run settings")
    selected_date = st.date_input("Reconciliation date (GMT+6)", value=date.today() - timedelta(days=1))
    amount_tolerance = st.number_input(
        "Amount tolerance",
        min_value=0.0,
        max_value=10.0,
        value=0.01,
        step=0.01,
        help="Amounts within this absolute difference are treated as matching.",
    )
    st.caption("All files are processed only in the current Streamlit session.")

    st.divider()
    st.subheader("Upload all reports")
    bulk_files = st.file_uploader(
        "Select all PSP and orchestrator files together",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        key="bulk_files",
        help=(
            "Select every available BridgerPay, PayProcc and PSP report in one action. "
            "The dashboard identifies each file from its columns and separates Nuvei EU/AE using SafeCharge IDs."
        ),
    )
    st.caption("You may upload only the files available for the day; incomplete routes are skipped automatically.")

    if bulk_files:
        with st.expander(f"Selected files ({len(bulk_files)})", expanded=False):
            for uploaded in bulk_files:
                st.write(f"• {uploaded.name}")

    run_clicked = st.button("Detect files and run reconciliation", type="primary", use_container_width=True)

uploaded_count = len(bulk_files or [])
ready_cols = st.columns(4)
ready_cols[0].metric("Files uploaded", uploaded_count)
ready_cols[1].metric("Target date", selected_date.strftime("%d %b %Y"))
ready_cols[2].metric("Timezone", "GMT+6")
ready_cols[3].metric("Amount tolerance", f"{amount_tolerance:.2f}")

# Create a stable run signature so downloads/results remain until inputs change.
def signature() -> str:
    digest = hashlib.sha256()
    digest.update(str(selected_date).encode())
    digest.update(str(amount_tolerance).encode())
    for uploaded in sorted(bulk_files or [], key=lambda item: (item.name, len(item.getvalue()))):
        digest.update(uploaded.name.encode())
        digest.update(uploaded.getvalue())
    return digest.hexdigest()

current_signature = signature()

if run_clicked:
    if uploaded_count == 0:
        st.warning("Upload at least one orchestrator file and its related PSP file.")
    else:
        with st.spinner("Detecting reports, applying GMT+6 rules and reconciling approved transactions…"):
            assigned_files, upload_mapping = auto_assign_uploaded_files(bulk_files, selected_date)
            results, file_audit = run_all_reconciliations(assigned_files, selected_date, amount_tolerance)
            st.session_state["recon_results"] = results
            st.session_state["file_audit"] = file_audit
            st.session_state["upload_mapping"] = upload_mapping
            st.session_state["assigned_slots"] = sorted(assigned_files)
            st.session_state["recon_signature"] = current_signature
            st.session_state["recon_date"] = selected_date

results = st.session_state.get("recon_results", [])
file_audit = st.session_state.get("file_audit", [])
upload_mapping = st.session_state.get("upload_mapping", [])
assigned_slots = st.session_state.get("assigned_slots", [])
results_are_current = st.session_state.get("recon_signature") == current_signature

if results and not results_are_current:
    st.info("The uploaded files or settings changed. Click **Detect files and run reconciliation** to refresh the results.")

if upload_mapping:
    with st.expander("Auto-detected file mapping", expanded=not results):
        mapping_df = pd.DataFrame(upload_mapping)
        st.dataframe(mapping_df, use_container_width=True, hide_index=True)
        needs_review = mapping_df[~mapping_df["Status"].isin(["Assigned", "Assigned by filename", "Assigned by elimination"])]
        if not needs_review.empty:
            st.warning("Some uploaded files were not assigned or were treated as duplicates. Review the mapping table above.")
        else:
            st.success(f"All {len(mapping_df)} uploaded files were assigned automatically.")

if not results:
    st.info(
        "Upload all available orchestrator and PSP files together, choose the GMT+6 date, and run the reconciliation. "
        "The dashboard detects file types automatically, and only routes with both required files are processed."
    )

    st.subheader("Built-in reconciliation logic")
    logic_preview = pd.DataFrame(
        [
            ["BridgerPay", "Nuvei EU/AE", "Nuvei Transaction ID = BP pspOrderId", "SafeCharge MID EU/AE"],
            ["BridgerPay", "TrustPayment", "Reference = BP pspOrderId", "Settle 0/100 + AUTH + Error 0"],
            ["BridgerPay", "Payabl", "Tx-Id = BP transactionId", "UTC+2 → GMT+6"],
            ["BridgerPay", "Paysafe", "Transaction ID = BP transactionId", "Merchant ID begins BP_"],
            ["BridgerPay", "Unlimit", "Payment ID = BP pspOrderId", "CardPay in BP"],
            ["BridgerPay", "Paystra/Axcess", "TransactionId = BP pspOrderId", "DB + ACK + 000.000.000"],
            ["BridgerPay", "PayPal", "Transaction ID = BP pspOrderId", "UTC-7 → GMT+6; Gross"],
            ["PayProcc", "Dlocal", "Reference = Gateway ID", "Validated date UTC → GMT+6"],
            ["PayProcc", "Skrill", "Reference = Gateway ID", "CEST UTC+2 → GMT+6"],
            ["PayProcc", "Paysafe Local", "Transaction ID = Gateway ID", "Non-BP_ rows"],
        ],
        columns=["Orchestrator", "PSP", "Primary match", "Filter/timezone"],
    )
    st.dataframe(logic_preview, use_container_width=True, hide_index=True)
    st.stop()

summary = summary_dataframe(results)
all_exceptions = exceptions_dataframe(results)

# KPI row.
processed = len(summary)
full_match = int(summary["Status"].eq("FULL MATCH").sum()) if not summary.empty else 0
review = int(summary["Status"].eq("REVIEW REQUIRED").sum()) if not summary.empty else 0
exception_count = len(all_exceptions)
matched_total = int(summary["Matched"].sum()) if "Matched" in summary else 0

kpis = st.columns(5)
kpis[0].metric("Routes processed", processed)
kpis[1].metric("Full match", full_match)
kpis[2].metric("Review required", review)
kpis[3].metric("Matched transactions", f"{matched_total:,}")
kpis[4].metric("Exception rows", f"{exception_count:,}")

# Prominent route-level summary requested for daily review.
st.subheader("PSP and orchestrator match summary")
summary_table_columns = [
    "PSP",
    "Orchestrator",
    "PSP Count",
    "Orchestrator Count",
    "Matched",
    "Unmatched",
    "Order Mismatch",
    "Amount Mismatch",
    "Currency Mismatch",
    "Status",
]
summary_table_available = [c for c in summary_table_columns if c in summary.columns]
summary_table = summary.sort_values(["Orchestrator", "PSP"])[summary_table_available]
st.dataframe(summary_table, use_container_width=True, hide_index=True)
st.caption(
    "Unmatched = PSP-only + orchestrator-only transactions. "
    "Order, amount, and currency mismatches are counted separately. "
    "Timestamp differences are shown only as audit evidence and are not counted as mismatches."
)

# Downloads.
report_bytes = build_excel_report(results, file_audit, st.session_state.get("recon_date", selected_date), upload_mapping)
download_cols = st.columns([1, 1, 2])
download_cols[0].download_button(
    "Download consolidated Excel",
    data=report_bytes,
    file_name=f"payment_reconciliation_{selected_date.isoformat()}_GMT6.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
if all_exceptions.empty:
    exception_csv = b"No exceptions found.\n"
else:
    exception_csv = all_exceptions.to_csv(index=False).encode("utf-8-sig")
download_cols[1].download_button(
    "Download exceptions CSV",
    data=exception_csv,
    file_name=f"reconciliation_exceptions_{selected_date.isoformat()}_GMT6.csv",
    mime="text/csv",
    use_container_width=True,
)
download_cols[2].caption("Excel includes the consolidated summary, file audit, all exceptions, and one detailed sheet per reconciliation route.")

status_order = {"REVIEW REQUIRED": 0, "MATCHED WITH AMOUNT VARIANCES": 1, "FULL MATCH": 2, "NO APPROVED DATA": 3}
summary_display = summary.copy()
summary_display["_order"] = summary_display["Status"].map(status_order).fillna(9)
summary_display = summary_display.sort_values(["_order", "Orchestrator", "PSP"]).drop(columns="_order")

overview_tab, bp_tab, pp_tab, exceptions_tab, audit_tab, logic_tab = st.tabs(
    ["Overview", "BridgerPay", "PayProcc", "Exceptions", "File audit", "Logic reference"]
)

with overview_tab:
    st.subheader("Reconciliation overview")
    columns = [
        "Orchestrator", "PSP", "Status", "PSP Count", "Orchestrator Count", "Matched",
        "Unmatched", "PSP Only", "Orchestrator Only", "Order Mismatch", "Amount Mismatch", "Currency Mismatch",
    ]
    available = [c for c in columns if c in summary_display.columns]
    st.dataframe(summary_display[available], use_container_width=True, hide_index=True)

    if not summary.empty:
        chart_df = summary.groupby("Status", as_index=False).size().rename(columns={"size": "Routes"})
        st.bar_chart(chart_df.set_index("Status"))

    st.subheader("Priority review")
    review_df = summary_display[summary_display["Status"].isin(["REVIEW REQUIRED", "MATCHED WITH AMOUNT VARIANCES"])]
    if review_df.empty:
        st.success("No routes require review.")
    else:
        st.dataframe(review_df[available], use_container_width=True, hide_index=True)


def status_badge(status: str) -> str:
    if status == "FULL MATCH":
        cls = "status-full"
    elif status == "MATCHED WITH AMOUNT VARIANCES":
        cls = "status-variance"
    elif status == "NO APPROVED DATA":
        cls = "status-empty"
    else:
        cls = "status-review"
    return f'<span class="{cls}">{status}</span>'


def summary_metric(result, key: str) -> int:
    """Read both current and legacy summary dictionaries safely."""
    data = result.summary or {}
    if key == "Unmatched":
        value = data.get(key)
        if value is None:
            value = (data.get("PSP Only", 0) or 0) + (data.get("Orchestrator Only", 0) or 0)
    else:
        value = data.get(key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def render_orchestrator(orchestrator: str):
    selected = [r for r in results if r.orchestrator == orchestrator]
    if not selected:
        st.info(f"No {orchestrator} routes were processed. Upload the orchestrator report and at least one related PSP file.")
        return
    for result in selected:
        with st.expander(f"{result.psp} — {result.status}", expanded=result.status != "FULL MATCH"):
            st.markdown(status_badge(result.status), unsafe_allow_html=True)
            cols = st.columns(7)
            cols[0].metric("PSP", f"{summary_metric(result, 'PSP Count'):,}")
            cols[1].metric("Orchestrator", f"{summary_metric(result, 'Orchestrator Count'):,}")
            cols[2].metric("Matched", f"{summary_metric(result, 'Matched'):,}")
            cols[3].metric("Unmatched", f"{summary_metric(result, 'Unmatched'):,}")
            cols[4].metric("Order mismatch", f"{summary_metric(result, 'Order Mismatch'):,}")
            cols[5].metric("Amount mismatch", f"{summary_metric(result, 'Amount Mismatch'):,}")
            cols[6].metric("Currency mismatch", f"{summary_metric(result, 'Currency Mismatch'):,}")

            if result.notes:
                st.caption(" • ".join(result.notes))

            detail_tab, exception_tab, source_tab, audit_subtab = st.tabs(["Reconciliation", "Exceptions", "Source rows", "Audit"])
            with detail_tab:
                st.dataframe(result.reconciliation, use_container_width=True, hide_index=True, height=420)
            with exception_tab:
                if result.exceptions.empty:
                    st.success("No exceptions for this route.")
                else:
                    st.dataframe(result.exceptions, use_container_width=True, hide_index=True, height=360)
            with source_tab:
                left, right = st.columns(2)
                with left:
                    st.markdown(f"**{result.psp} approved rows**")
                    st.dataframe(result.psp_source, use_container_width=True, hide_index=True, height=330)
                with right:
                    st.markdown(f"**{result.orchestrator} approved rows**")
                    st.dataframe(result.orchestrator_source, use_container_width=True, hide_index=True, height=330)
            with audit_subtab:
                st.json(result.audit)


with bp_tab:
    render_orchestrator("BridgerPay")

with pp_tab:
    render_orchestrator("PayProcc")

with exceptions_tab:
    st.subheader("All reconciliation exceptions")
    if all_exceptions.empty:
        st.success("No exceptions found in the processed routes.")
    else:
        orchestrators = ["All"] + sorted(all_exceptions["Orchestrator"].dropna().unique().tolist())
        selected_orch = st.selectbox("Filter orchestrator", orchestrators)
        filtered = all_exceptions if selected_orch == "All" else all_exceptions[all_exceptions["Orchestrator"] == selected_orch]
        st.dataframe(filtered, use_container_width=True, hide_index=True, height=520)

with audit_tab:
    st.subheader("Auto-detected upload mapping")
    if upload_mapping:
        st.dataframe(pd.DataFrame(upload_mapping), use_container_width=True, hide_index=True)
    else:
        st.info("Run the reconciliation to see automatic file assignments.")

    st.subheader("File readiness and parsing audit")
    audit_df = pd.DataFrame(file_audit)
    st.dataframe(audit_df, use_container_width=True, hide_index=True)
    error_rows = audit_df[audit_df["Status"].isin(["Error", "Reconciliation error"])] if not audit_df.empty else pd.DataFrame()
    if not error_rows.empty:
        st.error("One or more files could not be processed. Review the Error column above.")

with logic_tab:
    st.subheader("Configured business logic")
    logic_rows = []
    for result in results:
        logic_rows.append({
            "Orchestrator": result.orchestrator,
            "PSP": result.psp,
            "Status": result.status,
            "Notes": " | ".join(result.notes),
            "Amount tolerance": result.audit.get("Amount tolerance"),
            "Timestamp handling": result.audit.get("Timestamp comparison"),
        })
    st.dataframe(pd.DataFrame(logic_rows), use_container_width=True, hide_index=True)
    st.markdown(
        """
**Operational safeguards included**

- Only approved/successful payment records are reconciled; fee, refund, reversal, RG and CF lifecycle rows are excluded according to each PSP rule.
- Every source is converted or interpreted in GMT+6 before the selected date is applied. Timestamps remain visible for audit evidence but do not create mismatch counts.
- Duplicate or blank matching keys are isolated as exceptions instead of producing duplicate joins.
- Paysafe routing is split automatically: `BP_` merchant IDs go to BridgerPay; non-`BP_` IDs go to PayProcc.
- Dlocal amount variances are reported separately, so reference reconciliation is not confused with FX/gross-amount differences.
"""
    )
