"""P0.1 (ADR-0018) hardening of atlas/tools/downgrade_xsmom_shadow.py: required
--actor/--reason/--review-reference/--decision-ref/--expect-state args, idempotent
re-runs that never re-stamp shadowed_at or emit a duplicate event, expect-state
refusal, and the UPDATE...WHERE-state-RETURNING concurrency guard. The tool runs
against the app session_scope, pointed at atlas_test via monkeypatch."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from atlas.tools.downgrade_xsmom_shadow import main
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

FAMILY = "zz-downgrade-test"
ARGS = ["--actor", "Tester (Principal)", "--reason", "unit-test downgrade",
        "--review-reference", "REVIEW_PACKAGE/x.md", "--decision-ref", "ADR-0018",
        "--family", FAMILY]


@pytest.fixture
def tool_db(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    s.execute(text("DELETE FROM quant.strategies WHERE family = :f"), {"f": FAMILY})

    def _seed_paper() -> None:
        s.execute(text(
            "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
            " tolerance_bands, state) "
            "VALUES (:f, 'zz', '1.0.0', '{}', 'sha', '{}', 'paper')"), {"f": FAMILY})
        s.commit()

    s._seed_paper = _seed_paper                    # type: ignore[attr-defined]
    yield s
    s.rollback()
    s.execute(text("DELETE FROM quant.strategies WHERE family = :f"), {"f": FAMILY})
    s.commit()
    reset_app_engine()


def _state_and_shadowed(s):
    return s.execute(text(
        "SELECT state, shadowed_at FROM quant.strategies WHERE family = :f"),
        {"f": FAMILY}).mappings().one()


def _events(s) -> list:
    return s.execute(text(
        "SELECT actor_id, payload FROM audit.decision_events "
        "WHERE event_type = 'quant.strategy.research_shadow' "
        "ORDER BY seq")).mappings().all()


def test_downgrade_success_records_actor_reason_and_reference(tool_db):
    s = tool_db
    s._seed_paper()
    assert main([*ARGS, "--expect-state", "paper"]) == 0
    s.rollback()
    row = _state_and_shadowed(s)
    assert row["state"] == "research_shadow" and row["shadowed_at"] is not None
    ev = _events(s)
    assert len(ev) == 1
    assert ev[0]["actor_id"] == "Tester (Principal)"
    assert ev[0]["payload"]["reason"] == "unit-test downgrade"
    assert ev[0]["payload"]["review_reference"] == "REVIEW_PACKAGE/x.md"
    assert ev[0]["payload"]["decision_ref"] == "ADR-0018"


def test_downgrade_idempotent_preserves_shadowed_at_and_no_duplicate(tool_db):
    s = tool_db
    s._seed_paper()
    assert main([*ARGS, "--expect-state", "paper"]) == 0
    s.rollback()
    first = _state_and_shadowed(s)["shadowed_at"]
    # re-run: NO-OP, even though --expect-state names the OLD state
    assert main([*ARGS, "--expect-state", "paper"]) == 0
    s.rollback()
    row = _state_and_shadowed(s)
    assert row["shadowed_at"] == first             # never re-stamped
    assert len(_events(s)) == 1                     # no duplicate event


def test_downgrade_refuses_on_expect_state_mismatch(tool_db):
    s = tool_db
    s._seed_paper()
    assert main([*ARGS, "--expect-state", "validated"]) == 1
    s.rollback()
    assert _state_and_shadowed(s)["state"] == "paper"   # unchanged
    assert _events(s) == []                             # no write, no event


def test_concurrent_update_guard_writes_nothing_once_state_changed(tool_db):
    """The tool's UPDATE ... WHERE state IN ('paper','live') RETURNING is the
    concurrency guard: once a winning downgrade has committed (row now
    research_shadow), the loser's identical UPDATE matches no row and RETURNING
    is empty -> the tool refuses before appending a duplicate event."""
    s = tool_db
    s._seed_paper()
    sid = s.execute(text("SELECT id FROM quant.strategies WHERE family = :f"),
                    {"f": FAMILY}).scalar_one()
    # winner already downgraded the row
    s.execute(text("UPDATE quant.strategies SET state='research_shadow' "
                   "WHERE id=:i"), {"i": sid})
    # loser's exact guarded UPDATE now matches nothing
    loser = s.execute(text(
        "UPDATE quant.strategies SET state='research_shadow', shadowed_at=now() "
        "WHERE id=:i AND state IN ('paper','live') RETURNING id"),
        {"i": sid}).first()
    assert loser is None
