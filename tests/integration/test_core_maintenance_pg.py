"""Standing-core maintenance (ops-reliability build, 2026-07): the passive
core (ADR-0012) is a STANDING POLICY — maintain_core_proposals must keep it
one click away every morning, never duplicate a live proposal, and never
resurrect expired/rejected history.

Run against a dedicated throwaway DB, never dev 'atlas' or shared 'atlas_test':
    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_ops"

The matrix (task spec, verbatim):
  * no live proposal + drift  -> regenerates (risk-checked, audited);
  * live pending              -> no-op;
  * within band               -> no-op;
  * expired yesterday         -> regenerates fresh (history untouched).
Plus the 72h core TTL pin (agent proposals stay 24h — their signals age fast)
and the approved-in-flight no-op.

Seeding mirrors test_core_allocation_pg.py (INDA at 15% clears limit_set_v1;
SPY's L2 block is that suite's concern, not repeated here — the matrix runs
on INDA-only targets so every regeneration is a clean PASS).
Nothing commits: pg_session rolls back.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.core_allocation import (
    CORE_PROPOSAL_TTL,
    MAINTENANCE_EVENT,
    maintain_core_proposals,
)
from atlas.dcp.trading.proposals import PROPOSAL_TTL, expire_stale
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # limit_set v1 first effective day
INDA_PX = Decimal("48.73")
FX_USD_AUD = Decimal("1.4453")
INDA_ONLY = {"INDA": Decimal("0.15")}
_HIST = [date(2026, 6, 23) + timedelta(days=i) for i in range(21)]


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol = 'INDA')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol = 'INDA'"))


def _seed(s) -> str:
    """Empty A$100k book + INDA at its golden close + a USD->AUD rate."""
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency, economic_exposure) "
        "VALUES ('INDA', 'INDA', 'US', 'etf', 'INDA', 'Broad', 'USD', ARRAY['IN']) "
        "RETURNING id")).scalar())
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, 10000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": INDA_PX} for d in _HIST])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"d": date(2026, 7, 10), "r": FX_USD_AUD})
    return iid


def _proposals(s):
    return s.execute(text(
        "SELECT id, state, expires_at, created_at FROM trading.trade_proposals "
        "WHERE origin = 'core_allocation' ORDER BY created_at, id")).all()


def _maintenance_events(s) -> int:
    return s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type = :et"),
        {"et": MAINTENANCE_EVENT}).scalar()


# ------------------------------------------------------------- the TTL pins

def test_core_ttl_is_72h_and_agent_ttl_stays_24h():
    """The signed split: a standing policy waits for the weekend (72h); an
    agent thesis is priced off a fresh signal and dies at 24h, UNCHANGED."""
    assert CORE_PROPOSAL_TTL == timedelta(hours=72)
    assert PROPOSAL_TTL == timedelta(hours=24)


def test_regenerated_core_proposal_expires_exactly_72h_out(clean_audit):
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    report = maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    assert len(report.regenerated) == 1
    rows = _proposals(s)
    assert len(rows) == 1
    assert rows[0].expires_at == T0 + timedelta(hours=72)   # pinned, not derived
    # and the audit trail says so
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events WHERE event_type = :et"),
        {"et": MAINTENANCE_EVENT}).scalar_one()
    assert ev["ttl_hours"] == 72


# --------------------------------------------------------------- the matrix

def test_drift_with_no_live_proposal_regenerates(clean_audit):
    """Empty book, 15% target: 15pp of drift, nothing live -> one risk-checked
    pending_approval proposal through the existing build path + ONE audit
    event."""
    s = clean_audit
    _seed(s)
    report = maintain_core_proposals(s, FrozenClock(T0), targets=INDA_ONLY, retired=None)
    assert [r.symbol for r in report.regenerated] == ["INDA"]
    r = report.regenerated[0]
    assert (r.action, r.qty, r.verdict, r.state) == (
        "buy", 212, "PASS", "pending_approval")
    assert report.live == () and report.in_band == () and report.missing == ()
    assert report.summary() == (
        "core regenerated INDA:buy 212 -> pending_approval")
    assert len(_proposals(s)) == 1
    assert _maintenance_events(s) == 1


def test_live_pending_proposal_is_a_noop(clean_audit):
    """A second maintenance pass in the same state must not duplicate: the
    live pending proposal IS the deliverable."""
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    clock.advance_to(T0 + timedelta(hours=20))              # well inside 72h
    again = maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    assert again.regenerated == () and again.live == ("INDA",)
    assert again.summary() == "core live INDA"
    assert len(_proposals(s)) == 1                          # still exactly one
    assert _maintenance_events(s) == 1                      # no new event


def test_approved_in_flight_proposal_is_live_too(clean_audit):
    """approved (order in flight, position not landed yet) counts as live —
    regenerating on top of it would double-buy the leg on fill."""
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    s.execute(text("UPDATE trading.trade_proposals SET state = 'approved' "
                   "WHERE origin = 'core_allocation'"))
    clock.advance_to(T0 + timedelta(hours=100))             # past even the 72h TTL
    again = maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    assert again.regenerated == () and again.live == ("INDA",)
    assert len(_proposals(s)) == 1


def test_within_band_is_a_noop(clean_audit):
    """A book already holding the target weight (whole-share) produces no
    legs and no proposals — idempotent inside the band (ADR-0012)."""
    s = clean_audit
    iid = _seed(s)
    # a consistent filled long at the target: 212 sh x 48.73 x 1.4453 ≈ 14.93%
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind) "
        "VALUES ('[]', 'PASS', 'proposal') RETURNING id")).scalar()
    ap = s.execute(text(
        "INSERT INTO trading.approvals (decision, approver, "
        " approval_time_risk_check_id) "
        "VALUES ('approve', 'principal', :c) RETURNING id"), {"c": rc}).scalar()
    o = s.execute(text(
        "INSERT INTO trading.orders (approval_id, risk_check_id, side, qty, state) "
        "VALUES (:a, :c, 'buy', 212, 'filled') RETURNING id"),
        {"a": ap, "c": rc}).scalar()
    s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, fees, "
        " fx_rate_used) VALUES (:o, 212, :p, 0, :fx)"),
        {"o": o, "p": INDA_PX, "fx": FX_USD_AUD})
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, is_core) VALUES (:i, 212, :p, 'USD', :t, true)"),
        {"i": iid, "p": INDA_PX, "t": datetime(2026, 7, 9, 15, 0, tzinfo=UTC)})

    report = maintain_core_proposals(s, FrozenClock(T0), targets=INDA_ONLY, retired=None)
    assert report.regenerated == () and report.live == ()
    assert report.in_band == ("INDA",)
    assert report.summary() == "core in band INDA"
    assert len(_proposals(s)) == 0
    assert _maintenance_events(s) == 0


def test_expired_yesterday_regenerates_fresh_and_leaves_history(clean_audit):
    """THE OBSERVED FAILURE, fixed: a core proposal that expired unapproved is
    HISTORY — the next pass regenerates a fresh 72h proposal; the expired row
    keeps its state (nothing resurrects, nothing UPDATEs the corpse)."""
    s = clean_audit
    _seed(s)
    clock = FrozenClock(T0)
    maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    clock.advance_to(T0 + timedelta(hours=73))              # past the 72h TTL
    expired = expire_stale(s, clock)                        # t2's own funeral
    assert len(expired) == 1
    report = maintain_core_proposals(s, clock, targets=INDA_ONLY, retired=None)
    assert [r.symbol for r in report.regenerated] == ["INDA"]
    assert report.regenerated[0].state == "pending_approval"
    rows = _proposals(s)
    assert [r.state for r in rows] == ["expired", "pending_approval"]
    assert rows[1].expires_at == clock.now() + timedelta(hours=72)
    assert _maintenance_events(s) == 2                      # one per regeneration


def test_missing_universe_is_reported_never_raised(clean_audit):
    """A DB without the core instruments idles honestly (the daily-cycle
    fixture path) — reported per symbol, no exception, no proposals.
    Fictional target symbols keep this debris-proof: other suites commit
    real SPY/INDA rows into the shared test database."""
    s = clean_audit
    _clean(s)
    report = maintain_core_proposals(
        s, FrozenClock(T0), retired=None,
        targets={"ZCMX": Decimal("0.15"), "ZCMY": Decimal("0.55")})
    assert report.missing == ("ZCMX", "ZCMY")
    assert report.summary() == "core not in universe ZCMX, ZCMY"
    assert len(_proposals(s)) == 0


def test_retired_core_reports_the_decision_and_writes_nothing(clean_audit):
    """ADR-0017: at DEFAULTS the core is retired — the nightly pass names the
    signed decision, proposes nothing, touches nothing (no lifecycle lock, no
    proposals, no audit events)."""
    s = clean_audit
    before = s.execute(text("SELECT count(*) FROM trading.trade_proposals")).scalar()
    report = maintain_core_proposals(s, FrozenClock(T0))
    assert report.retired == "ADR-0017"
    assert report.summary() == ("core retired (ADR-0017) — no ETF proposals "
                                "by signed policy")
    assert report.missing == () and report.regenerated == ()
    after = s.execute(text("SELECT count(*) FROM trading.trade_proposals")).scalar()
    assert after == before
