from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Callable, Iterable
import csv

import numpy as np
import pandas as pd

GMT6 = "Asia/Dhaka"
ENGINE_VERSION = "2.7"


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


# ===========================================================================
# Orchestrator -> Backend API reconciliation (v2.5)
# ===========================================================================

@dataclass
class BackendReconciliationResult:
    name: str
    orchestrator: str
    backend_gateway: str
    status: str
    summary: dict[str, Any]
    reconciliation: pd.DataFrame
    exceptions: pd.DataFrame
    orchestrator_source: pd.DataFrame
    backend_source: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


BACKEND_FILE_SLOT_LABELS: dict[str, str] = {
    "backend_api": "Backend API",
    "backend_bridgerpay": "BridgerPay",
    "backend_payprocc": "PayProcc",
    "backend_coinsbuy": "Coinsbuy",
    "backend_confirmo": "Confirmo",
    "backend_zen": "ZEN",
}

_BACKEND_FILE_SIGNATURES: list[tuple[str, set[str]]] = [
    (
        "backend_api",
        {"Order ID", "Transaction ID", "Gateway", "Status", "Grand Total", "Created At", "Updated At"},
    ),
    (
        "backend_bridgerpay",
        {"processing_date", "pspName", "transactionId", "pspOrderId", "merchantOrderId", "amount", "currency", "status", "midAlias"},
    ),
    (
        "backend_payprocc",
        {"Payment Public ID", "Merchant Order ID", "MID", "Transaction Date", "Type", "Status", "Amount", "Currency", "Gateway ID", "Transaction ID"},
    ),
    (
        "backend_coinsbuy",
        {"ID", "Created", "Type", "Status", "Currency", "Amount", "Rate", "Tracking ID", "Operation ID", "Target amount", "Target currency"},
    ),
    (
        "backend_confirmo",
        {"ID", "Reference", "CreatedAt", "Status", "MerchantAmount", "MerchantCurrency", "CustomerAmount", "CustomerCurrency"},
    ),
    (
        "backend_zen",
        {"transaction_id", "merchant_transaction_id", "created_at", "transaction_type", "transaction_amount", "transaction_currency", "payment_channel"},
    ),
]


def _read_csv_optional_sep_directive(source: Any) -> pd.DataFrame:
    """Read CSV exports that may begin with Excel's ``sep=,`` directive."""
    data = _bytes(source)
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        lines = text.splitlines()
        directive = lines[0].strip().strip("\"").strip("\'") if lines else ""
        if directive.lower().startswith("sep="):
            sep = directive[4:] or ","
            cleaned = "\n".join(lines[1:]).encode("utf-8")
            return pd.read_csv(BytesIO(cleaned), encoding="utf-8", sep=sep)
    return read_csv_flexible(source)


def _backend_header_rows_from_csv(source: Any) -> list[set[str]]:
    data = _bytes(source)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = data.decode(encoding)
            lines = text.splitlines()
            directive = lines[0].strip().strip("\"").strip("\'") if lines else ""
            if directive.lower().startswith("sep="):
                sep = directive[4:] or ","
                text = "\n".join(lines[1:])
                payload = text.encode("utf-8")
                frame = pd.read_csv(BytesIO(payload), encoding="utf-8", sep=sep, nrows=0)
            else:
                try:
                    sep = csv.Sniffer().sniff(text[:20_000], delimiters=",;\t|").delimiter
                except csv.Error:
                    sep = ","
                payload = data
                frame = pd.read_csv(BytesIO(payload), encoding=encoding, sep=sep, nrows=0)
                if len(frame.columns) == 1 and sep != ";":
                    frame = pd.read_csv(BytesIO(payload), encoding=encoding, sep=";", nrows=0)
            return [{str(column).strip() for column in frame.columns}]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise ValueError(f"Could not inspect CSV header: {last_error}")


def detect_backend_uploaded_file_type(source: Any) -> tuple[str | None, str]:
    filename = _source_name(source).lower()
    try:
        rows = _header_rows_from_excel(source) if filename.endswith((".xlsx", ".xls")) else _backend_header_rows_from_csv(source)
    except Exception as exc:  # noqa: BLE001
        return None, f"Header could not be read: {exc}"

    matches: list[tuple[str, int]] = []
    for role, signature in _BACKEND_FILE_SIGNATURES:
        if any(signature.issubset(row) for row in rows):
            matches.append((role, len(signature)))
    if not matches:
        return None, "No backend-reconciliation column signature was found"
    matches.sort(key=lambda item: item[1], reverse=True)
    best_size = matches[0][1]
    best = [role for role, size in matches if size == best_size]
    if len(best) > 1:
        return None, f"Ambiguous column signatures: {', '.join(best)}"
    return best[0], "Detected from exported columns"


def parse_backend_api_created(source: Any) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Order ID", "Transaction ID", "Gateway", "Status", "Grand Total", "Created At", "Updated At"],
        "Backend API",
    )
    df["Backend Created GMT+6"] = add_hours(df["Created At"], 3, fmt="%Y-%m-%d %H:%M:%S")
    df["Backend Updated GMT+6"] = add_hours(df["Updated At"], 3, fmt="%Y-%m-%d %H:%M:%S")
    df["Backend Currency"] = "USD"
    relevant_gateways = {"Bridger Pay", "Pay Procc", "Crypto", "Confirmo", "Zen Pay"}
    approved = string_series(df["Status"]).str.lower().eq("enabled") & string_series(df["Gateway"]).isin(relevant_gateways)
    return df[approved].copy()


def parse_backend_bridgerpay_full(source: Any) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["processing_date", "merchantOrderId", "amount", "currency", "status", "transactionId", "pspOrderId"],
        "BridgerPay",
    )
    df["Orchestrator Business GMT+6"] = to_gmt6_from_utc(df["processing_date"])
    return df[string_series(df["status"]).str.lower().eq("approved")].copy()


def parse_backend_payprocc_full(source: Any) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["Merchant Order ID", "Transaction Date", "Type", "Status", "Amount", "Currency", "Applied Amount", "Applied Currency", "MID"],
        "PayProcc",
    )
    df["Orchestrator Business GMT+6"] = pd.to_datetime(
        df["Transaction Date"], format="%Y-%m-%d %I:%M:%S %p", errors="coerce"
    )
    amount = numeric_series(df["Amount"])
    applied = numeric_series(df["Applied Amount"])
    currency = string_series(df["Currency"]).str.upper()
    applied_currency = string_series(df["Applied Currency"]).str.upper()
    df["Selected USD Amount"] = np.where(applied_currency.eq("USD"), applied, np.where(currency.eq("USD"), amount, np.nan))
    df["Selected Currency"] = np.where(pd.notna(df["Selected USD Amount"]), "USD", "")
    approved = string_series(df["Status"]).str.lower().eq("success") & string_series(df["Type"]).str.lower().eq("sale")
    return df[approved].copy()


def parse_backend_coinsbuy_full(source: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = clean_columns(read_excel_detect_header(source, ["ID", "Created", "Type", "Status", "Amount", "Rate", "Operation ID"]))
    require_columns(
        df,
        ["ID", "Created", "Type", "Status", "Currency", "Amount", "Rate", "Tracking ID", "Operation ID", "Target amount", "Target currency"],
        "Coinsbuy",
    )
    df["Orchestrator Business GMT+6"] = to_gmt6_from_utc(df["Created"])
    df["Operation Number"] = string_series(df["Operation ID"]).str.extract(r"(\d+)$", expand=False).fillna("")
    df["Gross USD Equivalent"] = numeric_series(df["Amount"]) * numeric_series(df["Rate"])
    df["Selected Currency"] = "USD"
    approved = string_series(df["Type"]).str.lower().eq("deposit") & string_series(df["Status"]).str.lower().eq("confirmed")
    df = df[approved].copy()
    internal = numeric_series(df["Amount"]).gt(2500) & string_series(df["Tracking ID"]).eq("")
    excluded = df[internal].copy()
    included = df[~internal].copy()
    return included, excluded


def parse_backend_confirmo_full(source: Any) -> pd.DataFrame:
    df = clean_columns(_read_csv_optional_sep_directive(source))
    require_columns(
        df,
        ["ID", "Reference", "CreatedAt", "Status", "MerchantAmount", "MerchantCurrency", "CustomerAmount", "CustomerCurrency"],
        "Confirmo",
    )
    df["Orchestrator Business GMT+6"] = add_hours(df["CreatedAt"], 6, fmt="%d.%m.%Y %H:%M:%S")
    return df[string_series(df["Status"]).str.upper().eq("PAID")].copy()


def parse_backend_zen_full(source: Any) -> pd.DataFrame:
    df = clean_columns(read_csv_flexible(source))
    require_columns(
        df,
        ["transaction_id", "merchant_transaction_id", "created_at", "transaction_type", "transaction_amount", "transaction_currency", "payment_channel"],
        "ZEN",
    )
    df["Orchestrator Business GMT+6"] = to_gmt6_from_utc(df["created_at"])
    approved = (
        string_series(df["transaction_type"]).str.lower().eq("purchase")
        & string_series(df["payment_channel"]).isin(["Apple Pay", "Google Pay"])
    )
    return df[approved].copy()


def _backend_target_count(role: str, source: Any, target_date: date) -> int:
    if role == "backend_api":
        frame = parse_backend_api_created(source)
        return int(local_date_mask(frame["Backend Created GMT+6"], target_date).sum())
    if role == "backend_bridgerpay":
        frame = parse_backend_bridgerpay_full(source)
    elif role == "backend_payprocc":
        frame = parse_backend_payprocc_full(source)
    elif role == "backend_coinsbuy":
        frame, _ = parse_backend_coinsbuy_full(source)
    elif role == "backend_confirmo":
        frame = parse_backend_confirmo_full(source)
    elif role == "backend_zen":
        frame = parse_backend_zen_full(source)
    else:
        return 0
    return int(local_date_mask(frame["Orchestrator Business GMT+6"], target_date).sum())


def auto_assign_backend_uploaded_files(
    uploaded_files: Iterable[Any] | None,
    target_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources = list(uploaded_files or [])
    assigned: dict[str, Any] = {}
    mapping: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[int, Any]]] = {}

    for index, source in enumerate(sources):
        role, detail = detect_backend_uploaded_file_type(source)
        mapping.append({
            "File Name": _source_name(source, index),
            "Detected Type": "Unrecognized" if role is None else BACKEND_FILE_SLOT_LABELS[role],
            "Assigned Slot": "",
            "Status": "Unrecognized" if role is None else "Detected",
            "Details": detail,
        })
        if role is not None:
            candidates.setdefault(role, []).append((index, source))

    for role, role_candidates in candidates.items():
        if len(role_candidates) == 1:
            index, source = role_candidates[0]
            assigned[role] = source
            mapping[index]["Assigned Slot"] = BACKEND_FILE_SLOT_LABELS[role]
            mapping[index]["Status"] = "Assigned"
            continue

        scored: list[tuple[int, int, Any, str]] = []
        for index, source in role_candidates:
            try:
                count = _backend_target_count(role, source, target_date)
                error = ""
            except Exception as exc:  # noqa: BLE001
                count = -1
                error = str(exc)
            scored.append((count, index, source, error))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_count, best_index, best_source, _ = scored[0]
        assigned[role] = best_source
        mapping[best_index]["Assigned Slot"] = BACKEND_FILE_SLOT_LABELS[role]
        mapping[best_index]["Status"] = "Assigned"
        mapping[best_index]["Details"] = f"Selected from {len(role_candidates)} files; {max(best_count, 0)} target-date rows"
        for count, index, _source, error in scored[1:]:
            mapping[index]["Status"] = "Ignored duplicate"
            mapping[index]["Details"] = error or (
                f"Another {BACKEND_FILE_SLOT_LABELS[role]} file had more target-date rows "
                f"({max(best_count, 0)} vs {max(count, 0)})"
            )
    return assigned, mapping


def _dedupe_backend_frame(df: pd.DataFrame, key_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return _dedupe(df, key_col)


def reconcile_orchestrator_to_backend(
    *,
    name: str,
    orchestrator: str,
    backend_gateway: str,
    orchestrator_full: pd.DataFrame,
    backend_all: pd.DataFrame,
    target_date: date,
    orch_key: str,
    orch_time: str,
    orch_amount: str,
    orch_currency: str,
    tracking_col: str | None = None,
    amount_tolerance: float = 0.01,
    amount_variances_allowed: bool = False,
    notes: list[str] | None = None,
    extra_orch_columns: list[str] | None = None,
    excluded_count: int = 0,
    excluded_value: float = 0.0,
) -> BackendReconciliationResult:
    notes = notes or []
    extra_orch_columns = extra_orch_columns or []

    orch_full = orchestrator_full.copy()
    orch_target = orch_full[local_date_mask(orch_full[orch_time], target_date)].copy()
    backend_route = backend_all[string_series(backend_all["Gateway"]).eq(backend_gateway)].copy()
    backend_target = backend_route[local_date_mask(backend_route["Backend Created GMT+6"], target_date)].copy()

    orch_target_unique, orch_target_duplicates = _dedupe_backend_frame(orch_target, orch_key)
    orch_full_unique, _ = _dedupe_backend_frame(orch_full, orch_key)
    backend_full_unique, backend_duplicates = _dedupe_backend_frame(backend_route, "Transaction ID")
    backend_target_unique, _ = _dedupe_backend_frame(backend_target, "Transaction ID")

    orch_target_by_key = {row["_match_key"]: row for _, row in orch_target_unique.iterrows()}
    orch_full_by_key = {row["_match_key"]: row for _, row in orch_full_unique.iterrows()}
    backend_full_by_key = {row["_match_key"]: row for _, row in backend_full_unique.iterrows()}
    backend_target_by_key = {row["_match_key"]: row for _, row in backend_target_unique.iterrows()}

    keys = sorted(set(orch_target_by_key) | set(backend_target_by_key))
    rows: list[dict[str, Any]] = []

    for key in keys:
        orch = orch_target_by_key.get(key)
        backend = backend_full_by_key.get(key) if orch is not None else backend_target_by_key.get(key)
        adjacent_orch = None

        if orch is not None and backend is not None:
            backend_date = backend["Backend Created GMT+6"].date()
            if backend_date == target_date:
                classification = "MATCHED SAME CREATED DATE"
            elif backend_date < target_date:
                classification = "MATCHED PRIOR BACKEND CREATED DATE"
            else:
                classification = "MATCHED NEXT BACKEND CREATED DATE"
        elif orch is not None:
            classification = "ORCHESTRATOR ONLY"
        else:
            adjacent_orch = orch_full_by_key.get(key)
            if adjacent_orch is not None:
                classification = "BACKEND MATCHED TO ADJACENT ORCHESTRATOR DATE"
                orch = adjacent_orch
            else:
                classification = "BACKEND ONLY - ADJACENT REPORT NEEDED"

        orch_amount_value = float(pd.to_numeric(pd.Series([orch.get(orch_amount) if orch is not None else np.nan]), errors="coerce").iloc[0]) if orch is not None else np.nan
        backend_amount_value = float(pd.to_numeric(pd.Series([backend.get("Grand Total") if backend is not None else np.nan]), errors="coerce").iloc[0]) if backend is not None else np.nan
        difference = backend_amount_value - orch_amount_value if pd.notna(orch_amount_value) and pd.notna(backend_amount_value) else np.nan

        target_match = classification.startswith("MATCHED ")
        if target_match and pd.notna(difference):
            raw_amount_check = "MATCH" if round(abs(difference), 2) == 0 else "DIFFERENCE"
            if abs(difference) <= amount_tolerance + 1e-9:
                amount_check = "MATCH"
            elif amount_variances_allowed:
                amount_check = "VARIANCE"
            else:
                amount_check = "MISMATCH"
        else:
            raw_amount_check = "NOT APPLICABLE"
            amount_check = "NOT APPLICABLE"

        orch_currency_value = _norm_currency(orch.get(orch_currency)) if orch is not None else ""
        backend_currency_value = "USD" if backend is not None else ""
        currency_check = (
            "MATCH" if target_match and orch_currency_value == backend_currency_value
            else "MISMATCH" if target_match
            else "NOT APPLICABLE"
        )

        orch_tracking = _norm_key(orch.get(tracking_col)) if orch is not None and tracking_col else ""
        backend_tracking = _norm_key(backend.get("Tracking ID")) if backend is not None and tracking_col else ""
        if not tracking_col or not target_match:
            tracking_check = "NOT CONFIGURED" if not tracking_col else "NOT APPLICABLE"
        elif not orch_tracking and not backend_tracking:
            tracking_check = "NOT AVAILABLE"
        elif orch_tracking == backend_tracking:
            tracking_check = "MATCH"
        else:
            tracking_check = "MISMATCH"

        if classification == "ORCHESTRATOR ONLY":
            review_note = "Not found anywhere in the supplied backend file."
        elif classification == "BACKEND ONLY - ADJACENT REPORT NEEDED":
            review_note = "Check the adjacent-day orchestrator report."
        elif classification == "BACKEND MATCHED TO ADJACENT ORCHESTRATOR DATE":
            review_note = (
                f"Orchestrator business timestamp is {orch[orch_time]:%Y-%m-%d %H:%M:%S} GMT+6; "
                "not part of the selected orchestrator business date."
            )
        elif classification == "MATCHED PRIOR BACKEND CREATED DATE":
            review_note = f"Backend Created At is {backend['Backend Created GMT+6']:%Y-%m-%d %H:%M:%S} GMT+6."
        elif classification == "MATCHED NEXT BACKEND CREATED DATE":
            review_note = f"Backend Created At is {backend['Backend Created GMT+6']:%Y-%m-%d %H:%M:%S} GMT+6."
        else:
            review_note = ""

        row: dict[str, Any] = {
            "Classification": classification,
            "Matching ID": key,
            "Orchestrator Business Time GMT+6": orch.get(orch_time) if orch is not None else pd.NaT,
            "Backend Created At GMT+6": backend.get("Backend Created GMT+6") if backend is not None else pd.NaT,
            "Backend Updated At GMT+6": backend.get("Backend Updated GMT+6") if backend is not None else pd.NaT,
            "Orchestrator Amount": orch_amount_value,
            "Backend Grand Total": backend_amount_value,
            "Amount Difference": difference,
            "Raw Amount Check": raw_amount_check,
            "Amount Check": amount_check,
            "Orchestrator Currency": orch_currency_value,
            "Backend Currency": backend_currency_value,
            "Currency Check": currency_check,
            "Orchestrator Tracking/Reference": orch_tracking,
            "Backend Tracking ID": backend_tracking,
            "Tracking Check": tracking_check,
            "Backend Order ID": backend.get("Order ID", "") if backend is not None else "",
            "Backend Customer Email": backend.get("Customer Email", "") if backend is not None else "",
            "Backend Plan": backend.get("Plan Name", "") if backend is not None else "",
            "Review Note": review_note,
        }
        for column in extra_orch_columns:
            row[f"Orchestrator Source {column}"] = orch.get(column, "") if orch is not None else ""
        rows.append(row)

    reconciliation = pd.DataFrame(rows)
    if reconciliation.empty:
        reconciliation = pd.DataFrame(columns=[
            "Classification", "Matching ID", "Orchestrator Business Time GMT+6",
            "Backend Created At GMT+6", "Backend Updated At GMT+6", "Orchestrator Amount",
            "Backend Grand Total", "Amount Difference", "Raw Amount Check", "Amount Check",
            "Orchestrator Currency", "Backend Currency", "Currency Check",
            "Orchestrator Tracking/Reference", "Backend Tracking ID", "Tracking Check",
            "Backend Order ID", "Backend Customer Email", "Backend Plan", "Review Note",
        ])

    target_match_mask = reconciliation["Classification"].isin([
        "MATCHED SAME CREATED DATE",
        "MATCHED PRIOR BACKEND CREATED DATE",
        "MATCHED NEXT BACKEND CREATED DATE",
    ])
    true_exception_mask = reconciliation["Classification"].isin([
        "ORCHESTRATOR ONLY",
        "BACKEND ONLY - ADJACENT REPORT NEEDED",
    ])
    mismatch_mask = (
        reconciliation["Amount Check"].eq("MISMATCH")
        | reconciliation["Tracking Check"].eq("MISMATCH")
        | reconciliation["Currency Check"].eq("MISMATCH")
    )
    exceptions = reconciliation[true_exception_mask | mismatch_mask].copy()

    duplicate_frames: list[pd.DataFrame] = []
    if not orch_target_duplicates.empty:
        dup = orch_target_duplicates.copy()
        dup.insert(0, "Classification", "ORCHESTRATOR DUPLICATE/BLANK KEY")
        duplicate_frames.append(dup)
    if not backend_duplicates.empty:
        dup = backend_duplicates.copy()
        dup.insert(0, "Classification", "BACKEND DUPLICATE/BLANK KEY")
        duplicate_frames.append(dup)
    if duplicate_frames:
        exceptions = pd.concat([exceptions, *duplicate_frames], ignore_index=True, sort=False)

    same_count = int(reconciliation["Classification"].eq("MATCHED SAME CREATED DATE").sum())
    prior_count = int(reconciliation["Classification"].eq("MATCHED PRIOR BACKEND CREATED DATE").sum())
    next_count = int(reconciliation["Classification"].eq("MATCHED NEXT BACKEND CREATED DATE").sum())
    matched = same_count + prior_count + next_count
    orch_only = int(reconciliation["Classification"].eq("ORCHESTRATOR ONLY").sum())
    adjacent_matched = int(reconciliation["Classification"].eq("BACKEND MATCHED TO ADJACENT ORCHESTRATOR DATE").sum())
    adjacent_needed = int(reconciliation["Classification"].eq("BACKEND ONLY - ADJACENT REPORT NEEDED").sum())
    raw_amount_differences = int((target_match_mask & reconciliation["Raw Amount Check"].eq("DIFFERENCE")).sum())
    amount_mismatch = int((target_match_mask & reconciliation["Amount Check"].eq("MISMATCH")).sum())
    amount_variance = int((target_match_mask & reconciliation["Amount Check"].eq("VARIANCE")).sum())
    tracking_mismatch = int((target_match_mask & reconciliation["Tracking Check"].eq("MISMATCH")).sum())
    currency_mismatch = int((target_match_mask & reconciliation["Currency Check"].eq("MISMATCH")).sum())

    if len(orch_target_unique) == 0 and len(backend_target_unique) == 0:
        status = "NO APPROVED DATA"
    elif (
        orch_only
        or adjacent_needed
        or amount_mismatch
        or tracking_mismatch
        or currency_mismatch
        or len(orch_target_duplicates)
        or len(backend_duplicates)
    ):
        status = "REVIEW REQUIRED"
    elif amount_variance:
        status = "MATCHED WITH AMOUNT VARIANCES"
    else:
        status = "FULL MATCH"

    summary = {
        "Orchestrator Count": int(len(orch_target_unique)),
        "Backend Created-Date Count": int(len(backend_target_unique)),
        "Matched": matched,
        "Same Created Date": same_count,
        "Prior Backend Created Date": prior_count,
        "Next Backend Created Date": next_count,
        "Orchestrator Only": orch_only,
        "Backend Adjacent Matched": adjacent_matched,
        "Backend Adjacent Report Needed": adjacent_needed,
        "Unmatched": orch_only + adjacent_needed,
        "Order Mismatch": 0,
        "Raw Amount Differences": raw_amount_differences,
        "Amount Mismatch": amount_mismatch,
        "Amount Variance": amount_variance,
        "Tracking Mismatch": tracking_mismatch,
        "Currency Mismatch": currency_mismatch,
        "Orchestrator Duplicate/Blank Keys": int(len(orch_target_duplicates)),
        "Backend Duplicate/Blank Keys": int(len(backend_duplicates)),
        "Excluded Internal Transfers": int(excluded_count),
        "Excluded Internal Value": float(excluded_value),
        "Orchestrator Value": float(pd.to_numeric(orch_target_unique[orch_amount], errors="coerce").fillna(0).sum()),
        "Matched Orchestrator Value": float(pd.to_numeric(reconciliation.loc[target_match_mask, "Orchestrator Amount"], errors="coerce").fillna(0).sum()),
        "Matched Backend Value": float(pd.to_numeric(reconciliation.loc[target_match_mask, "Backend Grand Total"], errors="coerce").fillna(0).sum()),
        "Orchestrator Only Value": float(pd.to_numeric(reconciliation.loc[reconciliation["Classification"].eq("ORCHESTRATOR ONLY"), "Orchestrator Amount"], errors="coerce").fillna(0).sum()),
    }

    audit = {
        "Backend business date field": "Created At",
        "Backend timezone conversion": "UTC+3 to GMT+6 (+3 hours)",
        "Backend Updated At": "Audit only; not used for business-date selection",
        "Orchestrator target rows before dedupe": len(orch_target),
        "Backend target rows before dedupe": len(backend_target),
        "Orchestrator full-file approved rows": len(orch_full),
        "Backend full-file route rows": len(backend_route),
        "Amount tolerance": amount_tolerance,
        "Amount variances allowed": amount_variances_allowed,
    }

    return BackendReconciliationResult(
        name=name,
        orchestrator=orchestrator,
        backend_gateway=backend_gateway,
        status=status,
        summary=summary,
        reconciliation=reconciliation,
        exceptions=exceptions,
        orchestrator_source=orch_target.copy(),
        backend_source=backend_target.copy(),
        notes=notes,
        audit=audit,
    )


def run_backend_reconciliations(
    files: dict[str, Any],
    target_date: date,
    amount_tolerance: float = 0.01,
) -> tuple[list[BackendReconciliationResult], list[dict[str, Any]]]:
    results: list[BackendReconciliationResult] = []
    file_audit: list[dict[str, Any]] = []

    backend_source = files.get("backend_api")
    if backend_source is None:
        file_audit.append({"File Slot": "Backend API", "Status": "Not uploaded", "Target-Date Rows": None, "Full Approved Rows": None, "Excluded Rows": None, "Error": ""})
        backend_all = None
    else:
        try:
            backend_all = parse_backend_api_created(backend_source)
            target_count = int(local_date_mask(backend_all["Backend Created GMT+6"], target_date).sum())
            file_audit.append({"File Slot": "Backend API", "Status": "Ready", "Target-Date Rows": target_count, "Full Approved Rows": len(backend_all), "Excluded Rows": 0, "Error": ""})
        except Exception as exc:  # noqa: BLE001
            backend_all = None
            file_audit.append({"File Slot": "Backend API", "Status": "Error", "Target-Date Rows": None, "Full Approved Rows": None, "Excluded Rows": None, "Error": str(exc)})

    parser_specs: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "backend_bridgerpay": ("BridgerPay", parse_backend_bridgerpay_full),
        "backend_payprocc": ("PayProcc", parse_backend_payprocc_full),
        "backend_coinsbuy": ("Coinsbuy", parse_backend_coinsbuy_full),
        "backend_confirmo": ("Confirmo", parse_backend_confirmo_full),
        "backend_zen": ("ZEN", parse_backend_zen_full),
    }
    parsed: dict[str, pd.DataFrame] = {}
    excluded: dict[str, pd.DataFrame] = {}

    for key, (label, parser) in parser_specs.items():
        source = files.get(key)
        if source is None:
            file_audit.append({"File Slot": label, "Status": "Not uploaded", "Target-Date Rows": None, "Full Approved Rows": None, "Excluded Rows": None, "Error": ""})
            continue
        try:
            parsed_value = parser(source)
            if key == "backend_coinsbuy":
                frame, excluded_frame = parsed_value
                excluded[key] = excluded_frame
            else:
                frame = parsed_value
            parsed[key] = frame
            target_count = int(local_date_mask(frame["Orchestrator Business GMT+6"], target_date).sum())
            file_audit.append({
                "File Slot": label,
                "Status": "Ready",
                "Target-Date Rows": target_count,
                "Full Approved Rows": len(frame),
                "Excluded Rows": len(excluded.get(key, pd.DataFrame())),
                "Error": "",
            })
        except Exception as exc:  # noqa: BLE001
            file_audit.append({"File Slot": label, "Status": "Error", "Target-Date Rows": None, "Full Approved Rows": None, "Excluded Rows": None, "Error": str(exc)})

    if backend_all is None:
        return results, file_audit

    def add_result(key: str, fn: Callable[[], BackendReconciliationResult]) -> None:
        if key not in parsed:
            return
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            file_audit.append({"File Slot": BACKEND_FILE_SLOT_LABELS[key], "Status": "Reconciliation error", "Target-Date Rows": None, "Full Approved Rows": None, "Excluded Rows": None, "Error": str(exc)})

    add_result(
        "backend_bridgerpay",
        lambda: reconcile_orchestrator_to_backend(
            name="BridgerPay vs Backend API",
            orchestrator="BridgerPay",
            backend_gateway="Bridger Pay",
            orchestrator_full=parsed["backend_bridgerpay"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="merchantOrderId",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="amount",
            orch_currency="currency",
            amount_tolerance=amount_tolerance,
            notes=["Backend Created At is the backend business-date field.", "BridgerPay merchantOrderId is matched to Backend Transaction ID."],
            extra_orch_columns=["transactionId", "pspOrderId", "pspName", "midAlias"],
        ),
    )
    add_result(
        "backend_payprocc",
        lambda: reconcile_orchestrator_to_backend(
            name="PayProcc vs Backend API",
            orchestrator="PayProcc",
            backend_gateway="Pay Procc",
            orchestrator_full=parsed["backend_payprocc"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="Merchant Order ID",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="Selected USD Amount",
            orch_currency="Selected Currency",
            amount_tolerance=amount_tolerance,
            notes=["PayProcc Amount is used for USD rows; Applied Amount is used where Applied Currency is USD."],
            extra_orch_columns=["Payment Public ID", "Gateway ID", "Transaction ID", "MID", "Currency", "Applied Currency"],
        ),
    )
    cb_excluded = excluded.get("backend_coinsbuy", pd.DataFrame())
    add_result(
        "backend_coinsbuy",
        lambda: reconcile_orchestrator_to_backend(
            name="Coinsbuy vs Backend API",
            orchestrator="Coinsbuy",
            backend_gateway="Crypto",
            orchestrator_full=parsed["backend_coinsbuy"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="Operation Number",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="Gross USD Equivalent",
            orch_currency="Selected Currency",
            tracking_col="Tracking ID",
            amount_tolerance=amount_tolerance,
            amount_variances_allowed=True,
            notes=["Coinsbuy Operation ID number is matched to Backend Transaction ID.", "Deposits over 2,500 with blank Tracking ID are excluded as internal transfers.", "Crypto conversion differences are shown as amount variances."],
            extra_orch_columns=["ID", "Operation ID", "Currency", "Amount", "Rate", "Target amount", "Target currency", "TXID"],
            excluded_count=len(cb_excluded),
            excluded_value=float(pd.to_numeric(cb_excluded.get("Gross USD Equivalent", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
        ),
    )
    add_result(
        "backend_confirmo",
        lambda: reconcile_orchestrator_to_backend(
            name="Confirmo vs Backend API",
            orchestrator="Confirmo",
            backend_gateway="Confirmo",
            orchestrator_full=parsed["backend_confirmo"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="ID",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="MerchantAmount",
            orch_currency="MerchantCurrency",
            tracking_col="Reference",
            amount_tolerance=amount_tolerance,
            notes=["Confirmo ID is matched to Backend Transaction ID; Reference is validated against Backend Tracking ID."],
            extra_orch_columns=["Reference", "CustomerAmount", "CustomerCurrency", "PaidAmount", "Credited", "Address"],
        ),
    )
    add_result(
        "backend_zen",
        lambda: reconcile_orchestrator_to_backend(
            name="ZEN vs Backend API",
            orchestrator="ZEN",
            backend_gateway="Zen Pay",
            orchestrator_full=parsed["backend_zen"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="merchant_transaction_id",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="transaction_amount",
            orch_currency="transaction_currency",
            amount_tolerance=amount_tolerance,
            notes=["Only Apple Pay and Google Pay purchase channels are included.", "payment_method may show Card for wallet transactions; payment_channel controls routing."],
            extra_orch_columns=["transaction_id", "payment_channel", "payment_method", "authorization_amount", "stl_amount"],
        ),
    )
    return results, file_audit


def backend_summary_dataframe(results: list[BackendReconciliationResult]) -> pd.DataFrame:
    columns = [
        "Orchestrator", "Backend Gateway", "Status", "Orchestrator Count", "Backend Created-Date Count",
        "Matched", "Unmatched", "Same Created Date", "Prior Backend Created Date", "Next Backend Created Date",
        "Orchestrator Only", "Backend Adjacent Matched", "Backend Adjacent Report Needed", "Order Mismatch",
        "Raw Amount Differences", "Amount Mismatch", "Amount Variance", "Tracking Mismatch", "Currency Mismatch",
        "Excluded Internal Transfers", "Orchestrator Value", "Matched Backend Value", "Orchestrator Only Value",
    ]
    rows: list[dict[str, Any]] = []
    for result in results:
        values = dict(result.summary or {})
        rows.append({
            "Orchestrator": result.orchestrator,
            "Backend Gateway": result.backend_gateway,
            "Status": result.status,
            **values,
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in frame.columns:
            frame[column] = 0 if column not in {"Orchestrator", "Backend Gateway", "Status"} else ""
    remaining = [c for c in frame.columns if c not in columns]
    return frame[columns + remaining]


def backend_exceptions_dataframe(results: list[BackendReconciliationResult]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for result in results:
        if result.exceptions.empty:
            continue
        frame = result.exceptions.copy()
        frame.insert(0, "Reconciliation", result.name)
        frame.insert(1, "Orchestrator", result.orchestrator)
        frame.insert(2, "Backend Gateway", result.backend_gateway)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def build_backend_excel_report(
    results: list[BackendReconciliationResult],
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

        summary = backend_summary_dataframe(results)
        summary.to_excel(writer, sheet_name="Summary", index=False, startrow=2)
        ws = writer.sheets["Summary"]
        ws.merge_range(0, 0, 0, max(0, len(summary.columns) - 1), f"Orchestrator to Backend Summary — {target_date.isoformat()} GMT+6 — Backend Created At", title_fmt)
        ws.set_row(0, 26)
        for col_idx, column in enumerate(summary.columns):
            ws.write(2, col_idx, column, header_fmt)
            ws.set_column(col_idx, col_idx, min(max(len(str(column)) + 2, 14), 28))
        if not summary.empty:
            status_col = summary.columns.get_loc("Status")
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "FULL MATCH", "format": green_fmt})
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "VARIANCES", "format": yellow_fmt})
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {"type": "text", "criteria": "containing", "value": "REVIEW", "format": red_fmt})
        for money_column in ["Orchestrator Value", "Matched Backend Value", "Orchestrator Only Value", "Excluded Internal Value"]:
            if money_column in summary.columns:
                idx = summary.columns.get_loc(money_column)
                ws.set_column(idx, idx, 20, money_fmt)
        ws.freeze_panes(3, 0)
        if len(summary.columns):
            ws.autofilter(2, 0, max(2, len(summary) + 2), len(summary.columns) - 1)

        if upload_mapping:
            mapping_df = pd.DataFrame(upload_mapping)
            mapping_df.to_excel(writer, sheet_name="Upload Mapping", index=False)
            mws = writer.sheets["Upload Mapping"]
            for col_idx, column in enumerate(mapping_df.columns):
                mws.write(0, col_idx, column, header_fmt)
                mws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 18), 55))
            mws.freeze_panes(1, 0)

        audit_df = pd.DataFrame(file_audit)
        audit_df.to_excel(writer, sheet_name="File Audit", index=False)
        aws = writer.sheets["File Audit"]
        for col_idx, column in enumerate(audit_df.columns):
            aws.write(0, col_idx, column, header_fmt)
            aws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 16), 50))
        aws.freeze_panes(1, 0)

        exceptions = backend_exceptions_dataframe(results)
        if exceptions.empty:
            pd.DataFrame({"Message": ["No exceptions found."]}).to_excel(writer, sheet_name="All Exceptions", index=False)
        else:
            exceptions.to_excel(writer, sheet_name="All Exceptions", index=False)
            ews = writer.sheets["All Exceptions"]
            for col_idx, column in enumerate(exceptions.columns):
                ews.write(0, col_idx, column, header_fmt)
                ews.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 14), 38))
            ews.freeze_panes(1, 0)

        used_names = {"Summary", "Upload Mapping", "File Audit", "All Exceptions"}
        for idx, result in enumerate(results, start=1):
            base = _safe_sheet_name(f"{idx:02d} {result.orchestrator}")
            sheet_name = base
            suffix_num = 2
            while sheet_name in used_names:
                suffix = f" {suffix_num}"
                sheet_name = _safe_sheet_name(base[:31-len(suffix)] + suffix)
                suffix_num += 1
            used_names.add(sheet_name)
            result.reconciliation.to_excel(writer, sheet_name=sheet_name, index=False)
            rws = writer.sheets[sheet_name]
            for col_idx, column in enumerate(result.reconciliation.columns):
                rws.write(0, col_idx, column, header_fmt)
                rws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 14), 38))
            rws.freeze_panes(1, 0)
            if not result.reconciliation.empty:
                rws.autofilter(0, 0, len(result.reconciliation), len(result.reconciliation.columns) - 1)
                class_col = result.reconciliation.columns.get_loc("Classification")
                rws.conditional_format(1, class_col, len(result.reconciliation), class_col, {"type": "text", "criteria": "containing", "value": "MATCHED", "format": green_fmt})
                rws.conditional_format(1, class_col, len(result.reconciliation), class_col, {"type": "text", "criteria": "containing", "value": "ONLY", "format": red_fmt})
                rws.conditional_format(1, class_col, len(result.reconciliation), class_col, {"type": "text", "criteria": "containing", "value": "ADJACENT", "format": yellow_fmt})
    return output.getvalue()

# ---------------------------------------------------------------------------
# Date-range helpers (v2.6)
# ---------------------------------------------------------------------------

def inclusive_date_range(start_date: date, end_date: date) -> list[date]:
    """Return every calendar date from start_date through end_date, inclusive."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def auto_assign_backend_uploaded_files_range(
    uploaded_files: Iterable[Any] | None,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Auto-assign backend-stage uploads using total approved rows across a date range.

    The range score matters only when multiple files have the same detected role. It
    prevents a file that has rows on just one day from being selected over the file
    that covers the requested range.
    """
    sources = list(uploaded_files or [])
    assigned: dict[str, Any] = {}
    mapping: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[int, Any]]] = {}
    dates = inclusive_date_range(start_date, end_date)

    for index, source in enumerate(sources):
        role, detail = detect_backend_uploaded_file_type(source)
        mapping.append({
            "File Name": _source_name(source, index),
            "Detected Type": "Unrecognized" if role is None else BACKEND_FILE_SLOT_LABELS[role],
            "Assigned Slot": "",
            "Status": "Unrecognized" if role is None else "Detected",
            "Details": detail,
        })
        if role is not None:
            candidates.setdefault(role, []).append((index, source))

    for role, role_candidates in candidates.items():
        if len(role_candidates) == 1:
            index, source = role_candidates[0]
            assigned[role] = source
            mapping[index]["Assigned Slot"] = BACKEND_FILE_SLOT_LABELS[role]
            mapping[index]["Status"] = "Assigned"
            mapping[index]["Details"] = (
                f"Selected for {start_date.isoformat()} to {end_date.isoformat()}"
            )
            continue

        scored: list[tuple[int, int, Any, str]] = []
        for index, source in role_candidates:
            try:
                count = sum(_backend_target_count(role, source, item_date) for item_date in dates)
                error = ""
            except Exception as exc:  # noqa: BLE001
                count = -1
                error = str(exc)
            scored.append((count, index, source, error))
        scored.sort(key=lambda item: item[0], reverse=True)
        best_count, best_index, best_source, _ = scored[0]
        assigned[role] = best_source
        mapping[best_index]["Assigned Slot"] = BACKEND_FILE_SLOT_LABELS[role]
        mapping[best_index]["Status"] = "Assigned"
        mapping[best_index]["Details"] = (
            f"Selected from {len(role_candidates)} files; {max(best_count, 0)} range rows"
        )
        for count, index, _source, error in scored[1:]:
            mapping[index]["Status"] = "Ignored duplicate"
            mapping[index]["Details"] = error or (
                f"Another {BACKEND_FILE_SLOT_LABELS[role]} file covered more requested-range rows "
                f"({max(best_count, 0)} vs {max(count, 0)})"
            )
    return assigned, mapping


def run_backend_reconciliation_range(
    files: dict[str, Any],
    start_date: date,
    end_date: date,
    amount_tolerance: float = 0.01,
) -> tuple[dict[date, list[BackendReconciliationResult]], dict[date, list[dict[str, Any]]]]:
    """Run backend reconciliation independently for every selected GMT+6 date.

    Returning a dictionary keyed by date prevents the UI from reusing one day's
    result for another selected date. Each daily result is produced using the
    existing single-date engine and therefore retains the same reconciliation rules.
    """
    results_by_date: dict[date, list[BackendReconciliationResult]] = {}
    audit_by_date: dict[date, list[dict[str, Any]]] = {}
    for item_date in inclusive_date_range(start_date, end_date):
        daily_results, daily_audit = run_backend_reconciliations(
            files,
            item_date,
            amount_tolerance,
        )
        results_by_date[item_date] = daily_results
        audit_by_date[item_date] = daily_audit
    return results_by_date, audit_by_date


def backend_range_summary_dataframe(
    results_by_date: dict[date, list[BackendReconciliationResult]],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item_date in sorted(results_by_date):
        frame = backend_summary_dataframe(results_by_date[item_date])
        if frame.empty:
            continue
        frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
        frames.append(frame)
    if not frames:
        base = backend_summary_dataframe([])
        base.insert(0, "Reconciliation Date GMT+6", pd.Series(dtype="datetime64[ns]"))
        return base
    return pd.concat(frames, ignore_index=True, sort=False)


def backend_range_exceptions_dataframe(
    results_by_date: dict[date, list[BackendReconciliationResult]],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item_date in sorted(results_by_date):
        frame = backend_exceptions_dataframe(results_by_date[item_date])
        if frame.empty:
            continue
        frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
        frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def build_backend_range_excel_report(
    results_by_date: dict[date, list[BackendReconciliationResult]],
    audit_by_date: dict[date, list[dict[str, Any]]],
    start_date: date,
    end_date: date,
    upload_mapping: list[dict[str, Any]] | None = None,
) -> bytes:
    """Create one evidence workbook containing all dates in the selected range."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
        workbook = writer.book
        header_fmt = workbook.add_format({
            "bold": True, "font_color": "white", "bg_color": "#4472C4",
            "border": 1, "align": "center", "valign": "vcenter",
        })
        title_fmt = workbook.add_format({
            "bold": True, "font_color": "white", "bg_color": "#17365D",
            "font_size": 16, "align": "center", "valign": "vcenter",
        })
        green_fmt = workbook.add_format({"bg_color": "#E2F0D9", "font_color": "#375623"})
        yellow_fmt = workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"})
        red_fmt = workbook.add_format({"bg_color": "#FCE4D6", "font_color": "#C00000"})
        money_fmt = workbook.add_format({"num_format": "$#,##0.00;[Red]-$#,##0.00"})
        date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})

        summary = backend_range_summary_dataframe(results_by_date)
        summary.to_excel(writer, sheet_name="Daily Summary", index=False, startrow=2)
        ws = writer.sheets["Daily Summary"]
        ws.merge_range(
            0, 0, 0, max(0, len(summary.columns) - 1),
            f"Orchestrator to Backend Summary — {start_date.isoformat()} to {end_date.isoformat()} GMT+6",
            title_fmt,
        )
        ws.set_row(0, 26)
        for col_idx, column in enumerate(summary.columns):
            ws.write(2, col_idx, column, header_fmt)
            ws.set_column(col_idx, col_idx, min(max(len(str(column)) + 2, 14), 29))
        if not summary.empty:
            date_col = summary.columns.get_loc("Reconciliation Date GMT+6")
            ws.set_column(date_col, date_col, 18, date_fmt)
            status_col = summary.columns.get_loc("Status")
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "FULL MATCH", "format": green_fmt,
            })
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "VARIANCES", "format": yellow_fmt,
            })
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "REVIEW", "format": red_fmt,
            })
        for money_column in [
            "Orchestrator Value", "Matched Backend Value", "Orchestrator Only Value",
            "Excluded Internal Value",
        ]:
            if money_column in summary.columns:
                idx = summary.columns.get_loc(money_column)
                ws.set_column(idx, idx, 20, money_fmt)
        ws.freeze_panes(3, 1)
        if len(summary.columns):
            ws.autofilter(2, 0, max(2, len(summary) + 2), len(summary.columns) - 1)

        exceptions = backend_range_exceptions_dataframe(results_by_date)
        if exceptions.empty:
            pd.DataFrame({"Message": ["No exceptions found in the selected date range."]}).to_excel(
                writer, sheet_name="All Exceptions", index=False
            )
        else:
            exceptions.to_excel(writer, sheet_name="All Exceptions", index=False)
            ews = writer.sheets["All Exceptions"]
            for col_idx, column in enumerate(exceptions.columns):
                ews.write(0, col_idx, column, header_fmt)
                ews.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 16), 38))
            ews.freeze_panes(1, 1)
            ews.autofilter(0, 0, len(exceptions), len(exceptions.columns) - 1)

        if upload_mapping:
            mapping_df = pd.DataFrame(upload_mapping)
            mapping_df.to_excel(writer, sheet_name="Upload Mapping", index=False)
            mws = writer.sheets["Upload Mapping"]
            for col_idx, column in enumerate(mapping_df.columns):
                mws.write(0, col_idx, column, header_fmt)
                mws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 18), 55))
            mws.freeze_panes(1, 0)

        audit_frames: list[pd.DataFrame] = []
        for item_date in sorted(audit_by_date):
            frame = pd.DataFrame(audit_by_date[item_date])
            if frame.empty:
                continue
            frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
            audit_frames.append(frame)
        audit_df = pd.concat(audit_frames, ignore_index=True, sort=False) if audit_frames else pd.DataFrame()
        audit_df.to_excel(writer, sheet_name="File Audit", index=False)
        aws = writer.sheets["File Audit"]
        for col_idx, column in enumerate(audit_df.columns):
            aws.write(0, col_idx, column, header_fmt)
            aws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 16), 50))
        aws.freeze_panes(1, 1)

        # Range workbooks keep one consolidated reconciliation sheet per route.
        # Source-row sheets are intentionally omitted because they duplicate the same
        # large uploads across dates and make range exports unnecessarily slow.
        route_names = ["BridgerPay", "PayProcc", "Coinsbuy", "Confirmo", "ZEN"]
        for route_name in route_names:
            detail_frames: list[pd.DataFrame] = []
            for item_date in sorted(results_by_date):
                selected = [r for r in results_by_date[item_date] if r.orchestrator == route_name]
                if not selected:
                    continue
                result = selected[0]
                if result.reconciliation.empty:
                    continue
                frame = result.reconciliation.copy()
                frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
                detail_frames.append(frame)
            if not detail_frames:
                continue
            frame = pd.concat(detail_frames, ignore_index=True, sort=False)
            sheet_name = _safe_sheet_name(f"{route_name} Recon")
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            rws = writer.sheets[sheet_name]
            for col_idx, column in enumerate(frame.columns):
                rws.write(0, col_idx, column, header_fmt)
                rws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 14), 36))
            rws.freeze_panes(1, 1)
            rws.autofilter(0, 0, len(frame), len(frame.columns) - 1)

    output.seek(0)
    return output.getvalue()

# ---------------------------------------------------------------------------
# Optimized date-range execution (parses each upload once)
# ---------------------------------------------------------------------------

def _build_backend_daily_results_from_parsed(
    *,
    backend_all: pd.DataFrame,
    parsed: dict[str, pd.DataFrame],
    excluded: dict[str, pd.DataFrame],
    target_date: date,
    amount_tolerance: float,
    file_audit: list[dict[str, Any]],
) -> list[BackendReconciliationResult]:
    results: list[BackendReconciliationResult] = []

    def add_result(key: str, fn: Callable[[], BackendReconciliationResult]) -> None:
        if key not in parsed:
            return
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            file_audit.append({
                "File Slot": BACKEND_FILE_SLOT_LABELS[key],
                "Status": "Reconciliation error",
                "Target-Date Rows": None,
                "Full Approved Rows": None,
                "Excluded Rows": None,
                "Error": str(exc),
            })

    add_result(
        "backend_bridgerpay",
        lambda: reconcile_orchestrator_to_backend(
            name="BridgerPay vs Backend API",
            orchestrator="BridgerPay",
            backend_gateway="Bridger Pay",
            orchestrator_full=parsed["backend_bridgerpay"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="merchantOrderId",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="amount",
            orch_currency="currency",
            amount_tolerance=amount_tolerance,
            notes=[
                "Backend Created At is the backend business-date field.",
                "BridgerPay merchantOrderId is matched to Backend Transaction ID.",
            ],
            extra_orch_columns=["transactionId", "pspOrderId", "pspName", "midAlias"],
        ),
    )
    add_result(
        "backend_payprocc",
        lambda: reconcile_orchestrator_to_backend(
            name="PayProcc vs Backend API",
            orchestrator="PayProcc",
            backend_gateway="Pay Procc",
            orchestrator_full=parsed["backend_payprocc"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="Merchant Order ID",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="Selected USD Amount",
            orch_currency="Selected Currency",
            amount_tolerance=amount_tolerance,
            notes=[
                "PayProcc Amount is used for USD rows; Applied Amount is used where Applied Currency is USD."
            ],
            extra_orch_columns=[
                "Payment Public ID", "Gateway ID", "Transaction ID", "MID", "Currency", "Applied Currency"
            ],
        ),
    )

    cb_excluded_all = excluded.get("backend_coinsbuy", pd.DataFrame())
    if not cb_excluded_all.empty and "Orchestrator Business GMT+6" in cb_excluded_all.columns:
        cb_excluded = cb_excluded_all[
            local_date_mask(cb_excluded_all["Orchestrator Business GMT+6"], target_date)
        ].copy()
    else:
        cb_excluded = cb_excluded_all

    add_result(
        "backend_coinsbuy",
        lambda: reconcile_orchestrator_to_backend(
            name="Coinsbuy vs Backend API",
            orchestrator="Coinsbuy",
            backend_gateway="Crypto",
            orchestrator_full=parsed["backend_coinsbuy"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="Operation Number",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="Gross USD Equivalent",
            orch_currency="Selected Currency",
            tracking_col="Tracking ID",
            amount_tolerance=amount_tolerance,
            amount_variances_allowed=True,
            notes=[
                "Coinsbuy Operation ID number is matched to Backend Transaction ID.",
                "Deposits over 2,500 with blank Tracking ID are excluded as internal transfers.",
                "Crypto conversion differences are shown as amount variances.",
            ],
            extra_orch_columns=[
                "ID", "Operation ID", "Currency", "Amount", "Rate", "Target amount", "Target currency", "TXID"
            ],
            excluded_count=len(cb_excluded),
            excluded_value=float(
                pd.to_numeric(
                    cb_excluded.get("Gross USD Equivalent", pd.Series(dtype=float)), errors="coerce"
                ).fillna(0).sum()
            ),
        ),
    )
    add_result(
        "backend_confirmo",
        lambda: reconcile_orchestrator_to_backend(
            name="Confirmo vs Backend API",
            orchestrator="Confirmo",
            backend_gateway="Confirmo",
            orchestrator_full=parsed["backend_confirmo"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="ID",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="MerchantAmount",
            orch_currency="MerchantCurrency",
            tracking_col="Reference",
            amount_tolerance=amount_tolerance,
            notes=[
                "Confirmo ID is matched to Backend Transaction ID; Reference is validated against Backend Tracking ID."
            ],
            extra_orch_columns=[
                "Reference", "CustomerAmount", "CustomerCurrency", "PaidAmount", "Credited", "Address"
            ],
        ),
    )
    add_result(
        "backend_zen",
        lambda: reconcile_orchestrator_to_backend(
            name="ZEN vs Backend API",
            orchestrator="ZEN",
            backend_gateway="Zen Pay",
            orchestrator_full=parsed["backend_zen"],
            backend_all=backend_all,
            target_date=target_date,
            orch_key="merchant_transaction_id",
            orch_time="Orchestrator Business GMT+6",
            orch_amount="transaction_amount",
            orch_currency="transaction_currency",
            amount_tolerance=amount_tolerance,
            notes=[
                "Only Apple Pay and Google Pay purchase channels are included.",
                "payment_method may show Card for wallet transactions; payment_channel controls routing.",
            ],
            extra_orch_columns=[
                "transaction_id", "payment_channel", "payment_method", "authorization_amount", "stl_amount"
            ],
        ),
    )
    return results


def run_backend_reconciliation_range(
    files: dict[str, Any],
    start_date: date,
    end_date: date,
    amount_tolerance: float = 0.01,
) -> tuple[dict[date, list[BackendReconciliationResult]], dict[date, list[dict[str, Any]]]]:
    """Run each requested GMT+6 date while parsing every uploaded file only once."""
    dates = inclusive_date_range(start_date, end_date)
    results_by_date: dict[date, list[BackendReconciliationResult]] = {}
    audit_by_date: dict[date, list[dict[str, Any]]] = {}

    backend_all: pd.DataFrame | None = None
    backend_error = ""
    backend_source = files.get("backend_api")
    if backend_source is not None:
        try:
            backend_all = parse_backend_api_created(backend_source)
        except Exception as exc:  # noqa: BLE001
            backend_error = str(exc)

    parser_specs: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "backend_bridgerpay": ("BridgerPay", parse_backend_bridgerpay_full),
        "backend_payprocc": ("PayProcc", parse_backend_payprocc_full),
        "backend_coinsbuy": ("Coinsbuy", parse_backend_coinsbuy_full),
        "backend_confirmo": ("Confirmo", parse_backend_confirmo_full),
        "backend_zen": ("ZEN", parse_backend_zen_full),
    }
    parsed: dict[str, pd.DataFrame] = {}
    excluded: dict[str, pd.DataFrame] = {}
    parse_errors: dict[str, str] = {}

    for key, (_label, parser) in parser_specs.items():
        source = files.get(key)
        if source is None:
            continue
        try:
            parsed_value = parser(source)
            if key == "backend_coinsbuy":
                frame, excluded_frame = parsed_value
                excluded[key] = excluded_frame
            else:
                frame = parsed_value
            parsed[key] = frame
        except Exception as exc:  # noqa: BLE001
            parse_errors[key] = str(exc)

    for item_date in dates:
        file_audit: list[dict[str, Any]] = []
        if backend_source is None:
            file_audit.append({
                "File Slot": "Backend API", "Status": "Not uploaded", "Target-Date Rows": None,
                "Full Approved Rows": None, "Excluded Rows": None, "Error": "",
            })
        elif backend_all is None:
            file_audit.append({
                "File Slot": "Backend API", "Status": "Error", "Target-Date Rows": None,
                "Full Approved Rows": None, "Excluded Rows": None, "Error": backend_error,
            })
        else:
            file_audit.append({
                "File Slot": "Backend API", "Status": "Ready",
                "Target-Date Rows": int(local_date_mask(backend_all["Backend Created GMT+6"], item_date).sum()),
                "Full Approved Rows": len(backend_all), "Excluded Rows": 0, "Error": "",
            })

        for key, (label, _parser) in parser_specs.items():
            source = files.get(key)
            if source is None:
                file_audit.append({
                    "File Slot": label, "Status": "Not uploaded", "Target-Date Rows": None,
                    "Full Approved Rows": None, "Excluded Rows": None, "Error": "",
                })
                continue
            if key in parse_errors:
                file_audit.append({
                    "File Slot": label, "Status": "Error", "Target-Date Rows": None,
                    "Full Approved Rows": None, "Excluded Rows": None, "Error": parse_errors[key],
                })
                continue
            frame = parsed[key]
            excluded_frame = excluded.get(key, pd.DataFrame())
            if not excluded_frame.empty and "Orchestrator Business GMT+6" in excluded_frame.columns:
                excluded_count = int(
                    local_date_mask(excluded_frame["Orchestrator Business GMT+6"], item_date).sum()
                )
            else:
                excluded_count = 0
            file_audit.append({
                "File Slot": label, "Status": "Ready",
                "Target-Date Rows": int(
                    local_date_mask(frame["Orchestrator Business GMT+6"], item_date).sum()
                ),
                "Full Approved Rows": len(frame), "Excluded Rows": excluded_count, "Error": "",
            })

        if backend_all is None:
            results_by_date[item_date] = []
        else:
            results_by_date[item_date] = _build_backend_daily_results_from_parsed(
                backend_all=backend_all,
                parsed=parsed,
                excluded=excluded,
                target_date=item_date,
                amount_tolerance=amount_tolerance,
                file_audit=file_audit,
            )
        audit_by_date[item_date] = file_audit

    return results_by_date, audit_by_date

# ---------------------------------------------------------------------------
# Shared date-range helpers for PSP stage (v2.7)
# ---------------------------------------------------------------------------

def auto_assign_uploaded_files_range(
    uploaded_files: Iterable[Any] | None,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assign PSP-stage uploads using all requested dates, not one reference date."""
    sources = list(uploaded_files or [])
    assigned: dict[str, Any] = {}
    mapping: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[int, Any]]] = {}
    dates = inclusive_date_range(start_date, end_date)

    for index, source in enumerate(sources):
        role, detail = detect_uploaded_file_type(source)
        row = {
            "File Name": _source_name(source, index),
            "Detected Type": "Unrecognized" if role is None else (
                "Nuvei / SafeCharge" if role == "nuvei" else FILE_SLOT_LABELS.get(role, role)
            ),
            "Assigned Slot": "",
            "Status": "Unrecognized" if role is None else "Detected",
            "Details": detail,
        }
        mapping.append(row)
        if role is not None:
            candidates.setdefault(role, []).append((index, source))

    # Select one source per structural role using the total requested-range rows.
    for role, role_candidates in candidates.items():
        if role == "nuvei":
            continue
        if len(role_candidates) == 1:
            index, source = role_candidates[0]
            assigned[role] = source
            mapping[index]["Assigned Slot"] = FILE_SLOT_LABELS[role]
            mapping[index]["Status"] = "Assigned"
            mapping[index]["Details"] = (
                f"Selected for {start_date.isoformat()} to {end_date.isoformat()}"
            )
            continue

        scored: list[tuple[int, int, Any, str]] = []
        for index, source in role_candidates:
            try:
                count = sum(_approved_count_for_role(role, source, item_date) for item_date in dates)
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
        mapping[best_index]["Details"] = (
            f"Selected from {len(role_candidates)} files; {max(best_count, 0)} requested-range rows"
        )
        for count, index, _source, error in scored[1:]:
            mapping[index]["Status"] = "Ignored duplicate"
            mapping[index]["Details"] = error or (
                f"Another {FILE_SLOT_LABELS[role]} file covered more requested-range rows "
                f"({max(best_count, 0)} vs {max(count, 0)})"
            )

    # Nuvei EU and AE have the same columns. Determine the region from overlap
    # against the full requested-range SafeCharge populations in BridgerPay.
    nuvei_candidates = candidates.get("nuvei", [])
    if nuvei_candidates:
        region_sets: dict[str, set[str]] = {"EU": set(), "AE": set()}
        bp_source = assigned.get("bridgerpay")
        if bp_source is not None:
            for item_date in dates:
                try:
                    bp = parse_bridgerpay(bp_source, item_date)
                    approved = string_series(bp["status"]).str.lower().eq("approved")
                    for region in ("EU", "AE"):
                        alias = f"SafeCharge-CreditCard-MID-{region}"
                        region_sets[region].update(
                            string_series(
                                bp.loc[approved & string_series(bp["midAlias"]).eq(alias), "pspOrderId"]
                            ).map(_norm_key)
                        )
                except Exception:  # noqa: BLE001
                    continue

        score_rows: list[tuple[int, int, str, Any, int]] = []
        for index, source in nuvei_candidates:
            ids: set[str] = set()
            parse_error = ""
            for item_date in dates:
                try:
                    frame = parse_nuvei(source, item_date)
                    ids.update(string_series(frame["Transaction ID"]).map(_norm_key))
                except Exception as exc:  # noqa: BLE001
                    parse_error = str(exc)
                    break
            if parse_error:
                mapping[index]["Status"] = "Needs review"
                mapping[index]["Details"] = f"Nuvei file could not be parsed: {parse_error}"
            for region in ("EU", "AE"):
                score_rows.append((len(ids & region_sets[region]), index, region, source, len(ids)))

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
            mapping[index]["Details"] = (
                f"{score} Transaction IDs matched BridgerPay SafeCharge {region}; "
                f"{row_count} requested-range rows"
            )

        # Filename and elimination fallbacks remain transparent in the mapping.
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
                mapping[index]["Details"] = (
                    "Could not determine whether this is Nuvei EU or Nuvei AE; "
                    "upload BridgerPay with the Nuvei files"
                )

    return assigned, mapping


def run_psp_reconciliation_range(
    files: dict[str, Any],
    start_date: date,
    end_date: date,
    amount_tolerance: float = 0.01,
) -> tuple[dict[date, list[ReconciliationResult]], dict[date, list[dict[str, Any]]]]:
    """Reconcile PSP routes independently for every selected GMT+6 date."""
    results_by_date: dict[date, list[ReconciliationResult]] = {}
    audit_by_date: dict[date, list[dict[str, Any]]] = {}
    for item_date in inclusive_date_range(start_date, end_date):
        daily_results, daily_audit = run_all_reconciliations(files, item_date, amount_tolerance)
        results_by_date[item_date] = daily_results
        audit_by_date[item_date] = daily_audit
    return results_by_date, audit_by_date


def psp_range_summary_dataframe(
    results_by_date: dict[date, list[ReconciliationResult]],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item_date in sorted(results_by_date):
        frame = summary_dataframe(results_by_date[item_date])
        if frame.empty:
            continue
        frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
        frames.append(frame)
    if not frames:
        base = summary_dataframe([])
        base.insert(0, "Reconciliation Date GMT+6", pd.Series(dtype="datetime64[ns]"))
        return base
    return pd.concat(frames, ignore_index=True, sort=False)


def psp_range_exceptions_dataframe(
    results_by_date: dict[date, list[ReconciliationResult]],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item_date in sorted(results_by_date):
        frame = exceptions_dataframe(results_by_date[item_date])
        if frame.empty:
            continue
        frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
        frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def build_psp_range_excel_report(
    results_by_date: dict[date, list[ReconciliationResult]],
    audit_by_date: dict[date, list[dict[str, Any]]],
    start_date: date,
    end_date: date,
    upload_mapping: list[dict[str, Any]] | None = None,
) -> bytes:
    """Create one PSP-stage evidence workbook for a single date or a date range."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
        workbook = writer.book
        header_fmt = workbook.add_format({
            "bold": True, "font_color": "white", "bg_color": "#4472C4",
            "border": 1, "align": "center", "valign": "vcenter",
        })
        title_fmt = workbook.add_format({
            "bold": True, "font_color": "white", "bg_color": "#17365D",
            "font_size": 16, "align": "center", "valign": "vcenter",
        })
        green_fmt = workbook.add_format({"bg_color": "#E2F0D9", "font_color": "#375623"})
        yellow_fmt = workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"})
        red_fmt = workbook.add_format({"bg_color": "#FCE4D6", "font_color": "#C00000"})
        date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})

        summary = psp_range_summary_dataframe(results_by_date)
        summary.to_excel(writer, sheet_name="Daily Summary", index=False, startrow=2)
        ws = writer.sheets["Daily Summary"]
        ws.merge_range(
            0, 0, 0, max(0, len(summary.columns) - 1),
            f"PSP to Orchestrator Summary — {start_date.isoformat()} to {end_date.isoformat()} GMT+6",
            title_fmt,
        )
        ws.set_row(0, 26)
        for col_idx, column in enumerate(summary.columns):
            ws.write(2, col_idx, column, header_fmt)
            ws.set_column(col_idx, col_idx, min(max(len(str(column)) + 2, 14), 30))
        if not summary.empty:
            date_col = summary.columns.get_loc("Reconciliation Date GMT+6")
            ws.set_column(date_col, date_col, 18, date_fmt)
            status_col = summary.columns.get_loc("Status")
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "FULL MATCH", "format": green_fmt,
            })
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "VARIANCES", "format": yellow_fmt,
            })
            ws.conditional_format(3, status_col, len(summary) + 2, status_col, {
                "type": "text", "criteria": "containing", "value": "REVIEW", "format": red_fmt,
            })
        ws.freeze_panes(3, 1)
        if len(summary.columns):
            ws.autofilter(2, 0, max(2, len(summary) + 2), len(summary.columns) - 1)

        exceptions = psp_range_exceptions_dataframe(results_by_date)
        if exceptions.empty:
            pd.DataFrame({"Message": ["No exceptions found in the selected date range."]}).to_excel(
                writer, sheet_name="All Exceptions", index=False
            )
        else:
            exceptions.to_excel(writer, sheet_name="All Exceptions", index=False)
            ews = writer.sheets["All Exceptions"]
            for col_idx, column in enumerate(exceptions.columns):
                ews.write(0, col_idx, column, header_fmt)
                ews.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 16), 38))
            ews.freeze_panes(1, 1)
            ews.autofilter(0, 0, len(exceptions), len(exceptions.columns) - 1)

        if upload_mapping:
            mapping_df = pd.DataFrame(upload_mapping)
            mapping_df.to_excel(writer, sheet_name="Upload Mapping", index=False)
            mws = writer.sheets["Upload Mapping"]
            for col_idx, column in enumerate(mapping_df.columns):
                mws.write(0, col_idx, column, header_fmt)
                mws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 18), 55))
            mws.freeze_panes(1, 0)

        audit_frames: list[pd.DataFrame] = []
        for item_date in sorted(audit_by_date):
            frame = pd.DataFrame(audit_by_date[item_date])
            if frame.empty:
                continue
            frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
            audit_frames.append(frame)
        audit_df = pd.concat(audit_frames, ignore_index=True, sort=False) if audit_frames else pd.DataFrame()
        audit_df.to_excel(writer, sheet_name="File Audit", index=False)
        aws = writer.sheets["File Audit"]
        for col_idx, column in enumerate(audit_df.columns):
            aws.write(0, col_idx, column, header_fmt)
            aws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 16), 50))
        aws.freeze_panes(1, 1)

        # One consolidated detail sheet per route across the selected range.
        route_keys: list[tuple[str, str]] = []
        for item_date in sorted(results_by_date):
            for result in results_by_date[item_date]:
                key = (result.orchestrator, result.psp)
                if key not in route_keys:
                    route_keys.append(key)

        for orchestrator, psp in route_keys:
            detail_frames: list[pd.DataFrame] = []
            for item_date in sorted(results_by_date):
                selected = [
                    r for r in results_by_date[item_date]
                    if r.orchestrator == orchestrator and r.psp == psp
                ]
                if not selected or selected[0].reconciliation.empty:
                    continue
                frame = selected[0].reconciliation.copy()
                frame.insert(0, "Reconciliation Date GMT+6", pd.Timestamp(item_date))
                detail_frames.append(frame)
            if not detail_frames:
                continue
            frame = pd.concat(detail_frames, ignore_index=True, sort=False)
            sheet_name = _safe_sheet_name(f"{psp}-{orchestrator}")
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            rws = writer.sheets[sheet_name]
            for col_idx, column in enumerate(frame.columns):
                rws.write(0, col_idx, column, header_fmt)
                rws.set_column(col_idx, col_idx, min(max(len(str(column)) + 3, 14), 36))
            rws.freeze_panes(1, 1)
            rws.autofilter(0, 0, len(frame), len(frame.columns) - 1)

    output.seek(0)
    return output.getvalue()
