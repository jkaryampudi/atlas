"""P0.1 (ADR-0018) — the SEPARATED calculation: the authoritative composite is
computed from ONLY authoritative sleeves and is byte-identical no matter what a
research_shadow sleeve does. Seeds BOTH satellite families at DIFFERENT lifecycle
states with NONZERO stored values (which no prior test did) and proves the three
scoped views are calculated independently. Stored per-sleeve rows are never
mutated by scoping — the value identity is untouched."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from atlas.dcp import strategy_lifecycle as sl
from atlas.dcp.reporting.attribution import (
    cumulative_alpha_pp,
    included_satellite_sleeves,
    scoped_performance,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

D1, D2 = date(2026, 7, 13), date(2026, 7, 14)
AS1 = datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
AS2 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)


def _seed(s, *, xsmom_state="research_shadow", pead_state="paper",
          xsmom_d2="1100") -> tuple[str, str]:
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    s.execute(text("DELETE FROM trading.portfolio_snapshots"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))

    def _strat(family, state, sha):
        return str(s.execute(text(
            "INSERT INTO quant.strategies (family, name, version, spec, code_sha,"
            " tolerance_bands, state) VALUES (:f,'n','1.0.0','{}',:sha,'{}',:st) "
            "RETURNING id"), {"f": family, "st": state, "sha": sha}).scalar())
    xid = _strat("xsmom-pit-tr", xsmom_state, "xsha")
    pid = _strat("pead-sue-tr", pead_state, "psha")

    for as_of, nav in ((AS1, "3000"), (AS2, "3300")):
        s.execute(text(
            "INSERT INTO trading.portfolio_snapshots (as_of, nav_aud, cash_aud, "
            " holdings, exposures, fx_rates) VALUES (:a,:n,0,'[]','{}','{}')"),
            {"a": as_of, "n": nav})

    # xsmom (shadow) and pead (paper), both NONZERO; d2 carries the SPY-TR leg
    rows = [
        (D1, "xsmom", "1000", None, None), (D2, "xsmom", xsmom_d2, "0.05", "0.01"),
        (D1, "pead", "2000", None, None), (D2, "pead", "2200", "0.10", "0.01"),
    ]
    for d, sleeve, val, ret, bench in rows:
        s.execute(text(
            "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
            " value_aud, ret_1d, benchmark_ret_1d, created_at) "
            "VALUES (:d,:sl,:v,:r,:b,:ca)"),
            {"d": d, "sl": sleeve, "v": val, "r": ret, "b": bench, "ca": AS1})
    return xid, pid


def test_authoritative_composite_excludes_shadow_and_is_scope_selected(clean_audit):
    s = clean_audit
    _seed(s)
    # authoritative = pead only (paper); research_shadow = xsmom only; all = both
    assert included_satellite_sleeves(s, sl.AUTHORITATIVE_PORTFOLIO) == frozenset({"pead"})
    assert included_satellite_sleeves(s, sl.RESEARCH_SHADOW_SCOPE) == frozenset({"xsmom"})
    assert included_satellite_sleeves(s, sl.ALL_SIMULATED) == frozenset({"xsmom", "pead"})


def test_shadow_gain_or_loss_leaves_authoritative_byte_identical(clean_audit):
    """Tests 1, 2, 15: a positive OR a huge negative research-shadow return does
    not change the authoritative composite by a single digit."""
    s = clean_audit
    _seed(s, xsmom_d2="1100")                       # small shadow move
    base = scoped_performance(s, sl.AUTHORITATIVE_PORTFOLIO)["satellite_alpha_pp"]
    for shadow in ("999999999", "1", "500"):        # enormous gain, huge loss, mid
        _seed(s, xsmom_d2=shadow)
        got = scoped_performance(s, sl.AUTHORITATIVE_PORTFOLIO)["satellite_alpha_pp"]
        assert got == base, f"shadow d2={shadow} moved the authoritative composite"
    # and the authoritative number is the pead-only composite (10% vs 1% = 9.00pp)
    assert base == Decimal("9.00")


def test_three_scopes_are_calculated_independently(clean_audit):
    """Test 3: authoritative (pead), research_shadow (xsmom) and all_simulated are
    three distinct numbers from the same stored rows."""
    s = clean_audit
    _seed(s, xsmom_d2="1300")                        # xsmom +30% vs pead +10%
    a = scoped_performance(s, sl.AUTHORITATIVE_PORTFOLIO)["satellite_alpha_pp"]
    r = scoped_performance(s, sl.RESEARCH_SHADOW_SCOPE)["satellite_alpha_pp"]
    c = scoped_performance(s, sl.ALL_SIMULATED)["satellite_alpha_pp"]
    assert a == cumulative_alpha_pp(s, included=frozenset({"pead"}))
    assert r == cumulative_alpha_pp(s, included=frozenset({"xsmom"}))
    assert c == cumulative_alpha_pp(s, included=frozenset({"xsmom", "pead"}))
    assert len({a, r, c}) == 3                       # genuinely independent


def test_default_scope_is_authoritative_and_all_simulated_is_explicit(clean_audit):
    """Tests 4, 5, 6: default == authoritative; all_simulated only on explicit
    request and always non-authoritative + contains_shadow_results."""
    s = clean_audit
    _seed(s)
    default = scoped_performance(s)                  # no scope arg
    assert default["performance_scope"] == sl.AUTHORITATIVE_PORTFOLIO
    assert default["authoritative"] is True
    assert default["contains_shadow_results"] is False
    assert default["satellite_alpha_pp"] == scoped_performance(
        s, sl.AUTHORITATIVE_PORTFOLIO)["satellite_alpha_pp"]
    comb = scoped_performance(s, sl.ALL_SIMULATED)
    assert comb["authoritative"] is False
    assert comb["contains_shadow_results"] is True
    assert comb["caveat"] == "COMBINED SIMULATION — NON-AUTHORITATIVE"


def test_scope_metadata_fields_present_and_correct(clean_audit):
    """Test 7 + the required field set: research_shadow view is labelled and
    lists the right included/excluded strategy ids + artifact digest."""
    s = clean_audit
    xid, pid = _seed(s)
    m = scoped_performance(s, sl.RESEARCH_SHADOW_SCOPE)
    assert set(m) >= {"performance_scope", "authoritative", "validation_status",
                      "included_strategy_ids", "excluded_strategy_ids",
                      "contains_shadow_results", "artifact_digest",
                      "artifact_status", "strategy_code_sha", "caveat",
                      "satellite_alpha_pp"}
    assert m["performance_scope"] == "research_shadow"
    assert m["authoritative"] is False
    assert m["validation_status"] == "research_shadow"
    assert m["caveat"] == "RESEARCH SHADOW — NOT VALIDATED"
    assert m["included_strategy_ids"] == [xid]       # the shadow strategy
    assert m["excluded_strategy_ids"] == [pid]       # the paper one
    # interim identity contract: NO complete digest yet; the raw code SHA is
    # surfaced honestly, never mislabelled as an artifact_digest
    assert m["artifact_digest"] is None
    assert m["artifact_status"] == "LEGACY_UNBOUND"
    assert m["strategy_code_sha"] == {"xsmom": "xsha"}


def test_unknown_state_is_excluded_from_the_authoritative_composite(clean_audit):
    """Test 8: an unmapped/non-authoritative state fails closed — never
    authoritative, and its value cannot move the authoritative composite. (A
    state the DB CHECK forbids entirely is covered by the unit classify test;
    here 'draft' is a valid non-paper/live, non-shadow state — classify maps it
    to non_authoritative, so it is excluded from every named composite.)"""
    s = clean_audit
    xid, _ = _seed(s, xsmom_state="draft", xsmom_d2="999999")
    assert included_satellite_sleeves(s, sl.AUTHORITATIVE_PORTFOLIO) == frozenset({"pead"})
    # unknown xsmom is excluded from ALL named composites (not authoritative, not shadow)
    assert "xsmom" not in included_satellite_sleeves(s, sl.ALL_SIMULATED)
    auth = scoped_performance(s, sl.AUTHORITATIVE_PORTFOLIO)
    assert auth["satellite_alpha_pp"] == Decimal("9.00")   # still pead-only
    assert xid not in auth["included_strategy_ids"]         # the unknown one is out


def test_scoping_does_not_mutate_stored_rows(clean_audit):
    """Test 16: the raw stored per-sleeve rows are byte-identical after every
    scoped read — scoping SELECTS, never re-derives."""
    s = clean_audit
    _seed(s)

    def _rows():
        return [tuple(r) for r in s.execute(text(
            "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d "
            "FROM reporting.attribution_daily ORDER BY session_date, sleeve")).all()]
    before = _rows()
    for scope in (sl.AUTHORITATIVE_PORTFOLIO, sl.RESEARCH_SHADOW_SCOPE,
                  sl.ALL_SIMULATED):
        scoped_performance(s, scope)
    assert _rows() == before
