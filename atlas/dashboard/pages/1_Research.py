"""Research page — committee memos with their full evidence trail (pure API client)."""
import streamlit as st

from atlas.dashboard._client import get_json

st.set_page_config(page_title="Atlas — Research", layout="wide")
st.title("Research — Committee Memos")

symbol = st.text_input("Filter by symbol (blank = all)", "")
path = "/v1/research/memos?limit=25" + (f"&symbol={symbol.strip().upper()}" if symbol.strip() else "")
memos, err = get_json(path)

if err:
    st.warning(err)
elif not memos:
    st.info("No memos yet. Live committee runs start once the Anthropic API key is set "
            "(ATLAS_ANTHROPIC_API_KEY); the red-team suite exercises the cage until then.")
else:
    for m in memos:
        shadow = " · SHADOW (non-actionable)" if m.get("shadow") else ""
        with st.expander(
                f'{m["created_at"][:16]} · {m["instrument_symbol"] or "—"} · '
                f'{m["recommendation"]} ({m["conviction"]}){shadow}'):
            st.markdown(f'**Thesis** — {m["thesis"]}')
            st.markdown(f'**Dissent** — {m["dissent"]}')
            if m.get("debate_summary"):
                st.markdown(f'**Debate** — {m["debate_summary"]}')
            st.markdown("**Kill criteria**")
            for k in m.get("kill_criteria") or []:
                st.markdown(f"- {k}")
            refs = m.get("evidence_refs") or []
            st.caption(f'evidence: {", ".join(refs) if refs else "none"} · '
                       f'model: {m.get("model") or "—"} · run: {m.get("run_status") or "—"}')
