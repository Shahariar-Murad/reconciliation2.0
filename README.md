# Payment Reconciliation Dashboard v2.6

This package contains two separate reconciliation workspaces:

1. PSP → Orchestrator
2. Orchestrator → Backend API

## v2.6 changes

- The backend workspace now supports an inclusive GMT+6 date range, such as 18 July to 22 July.
- Every date is reconciled independently; one day's result is never reused for another date.
- The backend overview displays one row per date and route.
- Route tabs include a detailed-date selector after the range reconciliation is completed.
- Changing the date range, files, or tolerance hides stale results until the reconciliation is run again.
- The backend Excel export contains the full selected date range in one workbook.
- Backend daily selection continues to use `Created At` converted from UTC+3 to GMT+6. `Updated At` remains audit-only.

## Files

- `app.py`
- `reconciliation_engine_v26.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `RECONCILIATION_LOGIC.md`
- `DEPLOYMENT_CHECKLIST.md`

## Deployment

Upload `app.py` and `reconciliation_engine_v26.py` from this same package to the application root, replace the previous versions, and reboot the Streamlit app. Delete or ignore older engine files so the deployment is unambiguous.
