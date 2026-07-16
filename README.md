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

- Guided upload slots for every source file
- GMT+6 conversion and date filtering
- PSP-specific approval rules
- ID, amount, currency and timestamp validation
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
2. Upload the relevant orchestrator file(s).
3. Upload all available PSP files.
4. Click **Run reconciliation**.
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
