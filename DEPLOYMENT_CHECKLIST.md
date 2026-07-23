# Deployment checklist — v2.6

- [ ] Replace the existing `app.py` with the v2.6 file.
- [ ] Upload `reconciliation_engine_v26.py` to the same root directory.
- [ ] Remove or ignore older reconciliation engine files.
- [ ] Keep `requirements.txt` and `.streamlit/config.toml` from this package.
- [ ] Reboot the Streamlit application after uploading the files.
- [ ] Upload the backend API and orchestrator reports.
- [ ] Select a backend GMT+6 start and end date.
- [ ] Click **Run backend reconciliation** once for the full range.
- [ ] Confirm that the overview shows different route counts for each date.
- [ ] Use the **Detailed reconciliation date** selector to inspect one date in the route tabs.
