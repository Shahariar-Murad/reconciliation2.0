# Payment Reconciliation Dashboard v2.7

This package contains two reconciliation workspaces:

1. PSP → Orchestrator
2. Orchestrator → Backend API

## v2.7 changes

- One shared **Reconciliation date or date range (GMT+6)** filter is used for both workspaces.
- A single day can be reconciled by selecting one date or the same start and end date.
- Multiple days can be reconciled by selecting the first and last date, for example **17 July to 22 July**.
- PSP → Orchestrator now supports multi-day reconciliation, not only one day.
- Orchestrator → Backend continues to support multi-day reconciliation.
- Every selected GMT+6 date is calculated independently and displayed with its own date.
- Both workspaces provide a detailed-date selector for route-level review.
- Changing the shared date range, uploaded files, or amount tolerance hides stale results until the relevant reconciliation is run again.
- Each stage exports one Excel workbook containing all dates in the selected range.
- Backend daily selection continues to use `Created At` converted from UTC+3 to GMT+6. `Updated At` remains audit-only.

## Files

- `app.py`
- `reconciliation_engine_v27.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `RECONCILIATION_LOGIC.md`
- `DEPLOYMENT_CHECKLIST.md`

## Deployment

Upload `app.py` and `reconciliation_engine_v27.py` from this same package to the application root, replace the previous versions, remove older reconciliation-engine files, and reboot the Streamlit app.
