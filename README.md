# Payment Reconciliation Dashboard

A Streamlit dashboard for reconciling approved PSP transactions against BridgerPay and PayProcc in **GMT+6 (Asia/Dhaka)**.

## Supported routes

### BridgerPay

- Nuvei EU (`SafeCharge-CreditCard-MID-EU`)
- Nuvei AE (`SafeCharge-CreditCard-MID-AE`)
- TrustPayment
- Payabl (`Innatech` in BridgerPay)
- Paysafe (`BP_` merchant transaction IDs)
- Unlimit (`CardPay` in BridgerPay)
- Paystra (`fundednext.com - PS`)
- Axcess (`fundednext.com - 3DS`)
- PayPal

### PayProcc

- Dlocal
- Skrill
- Paysafe Local (non-`BP_` merchant transaction IDs)

## Core features

- One multi-file uploader for all PSP and orchestrator reports
- GMT+6 conversion and date filtering
- PSP-specific approval rules
- Order/reference, amount and currency validation
- Route-level summary table with matched, unmatched, order-mismatch, amount-mismatch and currency-mismatch counts
- GMT+6 timestamps retained as audit evidence without creating time-mismatch exceptions
- Duplicate/blank matching-key detection
- Consolidated summary and exception review
- Downloadable Excel evidence pack and exception CSV
- Files remain in the active Streamlit session only

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy with GitHub and Streamlit Community Cloud

1. Create a new GitHub repository.
2. Upload all files from this project folder, including the `.streamlit` directory.
3. In Streamlit Community Cloud, select **Create app**.
4. Choose the repository, branch, and `app.py` as the main file.
5. Deploy.

No secrets or database configuration are required.

## Daily workflow

1. Select the reconciliation date in GMT+6.
2. Click the single upload field and select all available PSP and orchestrator reports together.
3. Click **Detect files and run reconciliation**.
4. Review the automatic file-mapping table.
5. Review routes marked `REVIEW REQUIRED` or `MATCHED WITH AMOUNT VARIANCES`.
6. Download the consolidated Excel report as audit evidence.

## Important file/timezone assumptions

- BridgerPay raw timestamps are converted from UTC to GMT+6 before the selected date is applied.
- PayProcc report timestamps are treated as GMT+6.
- Nuvei, TrustPayment, Unlimit and Axcess/Paystra reports are treated as GMT+6/as exported.
- Payabl: UTC+2 → GMT+6 (+4 hours).
- Paysafe: GMT+0 → GMT+6 (+6 hours).
- PayPal: UTC-7 → GMT+6 (+13 hours).
- Dlocal: `Validated date`, GMT+0 → GMT+6 (+6 hours).
- Skrill: July CET-labelled report is treated as CEST/UTC+2 → GMT+6 (+4 hours).

## Privacy

The app does not intentionally persist uploaded files. Streamlit Community Cloud may keep session data temporarily while the session is active; follow your organization's data-handling policy before uploading sensitive production reports.

## Version 2.3 bulk upload

- Replaces the individual upload slots with one multi-file uploader.
- Detects BridgerPay, PayProcc and PSP reports from their exported columns.
- Separates Nuvei EU and AE automatically by matching SafeCharge transaction IDs to the BridgerPay EU/AE MID aliases.
- Displays an upload-mapping table and includes it in the consolidated Excel report.
- Duplicate report types are handled by selecting the file with the most approved rows for the chosen date and clearly marking the other file as ignored.

## Version 2.2 fix

- Prevents `KeyError: Unmatched` after redeployment by clearing incompatible generated session results and safely normalizing legacy summary dictionaries.
- Currency mismatch is displayed in the main summary table and in each route's KPI cards.
- Timestamp differences remain audit-only and do not affect mismatch counts or route status.
