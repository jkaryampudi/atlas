"""Market page — bars and FX from the real vendor feed (pure API client)."""
import streamlit as st

from atlas.dashboard._client import get_json

st.set_page_config(page_title="Atlas — Market", layout="wide")
st.title("Market — Bars & FX")

instruments, ierr = get_json("/v1/market/instruments")
symbols = [i["symbol"] for i in instruments] if instruments else []
if ierr:
    st.warning(ierr)

col1, col2 = st.columns([1, 3])
symbol = col1.selectbox("Instrument", symbols or ["AVGO"])
days = col1.slider("Days", 30, 260, 90)

bars, berr = get_json(f"/v1/market/bars/{symbol}?days={days}")
if bars:
    closes = {b["bar_date"]: float(b["close"]) for b in bars}
    col2.line_chart(closes)
    st.caption(f"{len(bars)} bars · split-adjusted on read · source: EODHD")
elif berr:
    st.warning(berr)
else:
    st.info("No bars for this symbol yet — run the backfill.")

st.subheader("FX (USD→AUD)")
fx, xerr = get_json("/v1/market/fx?days=60")
if fx:
    st.line_chart({r["rate_date"]: float(r["rate"]) for r in fx})
elif xerr:
    st.warning(xerr)
