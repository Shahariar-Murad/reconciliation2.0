# Reconciliation Logic Reference — v2.6

All selected dates are evaluated in **GMT+6 (Asia/Dhaka)**.

# 1. PSP → Orchestrator

| Orchestrator | PSP | Approved transaction rule | Timezone handling | Primary match | Additional validation |
|---|---|---|---|---|---|
| BridgerPay | Nuvei EU | Approved Sale/Auth/Settle | Treated as GMT+6/as exported | Nuvei `Transaction ID` = BP `pspOrderId` | `Custom Data` = `merchantOrderId`; EU SafeCharge MID |
| BridgerPay | Nuvei AE | Approved Sale/Auth/Settle | Treated as GMT+6/as exported | Nuvei `Transaction ID` = BP `pspOrderId` | `Custom Data` = `merchantOrderId`; AE SafeCharge MID |
| BridgerPay | TrustPayment | Settle Status 0/100, AUTH, Error Code 0 | `Timestamp (BST)` treated as GMT+6 | `Reference` = `pspOrderId` | `Order Reference` = `merchantOrderId` |
| BridgerPay | Payabl | Successful Capture | UTC+2 → GMT+6 | `Tx-Id` = `transactionId` | `Order No.` = `pspOrderId` |
| BridgerPay | Paysafe | Completed authorization; Merchant ID begins `BP_` | UTC+0 → GMT+6 | `Transaction ID` = `transactionId` | Merchant ID = `merchantOrderId` |
| BridgerPay | Unlimit | Captured Payment | Report treated as GMT+6 | `Payment ID` = `pspOrderId` | BP PSP name `CardPay` |
| BridgerPay | Paystra | DB + ACK + `000.000.000` | GMT+6 | `TransactionId` = `pspOrderId` | PS channel and secondary IDs |
| BridgerPay | Axcess | DB + ACK + `000.000.000` | GMT+6 | `TransactionId` = `pspOrderId` | 3DS channel and secondary IDs |
| BridgerPay | PayPal | Express Checkout Payment, Completed, Credit | UTC−7 → GMT+6 | `Transaction ID` = `pspOrderId` | Compare Gross |
| PayProcc | Dlocal | PAID PAYMENT | UTC+0 → GMT+6 | `Reference` = `Gateway ID` | `Invoice` = `Payment Public ID` |
| PayProcc | Skrill | Processed Receive Money credit row | UTC+2 → GMT+6 | `Reference` = `Gateway ID` | Also validate Payment Public ID |
| PayProcc | Paysafe Local | Completed authorization; non-`BP_` Merchant ID | UTC+0 → GMT+6 | `Transaction ID` = `Gateway ID` | Merchant ID = Payment Public ID |

# 2. Orchestrator → Backend API

## Backend date rule

- Backend `Created At` is stored in UTC+3.
- Add 3 hours to obtain GMT+6.
- Apply the selected date after conversion.
- Backend `Updated At` is audit-only and does not determine the business date.

| Orchestrator | Backend gateway | Orchestrator rule | Orchestrator timezone | Primary match | Amount / additional rule |
|---|---|---|---|---|---|
| BridgerPay | `Bridger Pay` | `status = approved` | `processing_date` UTC+0 → GMT+6 | `merchantOrderId` = Backend `Transaction ID` | BP `amount` = Backend `Grand Total` |
| PayProcc | `Pay Procc` | `Type = sale`, `Status = success` | Report timestamp already GMT+6 | `Merchant Order ID` = Backend `Transaction ID` | USD `Amount`, otherwise USD `Applied Amount` |
| Coinsbuy | `Crypto` | Confirmed Deposit | `Created` UTC+0 → GMT+6 | Operation ID number = Backend `Transaction ID` | Compare `Amount × Rate`; validate Tracking ID |
| Confirmo | `Confirmo` | `Status = PAID` | `CreatedAt` UTC+0 → GMT+6 | `ID` = Backend `Transaction ID` | `Reference` = Backend `Tracking ID`; compare MerchantAmount |
| ZEN | `Zen Pay` | Purchase; Apple Pay or Google Pay | `created_at` UTC+0 → GMT+6 | `merchant_transaction_id` = Backend `Transaction ID` | Compare transaction amount; plain card excluded |

## Coinsbuy internal-transfer rule

Exclude a Coinsbuy deposit when both conditions are true:

- `Amount > 2,500`
- `Tracking ID` is blank

## Backend date-boundary classifications

- **MATCHED SAME CREATED DATE:** backend Created At is on the selected GMT+6 date.
- **MATCHED PRIOR BACKEND CREATED DATE:** the target orchestrator transaction exists in backend but backend Created At is earlier.
- **MATCHED NEXT BACKEND CREATED DATE:** the target orchestrator transaction exists in backend but backend Created At is later.
- **BACKEND MATCHED TO ADJACENT ORCHESTRATOR DATE:** a backend row created on the selected date belongs to another orchestrator business date found in the uploaded multi-day report.
- **BACKEND ONLY — ADJACENT REPORT NEEDED:** the backend row requires the adjacent-day orchestrator report for confirmation.
- **ORCHESTRATOR ONLY:** no matching Backend Transaction ID exists anywhere in the supplied backend file.

# Status definitions

- **FULL MATCH:** no true one-sided records and all configured amount, tracking/reference, and currency checks pass.
- **MATCHED WITH AMOUNT VARIANCES:** all required references match, with only an allowed amount-variance classification.
- **REVIEW REQUIRED:** true one-sided records, adjacent-report checks, amount mismatches, tracking/reference mismatches, currency mismatches, or duplicate keys exist.
- **NO APPROVED DATA:** neither side contains approved records for the selected route/date.


## Date-range execution

For the backend stage, each selected GMT+6 calendar date is reconciled independently. The range does not aggregate rows before matching. The overview then combines the daily summaries and includes the reconciliation date as a separate column. Changing the date range invalidates and hides earlier results until the user runs the reconciliation again.
