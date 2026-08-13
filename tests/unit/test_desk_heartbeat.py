"""Desk heartbeat (the 2026-08-12 lesson): the nightly desk runs for hours
inside ONE uncommitted transaction, so nothing it does is visible anywhere
until commit — a healthy 35-minute run was killed as "hung" because every
surface read 0. run_desk now beats an optional `progress` callback once per
candidate BEFORE the symbol's work, the cycle wires it to the t7_desk @@CYCLE
line, and a broken callback must never take the desk down with it."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import atlas.agents.desk as desk_mod
import atlas.ops.daily as daily
from atlas.agents.desk import run_desk
from atlas.core.clock import FrozenClock

CLOCK = FrozenClock(datetime(2026, 8, 13, 0, 0, tzinfo=UTC))


class _Result:
    def scalar(self):
        return 0


class _Session:
    """Just enough session for a desk run whose every symbol is skipped:
    the closing cost query is the only SQL that executes."""
    def execute(self, *a, **k):
        return _Result()


def _skip_all_evidence(monkeypatch):
    def raise_lookup(session, symbol):
        raise LookupError("no evidence in this test")
    monkeypatch.setattr(desk_mod, "build_evidence", raise_lookup)


def test_progress_beats_once_per_symbol_in_order(monkeypatch):
    _skip_all_evidence(monkeypatch)
    beats: list[str] = []
    report = run_desk(_Session(), CLOCK, ["AAA", "BBB", "CCC"],
                      progress=beats.append)
    assert beats == ["memo 1/3 · AAA", "memo 2/3 · BBB", "memo 3/3 · CCC"]
    assert len(report.skipped) == 3          # the beat precedes the work


def test_progress_failure_never_kills_the_desk(monkeypatch):
    _skip_all_evidence(monkeypatch)

    def boom(msg: str) -> None:
        raise RuntimeError("console pipe gone")

    report = run_desk(_Session(), CLOCK, ["AAA"], progress=boom)
    assert len(report.skipped) == 1          # heartbeat failure swallowed


def test_no_progress_callback_stays_silent(monkeypatch):
    _skip_all_evidence(monkeypatch)
    report = run_desk(_Session(), CLOCK, ["AAA"])
    assert len(report.skipped) == 1


def test_build_scanned_desk_threads_progress_through(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_desk(session, clock, symbols, progress=None):
        captured["progress"] = progress
        return SimpleNamespace(summary=lambda: "ok")

    monkeypatch.setattr(daily, "active_signal_symbols", lambda s, c: [])
    monkeypatch.setattr(daily, "active_pead_signal_symbols", lambda s, c: [])
    monkeypatch.setattr(daily, "scan",
                        lambda s, c, top_n: SimpleNamespace(shortlist=[],
                                                            scanned=0, n_held=0))

    def sentinel(msg: str) -> None:
        pass

    scanned = daily.build_scanned_desk(fake_run_desk, lambda s: [],
                                       progress=sentinel)
    scanned(_Session(), CLOCK)
    assert captured["progress"] is sentinel
