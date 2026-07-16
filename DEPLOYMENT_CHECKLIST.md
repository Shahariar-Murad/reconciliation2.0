# Deployment checklist — v2.5

- [ ] `app.py` is in the repository root
- [ ] `reconciliation_engine_v25.py` is in the same root directory
- [ ] `requirements.txt` is in the repository root
- [ ] `.streamlit/config.toml` is present
- [ ] Streamlit main file path is `app.py`
- [ ] Old engine files (`reconciliation_engine.py`, `reconciliation_engine_v24.py`) are deleted or ignored
- [ ] Changes are committed to the branch used by Streamlit Cloud
- [ ] The Streamlit app is rebooted after the commit
- [ ] Both top-level workspaces are visible:
  - [ ] PSP → Orchestrator
  - [ ] Orchestrator → Backend API
