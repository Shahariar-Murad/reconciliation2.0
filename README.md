# Payment Reconciliation Dashboard v2.5

This Streamlit dashboard now separates the complete payment flow into two independent workspaces:

1. **PSP → Orchestrator**
2. **Orchestrator → Backend API**

The separation keeps uploads, summaries, exceptions, source rows, file audits, and downloads organized instead of placing every reconciliation on one screen.

## Deployment files

Upload all files from this package together to the root of the GitHub repository:

- `app.py`
- `reconciliation_engine_v25.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `RECONCILIATION_LOGIC.md`
- `DEPLOYMENT_CHECKLIST.md`

Delete or ignore older engine files such as `reconciliation_engine.py` and `reconciliation_engine_v24.py`. Version 2.5 imports only `reconciliation_engine_v25.py`.

## Streamlit Cloud update steps

1. Extract this ZIP.
2. Replace `app.py` in the GitHub repository.
3. Upload `reconciliation_engine_v25.py` and the remaining package files.
4. Delete old engine files to avoid confusion.
5. Commit the changes.
6. In Streamlit Cloud, open **Manage app → Reboot app**.
7. Confirm the main file path is `app.py`.

## PSP → Orchestrator workspace

This workspace retains the existing bulk file detection and reconciliation logic for:

- Nuvei EU/AE, TrustPayment, Payabl, Paysafe, Unlimit, Paystra, Axcess, and PayPal → BridgerPay
- Dlocal, Skrill, and Paysafe Local → PayProcc

## Orchestrator → Backend API workspace

Upload the Backend API report together with available orchestrator reports:

- BridgerPay
- PayProcc
- Coinsbuy
- Confirmo
- ZEN

Backend business dates use **`Created At` converted from UTC+3 to GMT+6**. `Updated At` remains available for audit evidence but does not determine the daily population.

The backend workspace has individual tabs for each orchestrator, plus separate Overview, Exceptions, File Audit, and Logic Reference tabs.

## Backend-specific safeguards

- Coinsbuy deposits above 2,500 without a Tracking ID are excluded as internal transfers.
- ZEN includes only Apple Pay and Google Pay purchases. Plain card traffic remains under BridgerPay.
- The target orchestrator report is matched against the complete supplied backend file to avoid false next-day missing records.
- Backend rows created on the selected date but absent from a single-day orchestrator report are classified as adjacent-report checks.
