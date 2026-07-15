"""Passive index-core allocation against an isolated Postgres (ADR-0012).

Run against a dedicated throwaway DB, never dev 'atlas' or shared 'atlas_test':
    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_core"

Covers, in priority order:
  * INVARIANT PRESERVATION (the point of migration 0022): an origin='agent' row
    missing a memo / a signal / a stop is still rejected by the DB; only an
    origin='core_allocation' row may omit all three. The carve-out is exactly
    scoped — invariant 2 ("no BUY without DCP evidence") still binds every agent.
  * build_core_proposals routes legs through the REAL risk engine: INDA (15%)
    clears under limit_set_v1; SPY (55%) is blocked by L2 today (the documented
    limit-set-v2 dependency); the full core clears once v2 raises L2.
  * a core proposal carries NO stop and that is allowed (core is rebalanced, not
    stopped); idempotency within the drift band; migration up/down restoration.

Nothing commits: pg_session rolls back. The migration up/down test restores head.
"""
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.core_allocation import build_core_proposals
from tests.conftest import URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # first day limit_set v1 is effective
SPY_PX = Decimal("751.83")
INDA_PX = Decimal("48.73")
FX_USD_AUD = Decimal("1.4453")
_HIST = [date(2026, 6, 23) + timedelta(days=i) for i in range(21)]   # >=20 sessions


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol IN ('SPY','INDA'))"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol IN ('SPY','INDA')"))


def _etf(s, symbol: str, exposure: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency, economic_exposure) "
        "VALUES (:sym, :sym, 'US', 'etf', :sym, 'Broad', 'USD', ARRAY[:exp]) "
        "RETURNING id"), {"sym": symbol, "exp": exposure}).scalar())


def _bars(s, iid: str, close: Decimal) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, 10000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": close} for d in _HIST])


def _fx(s) -> None:
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"d": date(2026, 7, 10), "r": FX_USD_AUD})


def _seed(s) -> dict[str, str]:
    """Empty A$100k book + SPY/INDA at their golden closes + a USD->AUD rate."""
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    ids = {"SPY": _etf(s, "SPY", "US"), "INDA": _etf(s, "INDA", "IN")}
    _bars(s, ids["SPY"], SPY_PX)
    _bars(s, ids["INDA"], INDA_PX)
    _fx(s)
    return ids


def _seed_v2_l2(s, cap: str) -> None:
    """Simulate the board-memo item-8 signed limit-set v2 that raises the L2
    single-ETF cap for the index-core ETF class (dual-confirm change control)."""
    limits = json.loads((ROOT / "seeds" / "limit_set_v1.json").read_text())["limits"]
    limits["L2_max_etf_weight"] = float(cap)
    s.execute(text(
        "INSERT INTO risk.limit_sets (version, mode, limits, effective_from, "
        " created_by, confirmation_a, confirmation_b) "
        "VALUES (2, 'small_aum', CAST(:l AS jsonb), :ef, 'principal:test', "
        "        :t - interval '2 hours', :t)"),
        {"l": json.dumps(limits), "ef": date(2026, 7, 13), "t": T0})


def _deploy(s, iid: str, qty: int, price: Decimal) -> None:
    """A consistent filled long: the buy execution deducts the cash ledger so
    NAV = cash + holdings stays A$100k (a position without its execution would
    inflate NAV out of nowhere). Minimal risk-check/approval/order scaffolding
    satisfies the Doc 05 §7 NOT NULL lineage."""
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind) "
        "VALUES ('[]', 'PASS', 'proposal') RETURNING id")).scalar()
    ap = s.execute(text(
        "INSERT INTO trading.approvals (decision, approver, approval_time_risk_check_id) "
        "VALUES ('approve', 'principal', :c) RETURNING id"), {"c": rc}).scalar()
    o = s.execute(text(
        "INSERT INTO trading.orders (approval_id, risk_check_id, side, qty, state) "
        "VALUES (:a, :c, 'buy', :q, 'filled') RETURNING id"),
        {"a": ap, "c": rc, "q": qty}).scalar()
    s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, fees, fx_rate_used) "
        "VALUES (:o, :q, :p, 0, :fx)"), {"o": o, "q": qty, "p": price, "fx": FX_USD_AUD})
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, opened_at) "
        "VALUES (:i, :q, :p, 'USD', :t)"),
        {"i": iid, "q": qty, "p": price, "t": datetime(2026, 7, 9, 15, 0, tzinfo=UTC)})


def _row(s, proposal_id: str):
    return s.execute(text(
        "SELECT origin, action, committee_memo_id, signal_ids, stop_loss, "
        "       entry_price, target_price, thesis_summary, state, risk_check_id "
        "FROM trading.trade_proposals WHERE id = :p"), {"p": proposal_id}).one()


# --------------------------------------------- INVARIANT PRESERVATION (0022)

def test_invariant_carveout_is_exactly_scoped_to_core_allocation(clean_audit):
    """The MOST IMPORTANT test: migration 0022 must relax the evidence
    requirement ONLY for origin='core_allocation'. Every origin='agent' row still
    needs a memo, a signal AND a stop, or the DB rejects it (invariant 2)."""
    s = clean_audit
    _seed(s)
    memo = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee','BUY','[]') RETURNING id")).scalar())
    exp = T0 + timedelta(hours=24)
    sig = [uuid4()]

    def rejects(cols: str, vals: str, params: dict) -> None:
        with pytest.raises(IntegrityError):
            with s.begin_nested():
                s.execute(text(
                    f"INSERT INTO trading.trade_proposals ({cols}, entry_price, "
                    f"target_price, state, expires_at) VALUES ({vals}, 1, 1, 'draft', :e)"),
                    {**params, "e": exp})

    # origin defaults to 'agent' -> the full invariant binds:
    # (1) NULL committee memo  -> rejected
    rejects("signal_ids, stop_loss", ":sig, 1", {"sig": sig})
    # (2) empty signal_ids     -> rejected (the cardinality half of invariant 2)
    rejects("committee_memo_id, signal_ids, stop_loss", ":m, '{}', 1", {"m": memo})
    # (3) NULL stop            -> rejected
    rejects("committee_memo_id, signal_ids", ":m, :sig", {"m": memo, "sig": sig})
    # an unknown origin cannot smuggle past the discriminator
    rejects("origin, signal_ids", "'sneaky', '{}'", {})

    # a fully-evidenced agent row IS accepted (the invariant is not over-broad)
    ok_agent = s.execute(text(
        "INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, "
        "stop_loss, entry_price, target_price, state, expires_at) "
        "VALUES (:m, :sig, 1, 1, 1, 'draft', :e) RETURNING id"),
        {"m": memo, "sig": sig, "e": exp}).scalar()
    assert ok_agent is not None

    # THE CARVE-OUT: an origin='core_allocation' row with NO memo, EMPTY signals
    # and NO stop is ACCEPTED — authorised by ADR-0012.
    ok_core = s.execute(text(
        "INSERT INTO trading.trade_proposals (origin, signal_ids, entry_price, "
        "target_price, state, expires_at) "
        "VALUES ('core_allocation', '{}', 1, 1, 'draft', :e) RETURNING id"),
        {"e": exp}).scalar()
    core = _row(s, str(ok_core))
    assert core.origin == "core_allocation"
    assert core.committee_memo_id is None
    assert list(core.signal_ids) == []
    assert core.stop_loss is None


# --------------------------------------- build_core_proposals + real risk engine

def test_full_core_run_inda_clears_and_spy_blocked_on_l2(clean_audit):
    """One deterministic run against the empty A$100k book. INDA (15%) PASSES the
    real L1-L11; SPY (55%) is TERMINAL-rejected by L2 (single-ETF 15% cap under
    limit_set_v1) — the honest current-state blocker, invariant 3 intact."""
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)

    results = {r.symbol: r for r in build_core_proposals(s, clock)}
    assert set(results) == {"SPY", "INDA"}

    inda = results["INDA"]
    assert (inda.action, inda.qty) == ("buy", 212)
    assert inda.verdict == "PASS"
    assert inda.state == "pending_approval"
    assert inda.risk_check_id is not None
    ir = _row(s, inda.proposal_id)
    # persisted exactly as a core leg: no memo, empty signals, NO stop, ref close
    assert ir.origin == "core_allocation"
    assert ir.committee_memo_id is None
    assert list(ir.signal_ids) == []
    assert ir.stop_loss is None
    assert ir.entry_price == Decimal("48.730000")   # ref close, both cols
    assert ir.target_price == Decimal("48.730000")
    assert ir.thesis_summary.startswith("Passive index core per ADR-0012")
    # the referenced risk check is a genuine PASS
    verdict = s.execute(text("SELECT verdict FROM risk.risk_checks WHERE id = :c"),
                        {"c": inda.risk_check_id}).scalar()
    assert verdict == "PASS"

    spy = results["SPY"]
    assert (spy.action, spy.qty) == ("buy", 50)
    assert spy.verdict == "FAIL"
    assert spy.state == "rejected"
    assert "L2" in spy.failures            # single-ETF concentration, 55% > 15%
    assert spy.risk_check_id is None       # only a PASS is referenced (Doc 04 §2.1)
    sr = _row(s, spy.proposal_id)
    assert sr.origin == "core_allocation" and sr.stop_loss is None
    # invariant 3: the FAIL is terminal and recorded
    fail = s.execute(text(
        "SELECT verdict FROM risk.risk_checks WHERE proposal_id = :p"),
        {"p": spy.proposal_id}).scalar()
    assert fail == "FAIL"


def test_core_proposal_needs_no_stop_and_still_passes_risk(clean_audit):
    """A core leg with stop_loss NULL is accepted by the risk engine (the core is
    rebalanced, not stopped): zero stop-out risk, weight rules still enforced."""
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    # INDA-only target so the passing leg is isolated and unambiguous.
    results = build_core_proposals(s, clock, targets={"INDA": Decimal("0.15")})
    assert len(results) == 1
    r = results[0]
    assert r.verdict == "PASS" and r.state == "pending_approval"
    row = _row(s, r.proposal_id)
    assert row.stop_loss is None            # no stop, and that is ALLOWED
    # the risk check itemises L6/L7 (stop-based) as passing at zero risk
    results_json = s.execute(text(
        "SELECT results FROM risk.risk_checks WHERE id = :c"),
        {"c": r.risk_check_id}).scalar()
    by_rule = {x["rule"]: x for x in results_json}
    assert by_rule["L6"]["pass"] and by_rule["L7"]["pass"]


def test_full_core_clears_under_signed_v2_limit_set(clean_audit):
    """Proof the mechanism is correct and the ONLY blocker is the limit value:
    with a signed v2 raising L2 to 60%, BOTH SPY (55%) and INDA (15%) PASS."""
    s = clean_audit
    _seed(s)
    _seed_v2_l2(s, "0.60")
    clock = FrozenClock(T0)
    results = {r.symbol: r for r in build_core_proposals(s, clock)}
    assert results["SPY"].verdict == "PASS"
    assert results["SPY"].state == "pending_approval"
    assert results["INDA"].verdict == "PASS"
    assert all(_row(s, r.proposal_id).stop_loss is None for r in results.values())


def test_idempotent_within_drift_band_makes_no_proposals(clean_audit):
    """A book already at the golden resulting holdings (SPY 50 = 54.33%,
    INDA 212 = 14.93%) sits within +/-5pp of target -> zero proposals. This is
    exactly the state a prior rebalance produced, so re-running is a no-op."""
    s = clean_audit
    ids = _seed(s)
    _deploy(s, ids["SPY"], 50, SPY_PX)      # A$100k book stays balanced: the
    _deploy(s, ids["INDA"], 212, INDA_PX)   # executions deduct cash to A$30,737.96
    clock = FrozenClock(T0)
    assert build_core_proposals(s, clock) == []
    assert s.execute(text(
        "SELECT count(*) FROM trading.trade_proposals "
        "WHERE origin = 'core_allocation'")).scalar() == 0


# ------------------------------------------- migration 0022 up/down restoration

def _constraint_state():
    """(check-constraint names, memo/stop nullability) on a fresh connection."""
    eng = create_engine(URL)
    try:
        with eng.connect() as c:
            names = {r[0] for r in c.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'trading.trade_proposals'::regclass AND contype='c'"))}
            nullable = dict(c.execute(text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema='trading' AND table_name='trade_proposals' "
                "AND column_name IN ('committee_memo_id','stop_loss','origin')")).all())
        return names, nullable
    finally:
        eng.dispose()


def test_migration_0022_down_up_restores_constraints_exactly():
    _ensure_test_db()

    def alembic(*args: str) -> None:
        r = subprocess.run(["alembic", *args], cwd=ROOT,
                           env={**os.environ, "ATLAS_DATABASE_URL": URL},
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    try:
        # DOWN to 0021: the pre-0012 schema comes back EXACTLY.
        alembic("downgrade", "0021")
        names, nullable = _constraint_state()
        assert "trade_proposals_signal_ids_check" in names   # blanket cardinality
        assert "trade_proposals_origin_check" not in names
        assert not any(n.startswith("trade_proposals_agent_requires") for n in names)
        assert nullable == {"committee_memo_id": "NO", "stop_loss": "NO"}  # NOT NULL back

        # UP to head: the origin-scoped carve-out is re-applied.
        alembic("upgrade", "head")
        names, nullable = _constraint_state()
        assert {"trade_proposals_origin_check",
                "trade_proposals_agent_requires_memo",
                "trade_proposals_agent_requires_signal",
                "trade_proposals_agent_requires_stop"} <= names
        assert "trade_proposals_signal_ids_check" not in names  # blanket dropped
        assert nullable == {"committee_memo_id": "YES", "stop_loss": "YES",
                            "origin": "NO"}
    finally:
        alembic("upgrade", "head")   # never leave the shared test DB downgraded
