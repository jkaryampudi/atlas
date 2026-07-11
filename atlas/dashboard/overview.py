"""Atlas Overview — pure API client (Doc 06 §6). Never crashes on a failed endpoint;
every panel degrades independently."""
import os

import httpx
import streamlit as st

API = os.environ.get("ATLAS_API_URL", "http://localhost:8000")

st.set_page_config(page_title="Atlas AI Capital", layout="wide")
st.title("Atlas AI Capital — Overview")


def get_json(path: str):
    try:
        r = httpx.get(f"{API}{path}", timeout=5)
        if r.status_code == 200:
            return r.json(), None
        return None, f"{r.status_code} from {path}"
    except httpx.HTTPError as e:
        return None, f"{path}: {type(e).__name__}"


health, err = get_json("/v1/system/health")
if not health:
    st.error(f"API unreachable at {API} — is uvicorn running? ({err})")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Mode", health["trading_mode"].upper())
c2.metric("Armed", "YES" if health["armed"] else "NO")
c3.metric("Limit mode", health["limit_mode"])
chain, _ = get_json("/v1/audit/events/verify")
c4.metric("Audit chain", chain["chain"].upper() if chain else "UNREACHABLE",
          f'{chain["events_verified"]} events' if chain else None)

st.subheader("Data quality gates")
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
    st.info("No portfolio snapshot yet."
            + (f" ({serr})" if serr and "404" not in (serr or "") else ""))

st.subheader("Recent audit events")
events, eerr = get_json("/v1/audit/events?limit=15")
if events:
    st.table(events)
elif eerr:
    st.warning(eerr)
