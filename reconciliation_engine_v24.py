from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Any, Callable, Iterable
import csv

import numpy as np
import pandas as pd

GMT6 = "Asia/Dhaka"
ENGINE_VERSION = "2.4"


@dataclass
class ReconciliationResult:
    name: str
    orchestrator: str
    psp: str
    status: str
    summary: dict[str, Any]
    reconciliation: pd.DataFrame
    exceptions: pd.DataFrame
    psp_source: pd.DataFrame
    orchestrator_source: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _bytes(source: Any) -> bytes:
    if source is None:
        raise ValueError("No file supplied")
    if isinstance(source, bytes):
        return source
    if isinstance(source, bytearray):
        return bytes(source)
    if hasattr(source, "getvalue"):
        return source.getvalue()
    if hasattr(source, "read"):
        pos = source.tell() if hasattr(source, "tell") else None
        data = source.read()
        if pos is not None and hasattr(source, "seek"):
            source.seek(pos)
        return data
    raise TypeError(f"Unsupported file object: {type(source)!r}")


def read_csv_flexible(source: Any, preferred_sep: str | None = None) -> pd.DataFrame:
    data = _bytes(source)
    encodings = ("utf-8-sig", "utf-8", "latin1")
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            if preferred_sep:
                return pd.read_csv(BytesIO(data), encoding=encoding, sep=preferred_sep)
            sample = text[:8000]
            try:
                sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                sep = ","
            df = pd.read_csv(BytesIO(data), encoding=encoding, sep=sep)
            if df.shape[1] == 1 and sep != ";":
                df = pd.read_csv(BytesIO(data), encoding=encoding, sep=";")
            return df
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise ValueError(f"Could not read CSV: {last_error}")


def read_excel(source: Any, sheet_name: str | int | None = 0, header: int | None = 0) -> pd.DataFrame:
    return pd.read_excel(BytesIO(_bytes(source)), sheet_name=sheet_name, header=header, dtype=object)


def read_excel_detect_header(
    source: Any,
    required_columns: Iterable[str],
    sheet_name: str | int = 0,
    max_scan_rows: int = 40,
) -> pd.DataFrame:
    raw = read_excel(source, sheet_name=sheet_name, header=None)
    required = {str(c).strip().lower() for c in required_columns}
    for idx in range(min(max_scan_rows, len(raw))):
        values = {str(v).strip().lower() for v in raw.iloc[idx].tolist() if pd.notna(v)}
        if required.issubset(values):
            df = read_excel(source, sheet_name=sheet_name, header=idx)
            return df.dropna(how="all")
    raise ValueError(f"Could not detect header row containing: {sorted(required_columns)}")


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def require_columns(df: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{label}: missing required column(s): {', '.join(missing)}")


def string_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def numeric_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def local_date_mask(series: pd.Series, target_date: date) -> pd.Series:
    return series.dt.date == target_date


def to_gmt6_from_utc(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return parsed.dt.tz_convert(GMT6).dt.tz_localize(None)


def add_hours(series: pd.Series, hours: int, *, dayfirst: bool = False, fmt: str | None = None) -> pd.Series:
    if fmt:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
    else:
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=dayfirst)
    return parsed + pd.Timedelta(hours=hours)


def _norm_key(value: Any) -> str:
    if pd.isna(value):
        return ""
    value = str(value).strip()
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    return value


def _norm_currency(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip().upper()


def _safe_sheet_name(name: str) -> str:
    for ch in "[]:*?/\\":
        name = name.replace(ch, "-")
    return name[:31]


# ---------------------------------------------------------------------------
# Source parsers
# ---------------------------------------------------------------------------

def parse_bridgerpay(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["processing_date", "pspName", "transactionId", "pspOrderId", "merchantOrderId", "amount", "currency", "status", "midAlias"],
        "BridgerPay",
    )
    df["GMT+6 Timestamp"] = to_gmt6_from_utc(df["processing_date"])
    if "completionDate" in df.columns:
        df["GMT+6 Completion Timestamp"] = to_gmt6_from_utc(df["completionDate"])
    df = df[local_date_mask(df["GMT+6 Timestamp"], target_date)].copy()
    return df


def parse_payprocc(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Payment Public ID", "Merchant Order ID", "MID", "Transaction Date", "Type", "Status", "Amount", "Currency", "Gateway ID", "Transaction ID"],
        "PayProcc",
    )
    df["GMT+6 Timestamp"] = pd.to_datetime(df["Transaction Date"], format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
    df = df[local_date_mask(df["GMT+6 Timestamp"], target_date)].copy()
    return df


def parse_nuvei(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_excel_detect_header(source, ["Date", "Transaction ID", "Transaction Result"]))
    require_columns(df, ["Date", "Transaction ID", "Transaction Type", "Transaction Result", "Amount", "Currency", "Custom Data"], "Nuvei")
    df["GMT+6 Timestamp"] = pd.to_datetime(df["Date"], errors="coerce")
    approved = (
        string_series(df["Transaction Result"]).str.upper().eq("APPROVED")
        & string_series(df["Transaction Type"]).str.upper().isin(["SALE", "AUTH", "SETTLE"])
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_trustpayment(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_excel(source))
    require_columns(
        df,
        ["Reference", "Settle Status", "Error Code", "Authorised Amount", "Timestamp (BST)", "Currency", "Order Reference", "Request"],
        "TrustPayment",
    )
    # In this operational report, BST is Bangladesh Standard Time (GMT+6).
    df["GMT+6 Timestamp"] = pd.to_datetime(df["Timestamp (BST)"], errors="coerce")
    approved = (
        pd.to_numeric(df["Settle Status"], errors="coerce").isin([0, 100])
        & string_series(df["Request"]).str.upper().eq("AUTH")
        & pd.to_numeric(df["Error Code"], errors="coerce").eq(0)
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_payabl(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(df, ["Tx-Id", "Tx-Type", "Order No.", "Date", "Time", "Currency", "Amount", "Status"], "Payabl")
    combined = string_series(df["Date"]) + " " + string_series(df["Time"])
    df["GMT+6 Timestamp"] = add_hours(combined, 4, fmt="%d.%m.%Y %H:%M:%S")
    approved = (
        string_series(df["Status"]).str.upper().eq("SUCCESSFUL")
        & string_series(df["Tx-Type"]).str.upper().eq("CAPTURE")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_paysafe(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Transaction ID", "Merchant Transaction ID", "Transaction Date", "Transaction Time (GMT)", "Transaction Type", "Status", "Amount", "Currency"],
        "Paysafe",
    )
    combined = string_series(df["Transaction Date"]) + " " + string_series(df["Transaction Time (GMT)"])
    df["GMT+6 Timestamp"] = add_hours(combined, 6, fmt="%m-%d-%Y %I:%M:%S %p")
    approved = (
        string_series(df["Transaction Type"]).str.lower().eq("authorization")
        & string_series(df["Status"]).str.upper().eq("COMPLETED")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_unlimit(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_excel(source, sheet_name="Payments"))
    require_columns(df, ["Payment ID", "Amount", "CUR", "Status", "Order type", "Payment date"], "Unlimit")
    df["GMT+6 Timestamp"] = pd.to_datetime(df["Payment date"], errors="coerce")
    approved = (
        string_series(df["Status"]).str.upper().eq("CAPTURED")
        & string_series(df["Order type"]).str.upper().eq("PAYMENT")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_axcess_paystra(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source, preferred_sep=";"))
    require_columns(
        df,
        ["UniqueId", "PaymentType", "RequestTimestamp", "TransactionId", "ChannelName", "ReturnCode", "Credit", "Currency", "Result", "InvoiceId"],
        "Axcess/Paystra",
    )
    df["GMT+6 Timestamp"] = pd.to_datetime(df["RequestTimestamp"], errors="coerce")
    df["Reconciliation Amount"] = pd.to_numeric(
        string_series(df["Credit"]).str.replace(",", ".", regex=False), errors="coerce"
    )
    approved = (
        string_series(df["PaymentType"]).str.upper().eq("DB")
        & string_series(df["Result"]).str.upper().eq("ACK")
        & string_series(df["ReturnCode"]).eq("000.000.000")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_paypal(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Date", "Time", "Type", "Status", "Currency", "Gross", "Transaction ID", "Balance Impact"],
        "PayPal",
    )
    combined = string_series(df["Date"]) + " " + string_series(df["Time"])
    df["GMT+6 Timestamp"] = add_hours(combined, 13, fmt="%d/%m/%Y %H:%M:%S")
    approved = (
        string_series(df["Type"]).str.upper().eq("EXPRESS CHECKOUT PAYMENT")
        & string_series(df["Status"]).str.upper().eq("COMPLETED")
        & string_series(df["Balance Impact"]).str.upper().eq("CREDIT")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_dlocal(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Reference", "Invoice", "Validated date", "Balance currency", "Amount", "Status", "Transaction type"],
        "Dlocal",
    )
    df["GMT+6 Timestamp"] = add_hours(df["Validated date"], 6, fmt="%Y-%m-%d %H:%M:%S")
    approved = (
        string_series(df["Status"]).str.upper().eq("PAID")
        & string_series(df["Transaction type"]).str.upper().eq("PAYMENT")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


def parse_skrill(source: Any, target_date: date) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["ID", "Time (CET)", "Type", "Transaction Details", "[+]", "Status", "Reference", "Currency"],
        "Skrill",
    )
    # July is CEST (UTC+2); convert to GMT+6 by adding four hours.
    df["GMT+6 Timestamp"] = add_hours(df["Time (CET)"], 4, fmt="%d %b %y %H:%M")
    approved = (
        string_series(df["Type"]).str.upper().eq("RECEIVE MONEY")
        & string_series(df["Status"]).str.upper().eq("PROCESSED")
        & string_series(df["Reference"]).ne("")
        & pd.to_numeric(df["[+]"], errors="coerce").notna()
        & string_series(df["Transaction Details"]).str.lower().str.startswith("from ")
        & local_date_mask(df["GMT+6 Timestamp"], target_date)
    )
    return df[approved].copy()


# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------

def _dedupe(df: pd.DataFrame, key_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = df.copy()
    work["_match_key"] = work[key_col].map(_norm_key)
    blank = work["_match_key"].eq("")
    duplicates = work[work["_match_key"].duplicated(keep=False) | blank].copy()
    unique = work[~blank].drop_duplicates("_match_key", keep="first").copy()
    return unique, duplicates


def reconcile_frames(
    *,
    name: str,
    orchestrator: str,
    psp: str,
    psp_df: pd.DataFrame,
    orch_df: pd.DataFrame,
    psp_key: str,
    orch_key: str,
    psp_amount: str,
    orch_amount: str,
    psp_currency: str,
    orch_currency: str,
    psp_time: str = "GMT+6 Timestamp",
    orch_time: str = "GMT+6 Timestamp",
    secondary_checks: list[tuple[str, str, str]] | None = None,
    amount_tolerance: float = 0.01,
    time_tolerance_seconds: float | None = None,
    amount_variances_allowed: bool = False,
    notes: list[str] | None = None,
    extra_psp_columns: list[str] | None = None,
    extra_orch_columns: list[str] | None = None,
    usd_amount_getter: Callable[[pd.DataFrame], pd.Series] | None = None,
) -> ReconciliationResult:
    secondary_checks = secondary_checks or []
    notes = notes or []
    extra_psp_columns = extra_psp_columns or []
    extra_orch_columns = extra_orch_columns or []

    psp_unique, psp_duplicates = _dedupe(psp_df, psp_key)
    orch_unique, orch_duplicates = _dedupe(orch_df, orch_key)

    psp_cols = [psp_key, psp_amount, psp_currency, psp_time] + [a for a, _, _ in secondary_checks] + extra_psp_columns
    orch_cols = [orch_key, orch_amount, orch_currency, orch_time] + [b for _, b, _ in secondary_checks] + extra_orch_columns
    psp_cols = list(dict.fromkeys(c for c in psp_cols if c in psp_unique.columns))
    orch_cols = list(dict.fromkeys(c for c in orch_cols if c in orch_unique.columns))

    left = psp_unique[["_match_key"] + psp_cols].copy()
    right = orch_unique[["_match_key"] + orch_cols].copy()
    left = left.rename(columns={c: f"PSP {c}" for c in psp_cols})
    right = right.rename(columns={c: f"ORCH {c}" for c in orch_cols})
    merged = left.merge(right, on="_match_key", how="outer", indicator=True)

    psp_key_out = f"PSP {psp_key}"
    orch_key_out = f"ORCH {orch_key}"
    psp_amount_out = f"PSP {psp_amount}"
    orch_amount_out = f"ORCH {orch_amount}"
    psp_currency_out = f"PSP {psp_currency}"
    orch_currency_out = f"ORCH {orch_currency}"
    psp_time_out = f"PSP {psp_time}"
    orch_time_out = f"ORCH {orch_time}"

    both = merged["_merge"].eq("both")
    merged["Key Check"] = np.where(both, "MATCH", np.where(merged["_merge"].eq("left_only"), "PSP ONLY", "ORCHESTRATOR ONLY"))

    psp_amt = numeric_series(merged.get(psp_amount_out, pd.Series(index=merged.index, dtype=float)))
    orch_amt = numeric_series(merged.get(orch_amount_out, pd.Series(index=merged.index, dtype=float)))
    merged["Amount Difference"] = orch_amt - psp_amt
    merged["Amount Check"] = np.where(
        ~both,
        "MISSING",
        np.where((orch_amt - psp_amt).abs() <= amount_tolerance + 1e-9, "MATCH", "MISMATCH"),
    )

    psp_cur = merged.get(psp_currency_out, pd.Series(index=merged.index, dtype=object)).map(_norm_currency)
    orch_cur = merged.get(orch_currency_out, pd.Series(index=merged.index, dtype=object)).map(_norm_currency)
    merged["Currency Check"] = np.where(~both, "MISSING", np.where(psp_cur.eq(orch_cur), "MATCH", "MISMATCH"))

    check_columns = ["Amount Check", "Currency Check"]
    order_check_columns: list[str] = []
    for psp_col, orch_col, label in secondary_checks:
        left_col = f"PSP {psp_col}"
        right_col = f"ORCH {orch_col}"
        left_val = merged.get(left_col, pd.Series(index=merged.index, dtype=object)).map(_norm_key)
        right_val = merged.get(right_col, pd.Series(index=merged.index, dtype=object)).map(_norm_key)
        check_name = f"{label} Check"
        merged[check_name] = np.where(~both, "MISSING", np.where(left_val.eq(right_val), "MATCH", "MISMATCH"))
        check_columns.append(check_name)
        order_check_columns.append(check_name)

    if time_tolerance_seconds is not None and psp_time_out in merged and orch_time_out in merged:
        psp_dt = pd.to_datetime(merged[psp_time_out], errors="coerce")
        orch_dt = pd.to_datetime(merged[orch_time_out], errors="coerce")
        merged["Time Difference (sec)"] = (orch_dt - psp_dt).dt.total_seconds()
        # Timestamps are retained as audit evidence only. They do not affect
        # match status or exception counts because the operational review is
        # based on order/reference and amount reconciliation.

    all_checks_match = pd.Series(True, index=merged.index)
    for col in check_columns:
        all_checks_match &= merged[col].eq("MATCH")

    merged["Match Status"] = np.select(
        [merged["_merge"].eq("left_only"), merged["_merge"].eq("right_only"), both & all_checks_match],
        ["PSP ONLY", "ORCHESTRATOR ONLY", "MATCH"],
        default="MISMATCH",
    )

    # Reorder most useful columns first.
    first_cols = [
        "Match Status",
        "Key Check",
        psp_key_out,
        orch_key_out,
        psp_time_out,
        orch_time_out,
        psp_amount_out,
        orch_amount_out,
        "Amount Difference",
        "Amount Check",
        psp_currency_out,
        orch_currency_out,
        "Currency Check",
    ]
    first_cols += [c for c in merged.columns if c.endswith(" Check") and c not in first_cols]
    if "Time Difference (sec)" in merged:
        first_cols += ["Time Difference (sec)"]
    remaining = [c for c in merged.columns if c not in first_cols and c not in ["_merge", "_match_key"]]
    reconciliation = merged[first_cols + remaining].copy()

    # Duplicate-key rows are exceptions even though the unique comparison keeps one row.
    exception_frames: list[pd.DataFrame] = []
    exceptions = reconciliation[reconciliation["Match Status"].ne("MATCH")].copy()
    if not exceptions.empty:
        exception_frames.append(exceptions)
    if not psp_duplicates.empty:
        dup = psp_duplicates.copy()
        dup.insert(0, "Match Status", "PSP DUPLICATE/BLANK KEY")
        exception_frames.append(dup)
    if not orch_duplicates.empty:
        dup = orch_duplicates.copy()
        dup.insert(0, "Match Status", "ORCHESTRATOR DUPLICATE/BLANK KEY")
        exception_frames.append(dup)
    combined_exceptions = pd.concat(exception_frames, ignore_index=True, sort=False) if exception_frames else pd.DataFrame()

    psp_only = int((merged["_merge"] == "left_only").sum())
    orch_only = int((merged["_merge"] == "right_only").sum())
    key_matched = int(both.sum())
    clean_matches = int((merged["Match Status"] == "MATCH").sum())
    amount_mismatch = int((merged["Amount Check"] == "MISMATCH").sum())
    currency_mismatch = int((merged["Currency Check"] == "MISMATCH").sum())
    if order_check_columns:
        order_mismatch_mask = pd.concat(
            [merged[col].eq("MISMATCH") for col in order_check_columns],
            axis=1,
        ).any(axis=1)
        order_mismatches = int(order_mismatch_mask.sum())
    else:
        order_mismatches = 0
    unmatched = psp_only + orch_only

    if len(psp_unique) == 0 and len(orch_unique) == 0:
        status = "NO APPROVED DATA"
    elif psp_only == 0 and orch_only == 0 and currency_mismatch == 0 and order_mismatches == 0:
        if amount_mismatch == 0:
            status = "FULL MATCH"
        elif amount_variances_allowed:
            status = "MATCHED WITH AMOUNT VARIANCES"
        else:
            status = "REVIEW REQUIRED"
    else:
        status = "REVIEW REQUIRED"

    usd_amount = None
    if usd_amount_getter is not None:
        try:
            usd_amount = float(pd.to_numeric(usd_amount_getter(orch_df), errors="coerce").fillna(0).sum())
        except Exception:  # noqa: BLE001
            usd_amount = None

    summary = {
        "PSP Count": int(len(psp_unique)),
        "Orchestrator Count": int(len(orch_unique)),
        "Matched": key_matched,
        "Unmatched": unmatched,
        "Clean Match": clean_matches,
        "PSP Only": psp_only,
        "Orchestrator Only": orch_only,
        "Order Mismatch": order_mismatches,
        "Amount Mismatch": amount_mismatch,
        "Currency Mismatch": currency_mismatch,
        "PSP Duplicate/Blank Keys": int(len(psp_duplicates)),
        "Orchestrator Duplicate/Blank Keys": int(len(orch_duplicates)),
        "Amount Difference Total": float(pd.to_numeric(merged["Amount Difference"], errors="coerce").fillna(0).sum()),
        "Applied/Matched USD": usd_amount,
    }

    audit = {
        "PSP approved rows before dedupe": len(psp_df),
        "Orchestrator approved rows before dedupe": len(orch_df),
        "PSP unique keys": len(psp_unique),
        "Orchestrator unique keys": len(orch_unique),
        "PSP duplicate/blank-key rows": len(psp_duplicates),
        "Orchestrator duplicate/blank-key rows": len(orch_duplicates),
        "Amount tolerance": amount_tolerance,
        "Timestamp comparison": "Informational only; not counted as a mismatch",
    }

    return ReconciliationResult(
        name=name,
        orchestrator=orchestrator,
        psp=psp,
        status=status,
        summary=summary,
        reconciliation=reconciliation,
        exceptions=combined_exceptions,
        psp_source=psp_df.copy(),
        orchestrator_source=orch_df.copy(),
        notes=notes,
        audit=audit,
    )


# ---------------------------------------------------------------------------
# Reconciliation definitions
# ---------------------------------------------------------------------------

def reconcile_nuvei(bp: pd.DataFrame, psp_df: pd.DataFrame, region: str, amount_tolerance: float) -> ReconciliationResult:
    alias = f"SafeCharge-CreditCard-MID-{region}"
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["midAlias"]).eq(alias)
    ].copy()
    return reconcile_frames(
        name=f"Nuvei {region} vs BridgerPay",
        orchestrator="BridgerPay",
        psp=f"Nuvei {region}",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Transaction ID",
        orch_key="pspOrderId",
        psp_amount="Amount",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("Custom Data", "merchantOrderId", "Merchant Order ID")],
        amount_tolerance=amount_tolerance,
        orch_time="GMT+6 Completion Timestamp",
        time_tolerance_seconds=30,
        notes=[f"BridgerPay filter: midAlias = {alias}.", "Nuvei Transaction ID is matched to BridgerPay pspOrderId."],
    )


def reconcile_trustpayment(bp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq("trustpayments")
    ].copy()
    return reconcile_frames(
        name="TrustPayment vs BridgerPay",
        orchestrator="BridgerPay",
        psp="TrustPayment",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Reference",
        orch_key="pspOrderId",
        psp_amount="Authorised Amount",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("Order Reference", "merchantOrderId", "Merchant Order ID")],
        amount_tolerance=amount_tolerance,
        orch_time="GMT+6 Completion Timestamp",
        time_tolerance_seconds=15,
        notes=["Approved rule: Settle Status 0/100, Request AUTH, Error Code 0.", "Timestamp (BST) is treated as Bangladesh Standard Time (GMT+6)."],
    )


def reconcile_payabl(bp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq("innatech")
    ].copy()
    return reconcile_frames(
        name="Payabl vs BridgerPay",
        orchestrator="BridgerPay",
        psp="Payabl",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Tx-Id",
        orch_key="transactionId",
        psp_amount="Amount",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("Order No.", "pspOrderId", "PSP Order ID")],
        amount_tolerance=amount_tolerance,
        orch_time="GMT+6 Completion Timestamp",
        time_tolerance_seconds=15,
        notes=["Payabl UTC+2 is converted to GMT+6 by adding four hours.", "Payabl Tx-Id is matched to BridgerPay transactionId."],
    )


def reconcile_paysafe_bp(bp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    psp = psp_df[string_series(psp_df["Merchant Transaction ID"]).str.startswith("BP_")].copy()
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq("paysafe")
    ].copy()
    return reconcile_frames(
        name="Paysafe vs BridgerPay",
        orchestrator="BridgerPay",
        psp="Paysafe",
        psp_df=psp,
        orch_df=orch,
        psp_key="Transaction ID",
        orch_key="transactionId",
        psp_amount="Amount",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("Merchant Transaction ID", "merchantOrderId", "Merchant Order ID")],
        amount_tolerance=amount_tolerance,
        orch_time="GMT+6 Completion Timestamp",
        time_tolerance_seconds=10,
        notes=["Only Merchant Transaction IDs beginning BP_ are included for BridgerPay."],
    )


def reconcile_unlimit(bp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq("cardpay")
    ].copy()
    return reconcile_frames(
        name="Unlimit vs BridgerPay",
        orchestrator="BridgerPay",
        psp="Unlimit",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Payment ID",
        orch_key="pspOrderId",
        psp_amount="Amount",
        orch_amount="amount",
        psp_currency="CUR",
        orch_currency="currency",
        amount_tolerance=amount_tolerance,
        time_tolerance_seconds=2,
        notes=["Unlimit is named CardPay in BridgerPay.", "Unlimit Payment ID is matched to BridgerPay pspOrderId."],
    )


def reconcile_ax_channel(bp: pd.DataFrame, psp_df: pd.DataFrame, *, channel: str, bp_name: str, display_name: str, amount_tolerance: float) -> ReconciliationResult:
    psp = psp_df[string_series(psp_df["ChannelName"]).eq(channel)].copy()
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq(bp_name.lower())
    ].copy()
    return reconcile_frames(
        name=f"{display_name} vs BridgerPay",
        orchestrator="BridgerPay",
        psp=display_name,
        psp_df=psp,
        orch_df=orch,
        psp_key="TransactionId",
        orch_key="pspOrderId",
        psp_amount="Reconciliation Amount",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("InvoiceId", "merchantOrderId", "Merchant Order ID"), ("UniqueId", "transactionId", "Transaction ID")],
        amount_tolerance=amount_tolerance,
        time_tolerance_seconds=10,
        notes=["Approved rule: PaymentType DB, Result ACK, ReturnCode 000.000.000.", "RG and CF lifecycle rows are excluded."],
    )


def reconcile_paypal(bp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = bp[
        string_series(bp["status"]).str.lower().eq("approved")
        & string_series(bp["pspName"]).str.lower().eq("paypal")
    ].copy()
    return reconcile_frames(
        name="PayPal vs BridgerPay",
        orchestrator="BridgerPay",
        psp="PayPal",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Transaction ID",
        orch_key="pspOrderId",
        psp_amount="Gross",
        orch_amount="amount",
        psp_currency="Currency",
        orch_currency="currency",
        secondary_checks=[("Transaction ID", "transactionId", "Transaction ID")],
        amount_tolerance=amount_tolerance,
        orch_time="GMT+6 Completion Timestamp",
        time_tolerance_seconds=10,
        notes=["PayPal UTC-7 is converted to GMT+6 by adding 13 hours.", "Gross is used as the payment amount."],
    )


def reconcile_dlocal(pp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = pp[
        string_series(pp["MID"]).eq("GL-DL-FUNDEDNEXT-01")
        & string_series(pp["Status"]).str.lower().eq("success")
        & string_series(pp["Type"]).str.lower().eq("sale")
    ].copy()
    return reconcile_frames(
        name="Dlocal vs PayProcc",
        orchestrator="PayProcc",
        psp="Dlocal",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Reference",
        orch_key="Gateway ID",
        psp_amount="Amount",
        orch_amount="Amount",
        psp_currency="Balance currency",
        orch_currency="Currency",
        secondary_checks=[("Invoice", "Payment Public ID", "Payment Public ID")],
        amount_tolerance=amount_tolerance,
        time_tolerance_seconds=2,
        amount_variances_allowed=True,
        notes=["Dlocal date is based on Validated date converted from GMT+0 to GMT+6.", "Amount variances are separately flagged while matched references remain reconciled."],
        usd_amount_getter=lambda frame: numeric_series(frame["Amount"]),
    )


def reconcile_skrill(pp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    orch = pp[
        string_series(pp["MID"]).eq("GL-SKRILL-FUNDEDNEXT-01")
        & string_series(pp["Status"]).str.lower().eq("success")
        & string_series(pp["Type"]).str.lower().eq("sale")
    ].copy()
    return reconcile_frames(
        name="Skrill vs PayProcc",
        orchestrator="PayProcc",
        psp="Skrill",
        psp_df=psp_df,
        orch_df=orch,
        psp_key="Reference",
        orch_key="Gateway ID",
        psp_amount="[+]",
        orch_amount="Amount",
        psp_currency="Currency",
        orch_currency="Currency",
        secondary_checks=[("Reference", "Payment Public ID", "Payment Public ID")],
        amount_tolerance=amount_tolerance,
        time_tolerance_seconds=60,
        notes=["The July CET-labelled timestamps are treated as CEST (UTC+2), then converted to GMT+6.", "Fee and surcharge ledger rows are excluded."],
        usd_amount_getter=lambda frame: numeric_series(frame["Amount"]),
    )


def reconcile_paysafe_payprocc(pp: pd.DataFrame, psp_df: pd.DataFrame, amount_tolerance: float) -> ReconciliationResult:
    psp = psp_df[~string_series(psp_df["Merchant Transaction ID"]).str.startswith("BP_")].copy()
    orch = pp[
        string_series(pp["MID"]).str.contains(r"-PS-FUNDEDNEXT-01", regex=True, na=False)
        & string_series(pp["Status"]).str.lower().eq("success")
        & string_series(pp["Type"]).str.lower().eq("sale")
    ].copy()

    def applied_usd(frame: pd.DataFrame) -> pd.Series:
        applied_currency = string_series(frame.get("Applied Currency", pd.Series(index=frame.index, dtype=object))).str.upper()
        amount = numeric_series(frame.get("Amount", pd.Series(index=frame.index, dtype=float)))
        applied = numeric_series(frame.get("Applied Amount", pd.Series(index=frame.index, dtype=float)))
        currency = string_series(frame.get("Currency", pd.Series(index=frame.index, dtype=object))).str.upper()
        return pd.Series(np.where(applied_currency.eq("USD"), applied, np.where(currency.eq("USD"), amount, 0)), index=frame.index)

    return reconcile_frames(
        name="Paysafe Local vs PayProcc",
        orchestrator="PayProcc",
        psp="Paysafe Local",
        psp_df=psp,
        orch_df=orch,
        psp_key="Transaction ID",
        orch_key="Gateway ID",
        psp_amount="Amount",
        orch_amount="Amount",
        psp_currency="Currency",
        orch_currency="Currency",
        secondary_checks=[("Merchant Transaction ID", "Payment Public ID", "Payment Public ID")],
        amount_tolerance=amount_tolerance,
        time_tolerance_seconds=6,
        notes=["Paysafe transactions with BP_ Merchant Transaction IDs are excluded because they belong to BridgerPay.", "PayProcc Applied Amount is used for USD reporting when Applied Currency is USD."],
        extra_orch_columns=["Applied Amount", "Applied Currency", "MID"],
        usd_amount_getter=applied_usd,
    )



# ---------------------------------------------------------------------------
# Bulk upload auto-detection
# ---------------------------------------------------------------------------

FILE_SLOT_LABELS: dict[str, str] = {
    "bridgerpay": "BridgerPay",
    "payprocc": "PayProcc",
    "nuvei_eu": "Nuvei EU",
    "nuvei_ae": "Nuvei AE",
    "trustpayment": "TrustPayment",
    "payabl": "Payabl",
    "paysafe": "Paysafe",
    "unlimit": "Unlimit",
    "axcess_paystra": "Axcess/Paystra",
    "paypal": "PayPal",
    "dlocal": "Dlocal",
    "skrill": "Skrill",
}

# Signatures are intentionally based on exported column names rather than file
# names. This lets users select all reports in one upload action even when a
# PSP generates generic or timestamp-only filenames.
_FILE_SIGNATURES: list[tuple[str, set[str]]] = [
    (
        "bridgerpay",
        {"processing_date", "pspName", "transactionId", "pspOrderId", "merchantOrderId", "amount", "currency", "status", "midAlias"},
    ),
    (
        "payprocc",
        {"Payment Public ID", "Merchant Order ID", "MID", "Transaction Date", "Type", "Status", "Amount", "Currency", "Gateway ID", "Transaction ID"},
    ),
    (
        "trustpayment",
        {"Reference", "Settle Status", "Error Code", "Authorised Amount", "Timestamp (BST)", "Currency", "Order Reference", "Request"},
    ),
    (
        "payabl",
        {"Tx-Id", "Tx-Type", "Order No.", "Date", "Time", "Currency", "Amount", "Status"},
    ),
    (
        "paysafe",
        {"Transaction ID", "Merchant Transaction ID", "Transaction Date", "Transaction Time (GMT)", "Transaction Type", "Status", "Amount", "Currency"},
    ),
    (
        "unlimit",
        {"Payment ID", "Amount", "CUR", "Status", "Order type", "Payment date"},
    ),
    (
        "axcess_paystra",
        {"UniqueId", "PaymentType", "RequestTimestamp", "TransactionId", "ChannelName", "ReturnCode", "Credit", "Currency", "Result", "InvoiceId"},
    ),
    (
        "paypal",
        {"Date", "Time", "Type", "Status", "Currency", "Gross", "Transaction ID", "Balance Impact"},
    ),
    (
        "dlocal",
        {"Reference", "Invoice", "Validated date", "Balance currency", "Amount", "Status", "Transaction type"},
    ),
    (
        "skrill",
        {"ID", "Time (CET)", "Type", "Transaction Details", "[+]", "Status", "Reference", "Currency"},
    ),
    (
        "nuvei",
        {"Date", "Transaction ID", "Transaction Type", "Transaction Result", "Amount", "Currency", "Custom Data"},
    ),
]


def _source_name(source: Any, index: int = 0) -> str:
    name = getattr(source, "name", None)
    return str(name) if name else f"uploaded_file_{index + 1}"


def _header_rows_from_csv(source: Any) -> list[set[str]]:
    data = _bytes(source)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            sample = data[:200_000].decode(encoding)
            try:
                sep = csv.Sniffer().sniff(sample[:20_000], delimiters=",;\t|").delimiter
            except csv.Error:
                sep = ","
            frame = pd.read_csv(BytesIO(data), encoding=encoding, sep=sep, nrows=0)
            if len(frame.columns) == 1 and sep != ";":
                frame = pd.read_csv(BytesIO(data), encoding=encoding, sep=";", nrows=0)
            return [{str(column).strip() for column in frame.columns}]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise ValueError(f"Could not inspect CSV header: {last_error}")


def _header_rows_from_excel(source: Any) -> list[set[str]]:
    data = BytesIO(_bytes(source))
    excel = pd.ExcelFile(data)
    header_rows: list[set[str]] = []
    for sheet_name in excel.sheet_names:
        raw = pd.read_excel(excel, sheet_name=sheet_name, header=None, nrows=45, dtype=object)
        for _, row in raw.iterrows():
            values = {
                str(value).strip()
                for value in row.tolist()
                if pd.notna(value) and str(value).strip()
            }
            if len(values) >= 3:
                header_rows.append(values)
    return header_rows


def detect_uploaded_file_type(source: Any) -> tuple[str | None, str]:
    """Detect one supported report type from its exported columns."""
    filename = _source_name(source).lower()
    try:
        if filename.endswith((".xlsx", ".xls")):
            rows = _header_rows_from_excel(source)
        else:
            rows = _header_rows_from_csv(source)
    except Exception as exc:  # noqa: BLE001
        return None, f"Header could not be read: {exc}"

    matches: list[tuple[str, int]] = []
    for role, signature in _FILE_SIGNATURES:
        if any(signature.issubset(row) for row in rows):
            matches.append((role, len(signature)))

    if not matches:
        return None, "No supported column signature was found"

    # Prefer the most specific signature if a generic export happens to satisfy
    # more than one partial pattern.
    matches.sort(key=lambda item: item[1], reverse=True)
    best_size = matches[0][1]
    best_roles = [role for role, size in matches if size == best_size]
    if len(best_roles) > 1:
        return None, f"Ambiguous column signatures: {', '.join(best_roles)}"
    return best_roles[0], "Detected from exported columns"


def _approved_count_for_role(role: str, source: Any, target_date: date) -> int:
    parsers: dict[str, Callable[[Any, date], pd.DataFrame]] = {
        "bridgerpay": parse_bridgerpay,
        "payprocc": parse_payprocc,
        "trustpayment": parse_trustpayment,
        "payabl": parse_payabl,
        "paysafe": parse_paysafe,
        "unlimit": parse_unlimit,
        "axcess_paystra": parse_axcess_paystra,
        "paypal": parse_paypal,
        "dlocal": parse_dlocal,
        "skrill": parse_skrill,
        "nuvei": parse_nuvei,
    }
    parser = parsers[role]
    return len(parser(source, target_date))


def auto_assign_uploaded_files(
    uploaded_files: Iterable[Any] | None,
    target_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assign a multi-file upload to dashboard slots automatically.

    Nuvei EU and AE have the same file structure, so those two reports are
    distinguished by matching their Transaction IDs against the SafeCharge EU
    and AE pspOrderId populations in the uploaded BridgerPay file.
    """
    sources = list(uploaded_files or [])
    assigned: dict[str, Any] = {}
    mapping: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[int, Any]]] = {}

    for index, source in enumerate(sources):
        role, detail = detect_uploaded_file_type(source)
        row = {
            "File Name": _source_name(source, index),
            "Detected Type": "Unrecognized" if role is None else ("Nuvei / SafeCharge" if role == "nuvei" else FILE_SLOT_LABELS.get(role, role)),
            "Assigned Slot": "",
            "Status": "Unrecognized" if role is None else "Detected",
            "Details": detail,
        }
        mapping.append(row)
        if role is not None:
            candidates.setdefault(role, []).append((index, source))

    # Unique file types. If more than one file has the same signature, select
    # the one containing the most approved rows for the requested date and
    # clearly mark the other files as ignored duplicates.
    for role, role_candidates in candidates.items():
        if role == "nuvei":
            continue
        if len(role_candidates) == 1:
            index, source = role_candidates[0]
            assigned[role] = source
            mapping[index]["Assigned Slot"] = FILE_SLOT_LABELS[role]
            mapping[index]["Status"] = "Assigned"
            continue

        scored: list[tuple[int, int, Any, str]] = []
        for index, source in role_candidates:
            try:
                count = _approved_count_for_role(role, source, target_date)
                error = ""
            except Exception as exc:  # noqa: BLE001
                count = -1
                error = str(exc)
            scored.append((count, index, source, error))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_count, best_index, best_source, _ = scored[0]
        assigned[role] = best_source
        mapping[best_index]["Assigned Slot"] = FILE_SLOT_LABELS[role]
        mapping[best_index]["Status"] = "Assigned"
        mapping[best_index]["Details"] = f"Selected from {len(role_candidates)} files; {max(best_count, 0)} approved/date rows"
        for count, index, _source, error in scored[1:]:
            mapping[index]["Status"] = "Ignored duplicate"
            mapping[index]["Details"] = error or f"Another {FILE_SLOT_LABELS[role]} file had more approved/date rows ({max(best_count, 0)} vs {max(count, 0)})"

    # Resolve Nuvei EU/AE using BridgerPay SafeCharge aliases.
    nuvei_candidates = candidates.get("nuvei", [])
    if nuvei_candidates:
        region_sets: dict[str, set[str]] = {"EU": set(), "AE": set()}
        bp_source = assigned.get("bridgerpay")
        if bp_source is not None:
            try:
                bp = parse_bridgerpay(bp_source, target_date)
                for region in ("EU", "AE"):
                    alias = f"SafeCharge-CreditCard-MID-{region}"
                    region_sets[region] = set(
                        string_series(
                            bp.loc[
                                string_series(bp["status"]).str.lower().eq("approved")
                                & string_series(bp["midAlias"]).eq(alias),
                                "pspOrderId",
                            ]
                        ).map(_norm_key)
                    )
            except Exception:  # noqa: BLE001
                pass

        score_rows: list[tuple[int, int, str, Any, int]] = []
        parsed_nuvei: dict[int, pd.DataFrame] = {}
        for index, source in nuvei_candidates:
            try:
                frame = parse_nuvei(source, target_date)
                parsed_nuvei[index] = frame
                ids = set(string_series(frame["Transaction ID"]).map(_norm_key))
            except Exception as exc:  # noqa: BLE001
                mapping[index]["Status"] = "Needs review"
                mapping[index]["Details"] = f"Nuvei file could not be parsed: {exc}"
                ids = set()
            for region in ("EU", "AE"):
                score_rows.append((len(ids & region_sets[region]), index, region, source, len(ids)))

        # Greedy assignment is sufficient for the two unique regions and is
        # deterministic because scores are sorted by overlap and row count.
        score_rows.sort(key=lambda item: (item[0], item[4]), reverse=True)
        used_indices: set[int] = set()
        used_regions: set[str] = set()
        for score, index, region, source, row_count in score_rows:
            if index in used_indices or region in used_regions or score <= 0:
                continue
            slot = f"nuvei_{region.lower()}"
            assigned[slot] = source
            used_indices.add(index)
            used_regions.add(region)
            mapping[index]["Assigned Slot"] = FILE_SLOT_LABELS[slot]
            mapping[index]["Status"] = "Assigned"
            mapping[index]["Details"] = f"{score} Transaction IDs matched BridgerPay SafeCharge {region}; {row_count} approved/date rows"

        # Filename hints are a fallback when no SafeCharge overlap is available.
        for index, source in nuvei_candidates:
            if index in used_indices:
                continue
            filename = _source_name(source, index).lower()
            hinted_region = None
            if "nuvei_eu" in filename or "safecharge_eu" in filename or "-eu" in filename or "_eu" in filename:
                hinted_region = "EU"
            elif "nuvei_ae" in filename or "safecharge_ae" in filename or "-ae" in filename or "_ae" in filename:
                hinted_region = "AE"
            if hinted_region and hinted_region not in used_regions:
                slot = f"nuvei_{hinted_region.lower()}"
                assigned[slot] = source
                used_indices.add(index)
                used_regions.add(hinted_region)
                mapping[index]["Assigned Slot"] = FILE_SLOT_LABELS[slot]
                mapping[index]["Status"] = "Assigned by filename"
                mapping[index]["Details"] = "SafeCharge overlap was unavailable; filename hint was used"

        # With exactly one unresolved candidate and one remaining region, assign
        # it as a transparent fallback instead of forcing a separate upload box.
        unresolved = [(index, source) for index, source in nuvei_candidates if index not in used_indices]
        remaining_regions = [region for region in ("EU", "AE") if region not in used_regions]
        if len(unresolved) == 1 and len(remaining_regions) == 1:
            index, source = unresolved[0]
            region = remaining_regions[0]
            slot = f"nuvei_{region.lower()}"
            assigned[slot] = source
            used_indices.add(index)
            mapping[index]["Assigned Slot"] = FILE_SLOT_LABELS[slot]
            mapping[index]["Status"] = "Assigned by elimination"
            mapping[index]["Details"] = "Only one Nuvei file and one SafeCharge region remained"

        for index, _source in nuvei_candidates:
            if index not in used_indices and mapping[index]["Status"] == "Detected":
                mapping[index]["Status"] = "Needs review"
                mapping[index]["Details"] = "Could not determine whether this is Nuvei EU or Nuvei AE; upload BridgerPay with the Nuvei files"

    return assigned, mapping


# ---------------------------------------------------------------------------
# Orchestration and exports
# ---------------------------------------------------------------------------

def run_all_reconciliations(
    files: dict[str, Any],
    target_date: date,
    amount_tolerance: float = 0.01,
) -> tuple[list[ReconciliationResult], list[dict[str, Any]]]:
    results: list[ReconciliationResult] = []
    file_audit: list[dict[str, Any]] = []

    parsed: dict[str, pd.DataFrame] = {}
    parser_map: dict[str, tuple[str, Callable[[Any, date], pd.DataFrame]]] = {
        "bridgerpay": ("BridgerPay", parse_bridgerpay),
        "payprocc": ("PayProcc", parse_payprocc),
        "nuvei_eu": ("Nuvei EU", parse_nuvei),
        "nuvei_ae": ("Nuvei AE", parse_nuvei),
        "trustpayment": ("TrustPayment", parse_trustpayment),
        "payabl": ("Payabl", parse_payabl),
        "paysafe": ("Paysafe", parse_paysafe),
        "unlimit": ("Unlimit", parse_unlimit),
        "axcess_paystra": ("Axcess/Paystra", parse_axcess_paystra),
        "paypal": ("PayPal", parse_paypal),
        "dlocal": ("Dlocal", parse_dlocal),
        "skrill": ("Skrill", parse_skrill),
    }

    for key, (label, parser) in parser_map.items():
        source = files.get(key)
        if source is None:
            file_audit.append({"File Slot": label, "Status": "Not uploaded", "Approved/Date Rows": None, "Error": ""})
            continue
        try:
            df = parser(source, target_date)
            parsed[key] = df
            file_audit.append({"File Slot": label, "Status": "Ready", "Approved/Date Rows": len(df), "Error": ""})
        except Exception as exc:  # noqa: BLE001
            file_audit.append({"File Slot": label, "Status": "Error", "Approved/Date Rows": None, "Error": str(exc)})

    bp = parsed.get("bridgerpay")
    pp = parsed.get("payprocc")

    def add_if(required: list[str], fn: Callable[[], ReconciliationResult]) -> None:
        if all(key in parsed for key in required):
            try:
                results.append(fn())
            except Exception as exc:  # noqa: BLE001
                name = getattr(fn, "__name__", "Reconciliation")
                file_audit.append({"File Slot": name, "Status": "Reconciliation error", "Approved/Date Rows": None, "Error": str(exc)})

    if bp is not None:
        add_if(["bridgerpay", "nuvei_eu"], lambda: reconcile_nuvei(bp, parsed["nuvei_eu"], "EU", amount_tolerance))
        add_if(["bridgerpay", "nuvei_ae"], lambda: reconcile_nuvei(bp, parsed["nuvei_ae"], "AE", amount_tolerance))
        add_if(["bridgerpay", "trustpayment"], lambda: reconcile_trustpayment(bp, parsed["trustpayment"], amount_tolerance))
        add_if(["bridgerpay", "payabl"], lambda: reconcile_payabl(bp, parsed["payabl"], amount_tolerance))
        add_if(["bridgerpay", "paysafe"], lambda: reconcile_paysafe_bp(bp, parsed["paysafe"], amount_tolerance))
        add_if(["bridgerpay", "unlimit"], lambda: reconcile_unlimit(bp, parsed["unlimit"], amount_tolerance))
        add_if(
            ["bridgerpay", "axcess_paystra"],
            lambda: reconcile_ax_channel(
                bp,
                parsed["axcess_paystra"],
                channel="fundednext.com - PS",
                bp_name="Paystra",
                display_name="Paystra",
                amount_tolerance=amount_tolerance,
            ),
        )
        add_if(
            ["bridgerpay", "axcess_paystra"],
            lambda: reconcile_ax_channel(
                bp,
                parsed["axcess_paystra"],
                channel="fundednext.com - 3DS",
                bp_name="Axcess",
                display_name="Axcess",
                amount_tolerance=amount_tolerance,
            ),
        )
        add_if(["bridgerpay", "paypal"], lambda: reconcile_paypal(bp, parsed["paypal"], amount_tolerance))

    if pp is not None:
        add_if(["payprocc", "dlocal"], lambda: reconcile_dlocal(pp, parsed["dlocal"], amount_tolerance))
        add_if(["payprocc", "skrill"], lambda: reconcile_skrill(pp, parsed["skrill"], amount_tolerance))
        add_if(["payprocc", "paysafe"], lambda: reconcile_paysafe_payprocc(pp, parsed["paysafe"], amount_tolerance))

    return results, file_audit


def summary_dataframe(results: list[ReconciliationResult]) -> pd.DataFrame:
    """Return a stable summary schema, including legacy session results.

    Streamlit may retain result objects in session state across code updates.
    Defaults here prevent missing newly introduced fields from breaking either
    the dashboard or the consolidated Excel export.
    """
    rows = []
    for r in results:
        values = dict(r.summary or {})
        psp_only = int(values.get("PSP Only", 0) or 0)
        orch_only = int(values.get("Orchestrator Only", 0) or 0)
        values.setdefault("PSP Count", 0)
        values.setdefault("Orchestrator Count", 0)
        values.setdefault("Matched", values.get("Clean Match", 0) or 0)
        values.setdefault("Unmatched", psp_only + orch_only)
        values.setdefault("Order Mismatch", 0)
        values.setdefault("Amount Mismatch", 0)
        values.setdefault("Currency Mismatch", 0)
        row = {
            "Orchestrator": r.orchestrator,
            "PSP": r.psp,
            "Status": r.status,
            **values,
        }
        rows.append(row)

    columns = [
        "Orchestrator",
        "PSP",
        "Status",
        "PSP Count",
        "Orchestrator Count",
        "Matched",
        "Unmatched",
        "PSP Only",
        "Orchestrator Only",
        "Order Mismatch",
        "Amount Mismatch",
        "Currency Mismatch",
    ]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in frame.columns:
            frame[column] = 0 if column not in {"Orchestrator", "PSP", "Status"} else ""
    remaining = [column for column in frame.columns if column not in columns]
    return frame[columns + remaining]


def exceptions_dataframe(results: list[ReconciliationResult]) -> pd.DataFrame:
    frames = []
    for result in results:
        if result.exceptions.empty:
            continue
        df = result.exceptions.copy()
        df.insert(0, "Reconciliation", result.name)
        df.insert(1, "Orchestrator", result.orchestrator)
        df.insert(2, "PSP", result.psp)
        frames.append(df)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def build_excel_report(
    results: list[ReconciliationResult],
    file_audit: list[dict[str, Any]],
    target_date: date,
    upload_mapping: list[dict[str, Any]] | None = None,
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
        workbook = writer.book
        header_fmt = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#4472C4", "border": 1, "align": "center", "valign": "vcenter"})
        title_fmt = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#17365D", "font_size": 16, "align": "center", "valign": "vcenter"})
        green_fmt = workbook.add_format({"bg_color": "#E2F0D9", "font_color": "#375623"})
        yellow_fmt = workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"})
        red_fmt = workbook.add_format({"bg_color": "#FCE4D6", "font_color": "#C00000"})
        money_fmt = workbook.add_format({"num_format": "$#,##0.00;[Red]-$#,##0.00"})

        summary = summary_dataframe(results)
        summary.to_excel(writer, sheet_name="Summary", index=False, startrow=2)
        ws = writer.sheets["Summary"]
        ws.merge_range(0, 0, 0, max(0, len(summary.columns) - 1), f"Payment Reconciliation Summary — {target_date.isoformat()} GMT+6", title_fmt)
        ws.set_row(0, 26)
        for col_idx, col in enumerate(summary.columns):
            ws.write(2, col_idx, col, header_fmt)
            width = min(max(len(str(col)) + 2, 14), 28)
            ws.set_column(col_idx, col_idx, width)
        if not summary.empty and "Status" in summary.columns:
            status_col = summary.columns.get_loc("Status")
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "FULL MATCH", "format": green_fmt})
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "VARIANCES", "format": yellow_fmt})
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "REVIEW", "format": red_fmt})
        if "Amount Difference Total" in summary.columns:
            col = summary.columns.get_loc("Amount Difference Total")
            ws.set_column(col, col, 20, money_fmt)
        if "Applied/Matched USD" in summary.columns:
            col = summary.columns.get_loc("Applied/Matched USD")
            ws.set_column(col, col, 20, money_fmt)
        ws.freeze_panes(3, 0)
        ws.autofilter(2, 0, max(2, len(summary) + 2), max(0, len(summary.columns) - 1))

        if upload_mapping:
            mapping_df = pd.DataFrame(upload_mapping)
            mapping_df.to_excel(writer, sheet_name="Upload Mapping", index=False)
            mws = writer.sheets["Upload Mapping"]
            for col_idx, col in enumerate(mapping_df.columns):
                mws.write(0, col_idx, col, header_fmt)
                mws.set_column(col_idx, col_idx, min(max(len(str(col)) + 3, 18), 55))
            mws.freeze_panes(1, 0)
            if not mapping_df.empty:
                mws.autofilter(0, 0, len(mapping_df), len(mapping_df.columns) - 1)

        audit_df = pd.DataFrame(file_audit)
        audit_df.to_excel(writer, sheet_name="File Audit", index=False)
        aws = writer.sheets["File Audit"]
        for col_idx, col in enumerate(audit_df.columns):
            aws.write(0, col_idx, col, header_fmt)
            aws.set_column(col_idx, col_idx, min(max(len(str(col)) + 3, 16), 50))
        aws.freeze_panes(1, 0)

        all_exceptions = exceptions_dataframe(results)
        if all_exceptions.empty:
            pd.DataFrame({"Message": ["No exceptions found."]}).to_excel(writer, sheet_name="All Exceptions", index=False)
        else:
            all_exceptions.to_excel(writer, sheet_name="All Exceptions", index=False)
            ews = writer.sheets["All Exceptions"]
            for col_idx, col in enumerate(all_exceptions.columns):
                ews.write(0, col_idx, col, header_fmt)
                ews.set_column(col_idx, col_idx, min(max(len(str(col)) + 3, 14), 38))
            ews.freeze_panes(1, 0)
            ews.autofilter(0, 0, len(all_exceptions), len(all_exceptions.columns) - 1)

        used_names: set[str] = {"Summary", "Upload Mapping", "File Audit", "All Exceptions"}
        for idx, result in enumerate(results, start=1):
            base = _safe_sheet_name(f"{idx:02d} {result.psp}")
            sheet_name = base
            counter = 2
            while sheet_name in used_names:
                suffix = f" {counter}"
                sheet_name = _safe_sheet_name(base[: 31 - len(suffix)] + suffix)
                counter += 1
            used_names.add(sheet_name)
            result.reconciliation.to_excel(writer, sheet_name=sheet_name, index=False)
            rws = writer.sheets[sheet_name]
            for col_idx, col in enumerate(result.reconciliation.columns):
                rws.write(0, col_idx, col, header_fmt)
                width = min(max(len(str(col)) + 3, 14), 38)
                rws.set_column(col_idx, col_idx, width)
            rws.freeze_panes(1, 0)
            if not result.reconciliation.empty:
                rws.autofilter(0, 0, len(result.reconciliation), len(result.reconciliation.columns) - 1)
                if "Match Status" in result.reconciliation.columns:
                    c = result.reconciliation.columns.get_loc("Match Status")
                    rws.conditional_format(1, c, len(result.reconciliation), c, {"type": "text", "criteria": "containing", "value": "MATCH", "format": green_fmt})
                    rws.conditional_format(1, c, len(result.reconciliation), c, {"type": "text", "criteria": "containing", "value": "ONLY", "format": red_fmt})
                    rws.conditional_format(1, c, len(result.reconciliation), c, {"type": "text", "criteria": "containing", "value": "MISMATCH", "format": yellow_fmt})

    return output.getvalue()
