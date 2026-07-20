"""P0.1 (ADR-0018) req 2 / test 14: the morning-brief attribution block — the
one persisted composite the dashboard renders — defaults to the AUTHORITATIVE
satellite alpha (shadow excluded) and names shadow sleeves separately. A shadow
sleeve never mixes into the dashboard total by default."""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text

from atlas.dcp.reporting.brief import _attribution_block
from tests.conftest import requires_pg

pytestmark = requires_pg

D1, D2 = date(2026, 7, 13), date(2026, 7, 14)
AS1 = datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
AS2 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)


def _seed(s, *, xsmom_d2="1100") -> None:
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    s.execute(text("DELETE FROM trading.portfolio_snapshots"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','n','1.0.0','{}','x','{}',"
        " 'research_shadow'),('pead-sue-tr','n','1.0.0','{}','p','{}','paper')"))
    for as_of, nav in ((AS1, "3000"), (AS2, "3300")):
        s.execute(text(
            "INSERT INTO trading.portfolio_snapshots (as_of, nav_aud, cash_aud, "
            " holdings, exposures, fx_rates) VALUES (:a,:n,0,'[]','{}','{}')"),
            {"a": as_of, "n": nav})
    for d, sleeve, val, ret, bench in [
            (D1, "xsmom", "1000", None, None), (D2, "xsmom", xsmom_d2, "0.05", "0.01"),
            (D1, "pead", "2000", None, None), (D2, "pead", "2200", "0.10", "0.01")]:
        s.execute(text(
            "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
            " value_aud, ret_1d, benchmark_ret_1d, created_at) "
            "VALUES (:d,:sl,:v,:r,:b,:ca)"),
            {"d": d, "sl": sleeve, "v": val, "r": ret, "b": bench, "ca": AS1})


def test_brief_total_does_not_mix_shadow_by_default(clean_audit):
    s = clean_audit
    _seed(s, xsmom_d2="1100")
    b = _attribution_block(s)
    assert b is not None
    assert b["performance_scope"] == "authoritative_portfolio"
    assert b["authoritative"] is True
    # the brief's satellite alpha is the AUTHORITATIVE (pead-only) 9.00 pp
    assert b["satellite_alpha_pp"] == 9.0
    # the shadow sleeve is named separately, never fused into the number
    assert b["shadow_sleeves"] == ["xsmom"]
    # a huge shadow move leaves the dashboard total byte-identical
    _seed(s, xsmom_d2="999999999")
    assert _attribution_block(s)["satellite_alpha_pp"] == 9.0
