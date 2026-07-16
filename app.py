from __future__ import annotations

from datetime import date, timedelta
import hashlib

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Payment Reconciliation Dashboard",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    from reconciliation_engine_v25 import (
        ENGINE_VERSION,
        auto_assign_backend_uploaded_files,
        auto_assign_uploaded_files,
        backend_exceptions_dataframe,
        backend_summary_dataframe,
        build_backend_excel_report,
        build_excel_report,
        exceptions_dataframe,
        run_all_reconciliations,
        run_backend_reconciliations,
        summary_dataframe,
    )
except (ImportError, ModuleNotFoundError) as exc:
    st.error(
        "The deployment files are incomplete or from different dashboard versions. "
        "Upload both app.py and reconciliation_engine_v25.py from the same v2.5 package, "
        "then reboot the Streamlit app."
    )
    st.code(f"Import details: {type(exc).__name__}: {exc}")
    st.stop()

APP_SCHEMA_VERSION = "2.5"
EXPECTED_ENGINE_VERSION = "2.5"
if ENGINE_VERSION != EXPECTED_ENGINE_VERSION:
    st.error(
        f"Dashboard/engine version mismatch: app {APP_SCHEMA_VERSION}, engine {ENGINE_VERSION}. "
        "Replace both files from the same package and reboot the app."
    )
    st.stop()

if st.session_state.get("_app_schema_version") != APP_SCHEMA_VERSION:
    for key in list(st.session_state.keys()):
        if key.startswith("psp_") or key.startswith("backend_") or key in {
            "recon_results", "file_audit", "recon_signature", "recon_date",
            "upload_mapping", "assigned_slots",
        }:
            st.session_state.pop(key, None)
    st.session_state["_app_schema_version"] = APP_SCHEMA_VERSION

st.markdown(
    """
<style>
    .block-container {padding-top: 1.35rem; padding-bottom: 3rem; max-width: 1650px;}
    .main-title {font-size: 2.05rem; font-weight: 760; color: #17365D; margin-bottom: .1rem;}
    .subtitle {color: #64748B; margin-bottom: 1.1rem;}
    .flow-title {font-size: 1.45rem; font-weight: 730; color: #17365D; margin-top: .15rem;}
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
    '<div class="subtitle">Two-stage payment flow: PSP → Orchestrator → Backend API. Each stage is separated into its own workspace.</div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Global settings")
    selected_date = st.date_input(
        "Reconciliation date (GMT+6)",
        value=date.today() - timedelta(days=1),
    )
    amount_tolerance = st.number_input(
        "Amount tolerance",
        min_value=0.0,
        max_value=10.0,
        value=0.01,
        step=0.01,
        help="Amounts within this absolute difference are treated as matching.",
    )
    st.divider()
    st.markdown("**Backend business-date rule**")
    st.caption("Backend `Created At` is converted from UTC+3 to GMT+6. `Updated At` is audit-only.")
    st.caption("Uploaded files and generated results remain only in the current Streamlit session.")


def files_signature(files, workspace: str) -> str:
    digest = hashlib.sha256()
    digest.update(workspace.encode())
    digest.update(str(selected_date).encode())
    digest.update(str(amount_tolerance).encode())
    for uploaded in sorted(files or [], key=lambda item: (item.name, len(item.getvalue()))):
        digest.update(uploaded.name.encode())
        digest.update(uploaded.getvalue())
    return digest.hexdigest()


def status_badge(status: str) -> str:
    if status == "FULL MATCH":
        css_class = "status-full"
    elif status == "MATCHED WITH AMOUNT VARIANCES":
        css_class = "status-variance"
    elif status == "NO APPROVED DATA":
        css_class = "status-empty"
    else:
        css_class = "status-review"
    return f'<span class="{css_class}">{status}</span>'


def safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def recognized_count(mapping: list[dict]) -> int:
    if not mapping:
        return 0
    return sum(1 for row in mapping if row.get("Status") in {"Assigned", "Assigned by filename", "Assigned by elimination"})


def render_mapping(mapping: list[dict], *, title: str = "Auto-detected file mapping") -> None:
    if not mapping:
        return
    with st.expander(title, expanded=False):
        mapping_df = pd.DataFrame(mapping)
        st.dataframe(mapping_df, use_container_width=True, hide_index=True)
        needs_review = mapping_df[~mapping_df["Status"].isin(["Assigned", "Assigned by filename", "Assigned by elimination"])]
        if needs_review.empty:
            st.success(f"All {len(mapping_df)} uploaded files were assigned automatically.")
        else:
            st.warning("Some files were unrecognized, duplicated, or require review. Check the mapping table.")


def render_psp_workspace() -> None:
    st.markdown('<div class="flow-title">PSP → Orchestrator</div>', unsafe_allow_html=True)
    st.caption("Existing PSP reconciliation workspace. Upload PSP reports together with BridgerPay and PayProcc reports.")

    upload_col, run_col = st.columns([4, 1])
    with upload_col:
        files = st.file_uploader(
            "Upload PSP and orchestrator reports",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="psp_bulk_files",
            help="The dashboard identifies reports from their columns and automatically separates Nuvei EU/AE routes.",
        )
    with run_col:
        st.write("")
        st.write("")
        run_clicked = st.button(
            "Run PSP reconciliation",
            type="primary",
            use_container_width=True,
            key="psp_run_button",
        )

    uploaded = len(files or [])
    current_signature = files_signature(files, "psp")

    if files:
        with st.expander(f"Selected PSP-stage files ({uploaded})", expanded=False):
            for item in files:
                st.write(f"• {item.name}")

    if run_clicked:
        if not files:
            st.warning("Upload at least one orchestrator file and its related PSP report.")
        else:
            with st.spinner("Detecting reports, applying GMT+6 rules, and reconciling PSP transactions…"):
                assigned, mapping = auto_assign_uploaded_files(files, selected_date)
                results, audit = run_all_reconciliations(assigned, selected_date, amount_tolerance)
                st.session_state["psp_results"] = results
                st.session_state["psp_audit"] = audit
                st.session_state["psp_mapping"] = mapping
                st.session_state["psp_signature"] = current_signature
                st.session_state["psp_date"] = selected_date

    results = st.session_state.get("psp_results", [])
    audit = st.session_state.get("psp_audit", [])
    mapping = st.session_state.get("psp_mapping", [])
    is_current = st.session_state.get("psp_signature") == current_signature

    if results and not is_current:
        st.info("The PSP-stage files or settings changed. Click **Run PSP reconciliation** to refresh.")

    render_mapping(mapping)

    summary = summary_dataframe(results)
    exceptions = exceptions_dataframe(results)

    metric_cols = st.columns(6)
    metric_cols[0].metric("Files uploaded", uploaded)
    metric_cols[1].metric("Recognized files", recognized_count(mapping))
    metric_cols[2].metric("Routes processed", len(summary))
    metric_cols[3].metric("Full match", int(summary["Status"].eq("FULL MATCH").sum()) if not summary.empty else 0)
    metric_cols[4].metric("Review required", int(summary["Status"].eq("REVIEW REQUIRED").sum()) if not summary.empty else 0)
    metric_cols[5].metric("Matched transactions", f"{int(summary['Matched'].sum()) if 'Matched' in summary else 0:,}")

    if not results:
        st.info("Upload the PSP-stage files and run the reconciliation. Only routes with both required sides are processed.")
        st.subheader("Configured PSP-stage routes")
        preview = pd.DataFrame(
            [
                ["Nuvei EU/AE", "BridgerPay", "Transaction ID = pspOrderId"],
                ["TrustPayment", "BridgerPay", "Reference = pspOrderId"],
                ["Payabl", "BridgerPay", "Tx-Id = transactionId"],
                ["Paysafe", "BridgerPay", "Transaction ID = transactionId"],
                ["Unlimit", "BridgerPay", "Payment ID = pspOrderId"],
                ["Paystra / Axcess", "BridgerPay", "TransactionId = pspOrderId"],
                ["PayPal", "BridgerPay", "Transaction ID = pspOrderId"],
                ["Dlocal", "PayProcc", "Reference = Gateway ID"],
                ["Skrill", "PayProcc", "Reference = Gateway ID"],
                ["Paysafe Local", "PayProcc", "Transaction ID = Gateway ID"],
            ],
            columns=["PSP", "Orchestrator", "Primary match"],
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)
        return

    st.subheader("PSP and orchestrator match summary")
    columns = [
        "PSP", "Orchestrator", "PSP Count", "Orchestrator Count", "Matched", "Unmatched",
        "Order Mismatch", "Amount Mismatch", "Currency Mismatch", "Status",
    ]
    available = [column for column in columns if column in summary.columns]
    st.dataframe(summary.sort_values(["Orchestrator", "PSP"])[available], use_container_width=True, hide_index=True)
    st.caption("Timestamp differences remain audit-only. Order, amount, and currency checks are counted separately.")

    report_bytes = build_excel_report(results, audit, st.session_state.get("psp_date", selected_date), mapping)
    download_cols = st.columns([1, 1, 2])
    download_cols[0].download_button(
        "Download PSP-stage Excel",
        data=report_bytes,
        file_name=f"psp_to_orchestrator_{selected_date.isoformat()}_GMT6.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    exception_csv = exceptions.to_csv(index=False).encode("utf-8-sig") if not exceptions.empty else b"No exceptions found.\n"
    download_cols[1].download_button(
        "Download exceptions CSV",
        data=exception_csv,
        file_name=f"psp_to_orchestrator_exceptions_{selected_date.isoformat()}_GMT6.csv",
        mime="text/csv",
        use_container_width=True,
    )
    download_cols[2].caption("The Excel file includes summary, upload mapping, file audit, exceptions, and route details.")

    status_order = {"REVIEW REQUIRED": 0, "MATCHED WITH AMOUNT VARIANCES": 1, "FULL MATCH": 2, "NO APPROVED DATA": 3}
    display = summary.copy()
    display["_order"] = display["Status"].map(status_order).fillna(9)
    display = display.sort_values(["_order", "Orchestrator", "PSP"]).drop(columns="_order")

    overview_tab, bp_tab, pp_tab, exception_tab, audit_tab, logic_tab = st.tabs(
        ["Overview", "BridgerPay", "PayProcc", "Exceptions", "File audit", "Logic reference"]
    )

    with overview_tab:
        overview_columns = [
            "Orchestrator", "PSP", "Status", "PSP Count", "Orchestrator Count", "Matched",
            "Unmatched", "PSP Only", "Orchestrator Only", "Order Mismatch", "Amount Mismatch", "Currency Mismatch",
        ]
        st.dataframe(display[[c for c in overview_columns if c in display.columns]], use_container_width=True, hide_index=True)
        review_df = display[display["Status"].isin(["REVIEW REQUIRED", "MATCHED WITH AMOUNT VARIANCES"])]
        st.subheader("Priority review")
        if review_df.empty:
            st.success("No PSP-stage routes require review.")
        else:
            st.dataframe(review_df[[c for c in overview_columns if c in review_df.columns]], use_container_width=True, hide_index=True)

    def render_psp_orchestrator(orchestrator: str) -> None:
        selected = [result for result in results if result.orchestrator == orchestrator]
        if not selected:
            st.info(f"No {orchestrator} routes were processed.")
            return
        for result in selected:
            with st.expander(f"{result.psp} — {result.status}", expanded=result.status != "FULL MATCH"):
                st.markdown(status_badge(result.status), unsafe_allow_html=True)
                values = result.summary or {}
                metrics = st.columns(7)
                metrics[0].metric("PSP", f"{safe_int(values.get('PSP Count')):,}")
                metrics[1].metric("Orchestrator", f"{safe_int(values.get('Orchestrator Count')):,}")
                metrics[2].metric("Matched", f"{safe_int(values.get('Matched')):,}")
                metrics[3].metric("Unmatched", f"{safe_int(values.get('Unmatched')):,}")
                metrics[4].metric("Order mismatch", f"{safe_int(values.get('Order Mismatch')):,}")
                metrics[5].metric("Amount mismatch", f"{safe_int(values.get('Amount Mismatch')):,}")
                metrics[6].metric("Currency mismatch", f"{safe_int(values.get('Currency Mismatch')):,}")
                if result.notes:
                    st.caption(" • ".join(result.notes))
                detail, exc, source, route_audit = st.tabs(["Reconciliation", "Exceptions", "Source rows", "Audit"])
                with detail:
                    st.dataframe(result.reconciliation, use_container_width=True, hide_index=True, height=430)
                with exc:
                    if result.exceptions.empty:
                        st.success("No exceptions for this route.")
                    else:
                        st.dataframe(result.exceptions, use_container_width=True, hide_index=True, height=380)
                with source:
                    left, right = st.columns(2)
                    with left:
                        st.markdown(f"**{result.psp} approved rows**")
                        st.dataframe(result.psp_source, use_container_width=True, hide_index=True, height=330)
                    with right:
                        st.markdown(f"**{result.orchestrator} approved rows**")
                        st.dataframe(result.orchestrator_source, use_container_width=True, hide_index=True, height=330)
                with route_audit:
                    st.json(result.audit)

    with bp_tab:
        render_psp_orchestrator("BridgerPay")
    with pp_tab:
        render_psp_orchestrator("PayProcc")
    with exception_tab:
        if exceptions.empty:
            st.success("No PSP-stage exceptions found.")
        else:
            options = ["All"] + sorted(exceptions["Orchestrator"].dropna().unique().tolist())
            selected_orchestrator = st.selectbox("Filter orchestrator", options, key="psp_exception_filter")
            filtered = exceptions if selected_orchestrator == "All" else exceptions[exceptions["Orchestrator"] == selected_orchestrator]
            st.dataframe(filtered, use_container_width=True, hide_index=True, height=540)
    with audit_tab:
        st.subheader("Upload mapping")
        st.dataframe(pd.DataFrame(mapping), use_container_width=True, hide_index=True)
        st.subheader("File readiness")
        st.dataframe(pd.DataFrame(audit), use_container_width=True, hide_index=True)
    with logic_tab:
        logic_rows = [{
            "Orchestrator": result.orchestrator,
            "PSP": result.psp,
            "Status": result.status,
            "Notes": " | ".join(result.notes),
            "Amount tolerance": result.audit.get("Amount tolerance"),
        } for result in results]
        st.dataframe(pd.DataFrame(logic_rows), use_container_width=True, hide_index=True)


def render_backend_workspace() -> None:
    st.markdown('<div class="flow-title">Orchestrator → Backend API</div>', unsafe_allow_html=True)
    st.caption("Upload the Backend API file together with available BridgerPay, PayProcc, Coinsbuy, Confirmo, and ZEN reports.")
    st.info("Backend daily selection uses **Created At**, converted from UTC+3 to GMT+6. Updated At is retained for audit only.")

    upload_col, run_col = st.columns([4, 1])
    with upload_col:
        files = st.file_uploader(
            "Upload backend and orchestrator reports",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="backend_bulk_files",
            help="The dashboard detects the Backend API file and each orchestrator report from exported columns.",
        )
    with run_col:
        st.write("")
        st.write("")
        run_clicked = st.button(
            "Run backend reconciliation",
            type="primary",
            use_container_width=True,
            key="backend_run_button",
        )

    uploaded = len(files or [])
    current_signature = files_signature(files, "backend")

    if files:
        with st.expander(f"Selected backend-stage files ({uploaded})", expanded=False):
            for item in files:
                st.write(f"• {item.name}")

    if run_clicked:
        if not files:
            st.warning("Upload the Backend API report and at least one orchestrator report.")
        else:
            with st.spinner("Detecting backend-stage reports and reconciling with Backend Created At…"):
                assigned, mapping = auto_assign_backend_uploaded_files(files, selected_date)
                results, audit = run_backend_reconciliations(assigned, selected_date, amount_tolerance)
                st.session_state["backend_results"] = results
                st.session_state["backend_audit"] = audit
                st.session_state["backend_mapping"] = mapping
                st.session_state["backend_signature"] = current_signature
                st.session_state["backend_date"] = selected_date

    results = st.session_state.get("backend_results", [])
    audit = st.session_state.get("backend_audit", [])
    mapping = st.session_state.get("backend_mapping", [])
    is_current = st.session_state.get("backend_signature") == current_signature

    if results and not is_current:
        st.info("The backend-stage files or settings changed. Click **Run backend reconciliation** to refresh.")

    render_mapping(mapping)

    summary = backend_summary_dataframe(results)
    exceptions = backend_exceptions_dataframe(results)

    total_orchestrator = int(summary["Orchestrator Count"].sum()) if "Orchestrator Count" in summary else 0
    total_matched = int(summary["Matched"].sum()) if "Matched" in summary else 0
    metric_cols = st.columns(6)
    metric_cols[0].metric("Files uploaded", uploaded)
    metric_cols[1].metric("Recognized files", recognized_count(mapping))
    metric_cols[2].metric("Routes processed", len(summary))
    metric_cols[3].metric("Orchestrator transactions", f"{total_orchestrator:,}")
    metric_cols[4].metric("Matched", f"{total_matched:,}")
    metric_cols[5].metric("Exception rows", f"{len(exceptions):,}")

    if not results:
        st.info("Upload the backend file and available orchestrator reports, then run this workspace.")
        st.subheader("Configured backend-stage routes")
        preview = pd.DataFrame(
            [
                ["BridgerPay", "Bridger Pay", "merchantOrderId = Backend Transaction ID", "amount = Grand Total"],
                ["PayProcc", "Pay Procc", "Merchant Order ID = Backend Transaction ID", "USD/Applied USD = Grand Total"],
                ["Coinsbuy", "Crypto", "Operation ID number = Backend Transaction ID", "Amount × Rate; internal transfers excluded"],
                ["Confirmo", "Confirmo", "ID = Backend Transaction ID", "Reference = Tracking ID"],
                ["ZEN", "Zen Pay", "merchant_transaction_id = Backend Transaction ID", "Apple Pay and Google Pay only"],
            ],
            columns=["Orchestrator", "Backend Gateway", "Primary match", "Additional rule"],
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)
        return

    st.subheader("Orchestrator and backend match summary")
    columns = [
        "Orchestrator", "Backend Gateway", "Orchestrator Count", "Backend Created-Date Count",
        "Matched", "Same Created Date", "Prior Backend Created Date", "Next Backend Created Date",
        "Orchestrator Only", "Backend Adjacent Matched", "Backend Adjacent Report Needed",
        "Amount Mismatch", "Amount Variance", "Tracking Mismatch", "Currency Mismatch", "Status",
    ]
    available = [column for column in columns if column in summary.columns]
    st.dataframe(summary[available], use_container_width=True, hide_index=True)
    st.caption(
        "The orchestrator report is the business-date anchor. Backend Created At selects backend daily rows. "
        "Adjacent-date matches are shown separately to prevent false missing-transaction counts."
    )

    report_bytes = build_backend_excel_report(results, audit, st.session_state.get("backend_date", selected_date), mapping)
    download_cols = st.columns([1, 1, 2])
    download_cols[0].download_button(
        "Download backend-stage Excel",
        data=report_bytes,
        file_name=f"orchestrator_to_backend_{selected_date.isoformat()}_GMT6.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    exception_csv = exceptions.to_csv(index=False).encode("utf-8-sig") if not exceptions.empty else b"No exceptions found.\n"
    download_cols[1].download_button(
        "Download exceptions CSV",
        data=exception_csv,
        file_name=f"orchestrator_to_backend_exceptions_{selected_date.isoformat()}_GMT6.csv",
        mime="text/csv",
        use_container_width=True,
    )
    download_cols[2].caption("The backend-stage Excel is separate from the PSP-stage workbook to keep the evidence organized.")

    overview_tab, bp_tab, pp_tab, cb_tab, confirmo_tab, zen_tab, exception_tab, audit_tab, logic_tab = st.tabs(
        ["Overview", "BridgerPay", "PayProcc", "Coinsbuy", "Confirmo", "ZEN", "Exceptions", "File audit", "Logic reference"]
    )

    with overview_tab:
        overview_columns = [
            "Orchestrator", "Backend Gateway", "Status", "Orchestrator Count", "Backend Created-Date Count",
            "Matched", "Unmatched", "Orchestrator Only", "Backend Adjacent Report Needed",
            "Raw Amount Differences", "Amount Mismatch", "Amount Variance", "Tracking Mismatch", "Currency Mismatch",
        ]
        st.dataframe(summary[[c for c in overview_columns if c in summary.columns]], use_container_width=True, hide_index=True)
        review = summary[summary["Status"].isin(["REVIEW REQUIRED", "MATCHED WITH AMOUNT VARIANCES"])]
        st.subheader("Priority review")
        if review.empty:
            st.success("No backend-stage routes require review.")
        else:
            st.dataframe(review[[c for c in overview_columns if c in review.columns]], use_container_width=True, hide_index=True)

    def render_backend_route(orchestrator: str) -> None:
        selected = [result for result in results if result.orchestrator == orchestrator]
        if not selected:
            st.info(f"No {orchestrator} route was processed. Upload both Backend API and {orchestrator} reports.")
            return
        result = selected[0]
        st.markdown(status_badge(result.status), unsafe_allow_html=True)
        values = result.summary or {}
        metrics = st.columns(8)
        metrics[0].metric("Orchestrator", f"{safe_int(values.get('Orchestrator Count')):,}")
        metrics[1].metric("Backend created date", f"{safe_int(values.get('Backend Created-Date Count')):,}")
        metrics[2].metric("Matched", f"{safe_int(values.get('Matched')):,}")
        metrics[3].metric("Orchestrator only", f"{safe_int(values.get('Orchestrator Only')):,}")
        metrics[4].metric("Adjacent check", f"{safe_int(values.get('Backend Adjacent Report Needed')):,}")
        metrics[5].metric("Amount mismatch", f"{safe_int(values.get('Amount Mismatch')):,}")
        metrics[6].metric("Tracking mismatch", f"{safe_int(values.get('Tracking Mismatch')):,}")
        metrics[7].metric("Currency mismatch", f"{safe_int(values.get('Currency Mismatch')):,}")
        if result.notes:
            st.caption(" • ".join(result.notes))

        detail, exc, sources, route_audit = st.tabs(["Reconciliation", "Exceptions", "Source rows", "Audit"])
        with detail:
            st.dataframe(result.reconciliation, use_container_width=True, hide_index=True, height=500)
        with exc:
            if result.exceptions.empty:
                st.success("No exceptions for this route.")
            else:
                st.dataframe(result.exceptions, use_container_width=True, hide_index=True, height=430)
        with sources:
            left, right = st.columns(2)
            with left:
                st.markdown(f"**{result.orchestrator} selected business-date rows**")
                st.dataframe(result.orchestrator_source, use_container_width=True, hide_index=True, height=360)
            with right:
                st.markdown("**Backend rows selected by Created At**")
                st.dataframe(result.backend_source, use_container_width=True, hide_index=True, height=360)
        with route_audit:
            st.json(result.audit)

    with bp_tab:
        render_backend_route("BridgerPay")
    with pp_tab:
        render_backend_route("PayProcc")
    with cb_tab:
        render_backend_route("Coinsbuy")
    with confirmo_tab:
        render_backend_route("Confirmo")
    with zen_tab:
        render_backend_route("ZEN")
    with exception_tab:
        if exceptions.empty:
            st.success("No backend-stage exceptions found.")
        else:
            options = ["All"] + sorted(exceptions["Orchestrator"].dropna().unique().tolist())
            selected_orchestrator = st.selectbox("Filter orchestrator", options, key="backend_exception_filter")
            filtered = exceptions if selected_orchestrator == "All" else exceptions[exceptions["Orchestrator"] == selected_orchestrator]
            st.dataframe(filtered, use_container_width=True, hide_index=True, height=560)
    with audit_tab:
        st.subheader("Upload mapping")
        st.dataframe(pd.DataFrame(mapping), use_container_width=True, hide_index=True)
        st.subheader("File readiness")
        st.dataframe(pd.DataFrame(audit), use_container_width=True, hide_index=True)
    with logic_tab:
        logic_rows = [{
            "Orchestrator": result.orchestrator,
            "Backend Gateway": result.backend_gateway,
            "Status": result.status,
            "Backend date field": result.audit.get("Backend business date field"),
            "Timezone": result.audit.get("Backend timezone conversion"),
            "Notes": " | ".join(result.notes),
        } for result in results]
        st.dataframe(pd.DataFrame(logic_rows), use_container_width=True, hide_index=True)
        st.markdown(
            """
**Backend-stage safeguards**

- Backend daily population is selected using `Created At` after UTC+3 → GMT+6 conversion.
- `Updated At` remains available for audit evidence but does not move a transaction into another business date.
- Orchestrator transactions are matched against the complete supplied backend file to avoid false next-day missing records.
- Coinsbuy deposits over 2,500 without a Tracking ID are excluded as internal transfers.
- ZEN includes only Apple Pay and Google Pay purchase channels; plain card traffic remains under BridgerPay.
"""
        )


psp_flow_tab, backend_flow_tab = st.tabs(["1. PSP → Orchestrator", "2. Orchestrator → Backend API"])
with psp_flow_tab:
    render_psp_workspace()
with backend_flow_tab:
    render_backend_workspace()
