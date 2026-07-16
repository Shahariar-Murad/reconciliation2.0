# Reconciliation Logic Reference

All selected dates are evaluated in **GMT+6 (Asia/Dhaka)**.

| Orchestrator | PSP | Approved transaction rule | Timezone handling | Primary match | Additional validation |
|---|---|---|---|---|---|
| BridgerPay | Nuvei EU | Approved Sale/Auth/Settle | Treated as GMT+6/as exported | Nuvei `Transaction ID` = BP `pspOrderId` | `Custom Data` = `merchantOrderId`; MID `SafeCharge-CreditCard-MID-EU` |
| BridgerPay | Nuvei AE | Approved Sale/Auth/Settle | Treated as GMT+6/as exported | Nuvei `Transaction ID` = BP `pspOrderId` | `Custom Data` = `merchantOrderId`; MID `SafeCharge-CreditCard-MID-AE` |
| BridgerPay | TrustPayment | Settle Status 0/100, Request AUTH, Error Code 0 | `Timestamp (BST)` treated as Bangladesh Standard Time/GMT+6 | `Reference` = BP `pspOrderId` | `Order Reference` = `merchantOrderId` |
| BridgerPay | Payabl | Successful Capture | UTC+2 → GMT+6 (+4h) | `Tx-Id` = BP `transactionId` | `Order No.` = `pspOrderId`; BP PSP name `Innatech` |
| BridgerPay | Paysafe | Completed authorization with Merchant Transaction ID beginning `BP_` | GMT+0 → GMT+6 (+6h) | `Transaction ID` = BP `transactionId` | Merchant Transaction ID = `merchantOrderId` |
| BridgerPay | Unlimit | Captured Payment | Treated as GMT+6/as exported | `Payment ID` = BP `pspOrderId` | BP PSP name `CardPay` |
| BridgerPay | Paystra | DB + ACK + ReturnCode `000.000.000` | GMT+6 | `TransactionId` = BP `pspOrderId` | `InvoiceId` = `merchantOrderId`; `UniqueId` = `transactionId`; channel `fundednext.com - PS` |
| BridgerPay | Axcess | DB + ACK + ReturnCode `000.000.000` | GMT+6 | `TransactionId` = BP `pspOrderId` | `InvoiceId` = `merchantOrderId`; `UniqueId` = `transactionId`; channel `fundednext.com - 3DS` |
| BridgerPay | PayPal | Express Checkout Payment, Completed, Credit | UTC−7 → GMT+6 (+13h) | `Transaction ID` = BP `pspOrderId` | Also equals BP `transactionId`; compare PayPal `Gross` |
| PayProcc | Dlocal | PAID PAYMENT | `Validated date`, GMT+0 → GMT+6 (+6h) | `Reference` = `Gateway ID` | `Invoice` = `Payment Public ID`; amount variances flagged separately |
| PayProcc | Skrill | Processed Receive Money payment-credit row | July CET label treated as CEST/UTC+2 → GMT+6 (+4h) | `Reference` = `Gateway ID` | Also equals `Payment Public ID`; compare `[+]` USD credit |
| PayProcc | Paysafe Local | Completed authorization with Merchant Transaction ID not beginning `BP_` | GMT+0 → GMT+6 (+6h) | `Transaction ID` = `Gateway ID` | Merchant Transaction ID = `Payment Public ID`; use Applied Amount for USD reporting |

## Status definitions

- **FULL MATCH:** no one-sided transactions and all configured order/reference, amount and currency checks pass.
- **MATCHED WITH AMOUNT VARIANCES:** all transaction references exist on both sides, but Dlocal amount differences are present.
- **REVIEW REQUIRED:** one-sided transactions or other mismatches are present.
- **NO APPROVED DATA:** neither side contains approved transactions for that route/date.

## Dashboard counts

- **Matched:** the primary PSP and orchestrator transaction keys exist on both sides.
- **Unmatched:** PSP-only plus orchestrator-only transaction keys.
- **Order Mismatch:** a matched primary key has a mismatch in an additional order/reference field.
- **Amount Mismatch:** the PSP and orchestrator amounts differ beyond the selected tolerance.
- **Currency Mismatch:** the PSP and orchestrator transaction currencies differ after trimming and upper-case normalization.
- Timestamp differences remain visible in detailed evidence but are not counted as mismatches.
