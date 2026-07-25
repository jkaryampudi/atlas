"""F-007 end-to-end: a registered run reproduces its ADJUSTED numbers as-of its
run date, even after a later correction to a bar AND a split.

This exercises the real backtest loader (real_run.load_adjusted_obars) with a
pinned knowledge instant K, composing the two versioned substrates (bars +
corporate actions) with the read-side split adjuster. It is the guarantee the
finding's IMPACT asks for: past runs are reproducible; and with K unset the
loader reads the head (byte-identical to pre-versioning)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.real_run import load_adjusted_obars
from atlas.dcp.market_data.bar_versions import set_knowledge_date
from atlas.dcp.market_data.ingest import record_split
from atlas.dcp.market_data.models import Split
from tests.conftest import requires_pg

pytestmark = requires_pg

K1 = datetime(2026, 2, 1, tzinfo=UTC)     # first run instant
K2 = datetime(2026, 3, 1, tzinfo=UTC)     # after a correction landed


def _inst(s) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES ('ZZ','US','US','stock','ZZ','USD',true)"
        " RETURNING id"), {}).scalar())


def _bar(s, iid, d, close):
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:i,:d,:c,200,1,:c,100,'EodhdAdapter') "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET close=:c"),
        {"i": iid, "d": d, "c": close})


def _closes(s, known_by):
    obars, _ = load_adjusted_obars(s, "ZZ", known_by=known_by)
    return [round(o.close, 4) for o in obars]


def test_pinned_run_reproduces_after_bar_and_split_correction(pg_session):
    s = pg_session
    iid = _inst(s)
    # --- K1 ingest: two bars straddling a 2:1 split ---
    set_knowledge_date(s, FrozenClock(K1))
    _bar(s, iid, date(2026, 1, 5), 100)      # pre-split
    _bar(s, iid, date(2026, 1, 15), 50)      # post-split
    record_split(s, iid, Split("ZZ", date(2026, 1, 10), Decimal(2)), "EodhdAdapter")

    asof_k1 = _closes(s, K1)
    head = [round(o.close, 4) for o in load_adjusted_obars(s, "ZZ")[0]]
    assert asof_k1 == [50.0, 50.0]           # 100/2 (pre-split ÷2), 50 (post)
    assert head == asof_k1                    # before any correction, head == as-of-K1

    # --- K2 ingest: correct the pre-split bar (100->110) AND the split (2->4) ---
    set_knowledge_date(s, FrozenClock(K2))
    _bar(s, iid, date(2026, 1, 5), 110)      # bar correction -> new version
    record_split(s, iid, Split("ZZ", date(2026, 1, 10), Decimal(4)), "EodhdAdapter")  # split correction

    # REPRODUCIBILITY: the run pinned at K1 is byte-identical to before —
    # neither the bar nor the split correction leaked into it.
    assert _closes(s, K1) == asof_k1

    # a run pinned at K2 reflects BOTH corrections (110/4 = 27.5, 50 unchanged).
    assert _closes(s, K2) == [27.5, 50.0]
    assert _closes(s, K2) != asof_k1
