"""Quant page — trial registry and validation verdicts (pure API client).
Failed gates are shown as prominently as passes: honest failures are deliverables."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root — venv-independent

import streamlit as st

from atlas.dashboard._client import get_json

st.set_page_config(page_title="Atlas — Quant", layout="wide")
st.title("Quant — Trials & Verdicts")

st.subheader("Trial registry")
st.caption("EVERY backtest is registered (ADR-0002); deflated Sharpe uses this count.")
trials, terr = get_json("/v1/quant/trials?limit=50")
if trials:
    st.metric("Registered trials (shown)", len(trials))
    st.table([{"created": t["created_at"][:16], "family": t["strategy_family"],
               **{k: round(v, 4) if isinstance(v, float) else v
                  for k, v in (t.get("metrics") or {}).items()}} for t in trials])
elif terr:
    st.warning(terr)
else:
    st.info("No trials registered yet.")

st.subheader("Validation verdicts")
verdicts, verr = get_json("/v1/quant/verdicts?limit=50")
if verdicts:
    for v in verdicts:
        icon = "✅" if v["verdict"] == "approve" else "❌"
        st.markdown(f'{icon} `{v["created_at"][:16]}` **{v["verdict"].upper()}**'
                    + (f' — {v["reasons"]}' if v.get("reasons") else ""))
elif verr:
    st.warning(verr)
else:
    st.info("No validation verdicts yet. The first real run's report lives at "
            "docs/reports/decision-grade-momentum-v1.md (gates: FAIL on 16y, decision-grade, as recorded).")
