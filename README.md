# Payment Reconciliation Dashboard v2.4

This Streamlit dashboard performs bulk PSP-to-orchestrator reconciliation in GMT+6.

## Important deployment fix

Version 2.4 uses `reconciliation_engine_v24.py` instead of the old generic engine filename. This prevents Streamlit Cloud from importing a stale `reconciliation_engine.py` from a previous release.

Upload **all files from this package together** to the root of the GitHub repository:

- `app.py`
- `reconciliation_engine_v24.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `RECONCILIATION_LOGIC.md`

The old `reconciliation_engine.py` may be deleted. It is not used by v2.4.

## Streamlit Cloud update steps

1. Extract this ZIP.
2. In GitHub, replace `app.py` and upload `reconciliation_engine_v24.py` plus the other package files.
3. Delete the old `reconciliation_engine.py` to avoid confusion.
4. Commit the changes.
5. In Streamlit Cloud, open **Manage app → Reboot app**.
6. Confirm the main file path is `app.py`.

If the engine file is missing or the versions differ, the app now displays a clear deployment message instead of a redacted ImportError.

## Reconciliation output

The summary table contains:

- PSP and orchestrator transaction counts
- Matched count
- Unmatched count
- Order mismatch
- Amount mismatch
- Currency mismatch
- Route status

Timestamp differences remain available as audit evidence but are not counted as reconciliation mismatches.
