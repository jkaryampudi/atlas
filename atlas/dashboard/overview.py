"""Atlas Overview — pure API client (Doc 06 §6). Never crashes on a failed endpoint;
every panel degrades independently. Run: make dashboard"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root — venv-independent

import streamlit as st

from atlas.dashboard._client import API, get_json

st.set_page_config(page_title="Atlas AI Capital", layout="wide")
st.title("Atlas AI Capital — Overview")

health, err = get_json("/v1/system/health")
if not health:
    st.error(f"API unreachable at {API} — is uvicorn running? ({err})")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Mode", health["trading_mode"].upper())
c2.metric("Armed", "YES" if health["armed"] else "NO")
c3.metric("Limit mode", health["limit_mode"])
chain, _ = get_json("/v1/audit/events/verify")
c4.metric("Audit chain", chain["chain"].upper() if chain else "UNREACHABLE",
          f'{chain["events_verified"]} events' if chain else None)
cost, _ = get_json("/v1/research/cost")
c5.metric("LLM spend today",
          f'${cost["spent_usd"]:.2f} / ${cost["daily_cap_usd"]:.0f}' if cost else "n/a")

st.subheader("Data freshness")
fresh, ferr = get_json("/v1/market/freshness")
if fresh:
    st.table(fresh)
elif ferr:
    st.warning(ferr)
else:
    st.info("No real bars yet — run the backfill.")

st.subheader("Data quality gates (latest per market)")
gates, gerr = get_json("/v1/market/quality-gates")
if gates:
    st.table(gates)
elif gerr:
    st.warning(gerr)
else:
    st.info("No gates recorded yet — run `make replay DATE=2024-07-15`")

st.subheader("Portfolio")
snap, serr = get_json("/v1/portfolio/snapshot")
if snap:
    st.json(snap)
else:
    st.info("No portfolio snapshot yet — positions arrive with Phase 5 paper trading."
            + (f" ({serr})" if serr and "404" not in (serr or "") else ""))

st.subheader("Recent audit events")
events, eerr = get_json("/v1/audit/events?limit=15")
if events:
    st.table(events)
elif eerr:
    st.warning(eerr)
