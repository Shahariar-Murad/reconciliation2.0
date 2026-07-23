# Deployment checklist — v2.7

- [ ] Replace the existing `app.py` with the v2.7 file.
- [ ] Upload `reconciliation_engine_v27.py` to the same root directory.
- [ ] Remove older files such as `reconciliation_engine_v26.py` to avoid version confusion.
- [ ] Keep `requirements.txt` and `.streamlit/config.toml` from this package.
- [ ] Reboot the Streamlit application after uploading the files.
- [ ] In **Global settings**, select one date or a start and end date.
- [ ] For a multi-day run, select a range such as **17 July to 22 July**.
- [ ] Upload and run the PSP-stage reports in the first workspace when required.
- [ ] Upload and run the backend-stage reports in the second workspace when required.
- [ ] Confirm that the overview shows a separate row for every date and route.
- [ ] Use **Detailed reconciliation date** to inspect one selected date in route tabs.
