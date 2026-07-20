"""Research Factory console surface (phase 2 chassis): the ops runner invokes
the UNCHANGED factory gauntlet and reports the demote-only wording; refusals
come back with the grammar's message verbatim; the board pairs main/kill legs
with verdicts from the append-only audit event; the report endpoint validates
its path component. Committed registry rows are this module's own families,
scrubbed before and after (the test_recipe_run_pg hygiene convention); the
seeded ZIV fixture world stays inside the rolled-back test transaction.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import atlas.ops.recipes as ops_recipes
from atlas.api.main import app
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.factory.spec import RecipeSpec
from tests.conftest import URL, requires_pg, reset_app_engine
from tests.integration.test_impl_variant_pg import _seed

pytestmark = requires_pg

_OPS_FAMILY_PREFIX = "recipe-ops-"          # this module's committed families
_BOARD_FAMILY_PREFIX = "recipe-brdtest"


def _scrub(prefix: str) -> None:
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM quant.trial_registry "
                           "WHERE strategy_family LIKE :f"), {"f": prefix + "%"})
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _committed_registry_isolation(pg_session):
    _scrub(_OPS_FAMILY_PREFIX)
    _scrub(_BOARD_FAMILY_PREFIX)
    yield
    _scrub(_OPS_FAMILY_PREFIX)
    _scrub(_BOARD_FAMILY_PREFIX)


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    yield TestClient(app), clean_audit
    reset_app_engine()


def _good_spec(name: str = "ops-mom-12-1-top5") -> dict[str, object]:
    return {"name": name, "rank_feature": "momentum_12_1", "direction": "desc",
            "top_n": 5, "rebalance": "monthly", "universe": "pit-sp500",
            "cost_bps_per_side": 10, "lineage": "momentum",
            "rationale": "Winners keep winning (Jegadeesh-Titman 1993); "
                         "console-chassis exercise of the identical recipe.",
            "kill_start": "2013-01-02"}


@contextmanager
def _test_session(s):
    yield s


# ------------------------------------------------------------- catalog ------

def test_catalog_serves_the_closed_vocabulary(client):
    c, _s = client
    d = c.get("/v1/factory/recipes/catalog").json()
    names = {f["name"] for f in d["features"]}
    assert names == {"momentum_12_1", "momentum_6_1", "momentum_3_1",
                     "momentum_12_0"}
    assert all(f["lineage"] == "momentum" for f in d["features"])
    g = d["grammar"]
    assert g["direction"] == ["desc"] and g["rebalance"] == ["monthly"]
    assert g["cost_bps_per_side"] == 10
    assert g["top_n_min"] == 1 and g["top_n_max"] == 10


# ------------------------------------------------- refusals, verbatim -------

def test_grammar_refusal_comes_back_verbatim_400(client):
    c, _s = client
    bad = _good_spec() | {"cost_bps_per_side": 5}     # costs are never free
    r = c.post("/v1/factory/recipes/run", json={"spec": bad})
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "cost" in msg.lower()
    # nothing registered, nothing running
    assert ops_recipes.recipe_status()["running"] is False


def test_duplicate_name_is_refused_one_name_one_experiment(client, monkeypatch):
    c, s = client
    s.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics, "
        " lineage) VALUES (:f, 'deadbeef', '{}'::jsonb, 'momentum')"),
        {"f": "recipe-ops-mom-12-1-top5"})
    s.commit()
    r = c.post("/v1/factory/recipes/run", json={"spec": _good_spec()})
    assert r.status_code == 400
    assert "one name, one experiment" in r.json()["error"]["message"]


def test_busy_factory_answers_honestly(client, monkeypatch):
    c, _s = client
    assert ops_recipes._recipe_lock.acquire(blocking=False)
    try:
        r = c.post("/v1/factory/recipes/run",
                   json={"spec": _good_spec("ops-unseen-name")})
        assert r.status_code == 200
        assert r.json()["started"] is False
        assert "one at a time" in r.json()["note"]
    finally:
        ops_recipes._recipe_lock.release()


def test_started_path_spawns_and_releases(client, monkeypatch, tmp_path):
    c, _s = client
    ran: list[str] = []

    def fake_run(spec: RecipeSpec) -> None:
        ran.append(spec.name)
        ops_recipes._status.update(phase="done", result=None,
                                   detail="stubbed run finished")

    monkeypatch.setattr(ops_recipes, "_run", fake_run)
    monkeypatch.setattr(ops_recipes, "_REPO", tmp_path)
    r = c.post("/v1/factory/recipes/run",
               json={"spec": _good_spec("ops-thread-check")})
    assert r.status_code == 200 and r.json()["started"] is True
    # the pre-registration record lands BEFORE the run (crash-safe convention)
    assert (tmp_path / "docs" / "specs" / "ops-thread-check.json").is_file()
    for _ in range(100):                      # the daemon thread finishes fast
        if not ops_recipes.recipe_status()["running"]:
            break
        time.sleep(0.05)
    assert ran == ["ops-thread-check"]
    assert ops_recipes.recipe_status()["running"] is False


# ------------------------------- the real gauntlet through the ops seam -----

def test_ops_run_executes_the_unchanged_gauntlet(pg_session, monkeypatch, tmp_path):
    s = pg_session
    _seed(s)
    monkeypatch.setattr(ops_recipes, "session_scope", lambda: _test_session(s))
    monkeypatch.setattr(ops_recipes, "RECIPE_PATHS", 4)   # test-speed knob only
    monkeypatch.setattr(ops_recipes, "_REPO", tmp_path)

    spec = RecipeSpec(name="ops-mom-12-1-top5", rank_feature="momentum_12_1",
                      direction="desc", top_n=5, rebalance="monthly",
                      universe="pit-sp500", lineage="momentum",
                      rationale="Winners keep winning (Jegadeesh-Titman 1993); "
                                "console-chassis exercise.",
                      kill_start=date(2013, 1, 2))
    ops_recipes._run(spec)                    # synchronous: no thread, no race

    st = ops_recipes.recipe_status()
    assert st["phase"] == "done"
    result = st["result"]
    assert result["spec_hash"] == spec.spec_hash()
    assert len(result["legs"]) == 2
    assert result["legs"][0]["family"] == "recipe-ops-mom-12-1-top5"
    assert result["legs"][1]["family"] == "recipe-ops-mom-12-1-top5-2013"
    # the demote-only wording is applied, whatever the fixture verdicts are
    main_ok, kill_ok = (result["legs"][0]["passed"], result["legs"][1]["passed"])
    if main_ok and not kill_ok:
        assert result["verdict"].startswith("STRIKE")
    elif main_ok:
        assert result["verdict"].startswith("PASS")
    else:
        assert result["verdict"] == "FAIL"
    # the report is persisted under the (tmp) repo root; the spec file is
    # written by start_recipe BEFORE the run (tested on the submit path below)
    report = tmp_path / "docs" / "reports" / "recipe-ops-mom-12-1-top5.md"
    assert report.is_file() and "Pre-committed kill trial" in report.read_text()
    # both trials registered (committed by the factory's own discipline)
    fams = [r[0] for r in s.execute(text(
        "SELECT strategy_family FROM quant.trial_registry "
        "WHERE strategy_family LIKE 'recipe-ops-mom-12-1-top5%' "
        "ORDER BY created_at"))]
    assert fams == ["recipe-ops-mom-12-1-top5", "recipe-ops-mom-12-1-top5-2013"]


# ----------------------------------------------------------- the board ------

def test_board_pairs_legs_and_reads_verdicts_from_audit(client):
    c, s = client
    clock = FrozenClock(datetime(2026, 7, 20, 6, 0, tzinfo=UTC))
    ids = {}
    for fam in (_BOARD_FAMILY_PREFIX, f"{_BOARD_FAMILY_PREFIX}-2016"):
        ids[fam] = s.execute(text(
            "INSERT INTO quant.trial_registry (strategy_family, spec_hash, "
            " metrics, lineage, hypothesis) "
            "VALUES (:f, 'deadbeef', '{}'::jsonb, 'momentum', 'board test') "
            "RETURNING CAST(id AS text)"), {"f": fam}).scalar()
    audit = PostgresAuditLog(s, clock)
    audit.append(event_type="quant.backtest.completed", entity_type="strategy",
                 entity_id=f"{_BOARD_FAMILY_PREFIX}/portfolio",
                 actor_type="dcp", actor_id="recipe_run",
                 payload={"trial_id": ids[_BOARD_FAMILY_PREFIX],
                          "gate_passed": True, "gate_reasons": [],
                          "dsr": 0.93, "null_p": 0.001, "n_trials": 3})
    s.commit()

    rows = [r for r in c.get("/v1/factory/recipes").json()
            if r["family"].startswith(_BOARD_FAMILY_PREFIX)]
    by_leg = {r["leg"]: r for r in rows}
    assert set(by_leg) == {"main", "kill"}
    main, kill = by_leg["main"], by_leg["kill"]
    assert main["name"] == kill["name"] == _BOARD_FAMILY_PREFIX.removeprefix("recipe-")
    assert main["completed"] is True and main["gate_passed"] is True
    assert main["dsr"] == 0.93 and main["n_trials"] == 3
    # the kill leg has no completed event: reported as such, never guessed
    assert kill["completed"] is False and kill["gate_passed"] is None
    assert main["hypothesis"] == "board test"


def test_ops_constants_pin_the_cli_evidentiary_settings():
    # the console must produce evidence at the SAME null-model strength and
    # seed as the CLI (recipe_run main(): --paths 1000 --seed 7); a drift here
    # would silently weaken the console's runs
    assert ops_recipes.RECIPE_PATHS == 1000
    assert ops_recipes.RECIPE_SEED == 7


def test_refusal_message_is_the_grammars_verbatim(client):
    c, _s = client
    bad = _good_spec() | {"cost_bps_per_side": 5}
    _spec, expected = ops_recipes.validate_spec(bad)
    assert _spec is None and expected
    r = c.post("/v1/factory/recipes/run", json={"spec": bad})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == expected      # verbatim, not paraphrase


def test_crash_path_reports_burned_trials_and_releases(client, monkeypatch, tmp_path):
    c, s = client
    monkeypatch.setattr(ops_recipes, "_REPO", tmp_path)
    # a durable registered stub exists when the run dies mid-gauntlet
    s.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics, "
        " lineage) VALUES ('recipe-ops-crash-check', 'deadbeef', '{}'::jsonb, "
        " 'momentum')"))
    s.commit()

    def exploding_run(spec: RecipeSpec) -> None:
        raise RuntimeError("kill-leg materialization died")

    monkeypatch.setattr(ops_recipes, "_run", exploding_run)
    # bypass the duplicate-name guard: the stub row above IS the crash artifact
    monkeypatch.setattr(ops_recipes, "_family_exists", lambda name: False)
    r = c.post("/v1/factory/recipes/run",
               json={"spec": _good_spec("ops-crash-check")})
    assert r.json()["started"] is True
    for _ in range(100):
        st = ops_recipes.recipe_status()
        if st["phase"] == "failed" and not st["running"]:
            break
        time.sleep(0.05)
    assert st["phase"] == "failed" and st["running"] is False
    # the failure line states the burn — never implies a rollback
    assert "remain COUNTED" in str(st["detail"])
    assert "1 registered trial" in str(st["detail"])
    _scrub("recipe-ops-crash-check")


def test_board_join_reads_a_real_recipe_run_event(pg_session, monkeypatch, tmp_path):
    # the board's LATERAL join must work against the REAL audit payload the
    # gauntlet emits — not only a hand-crafted one. Run the tiny gauntlet, then
    # execute the board's exact SQL through the same (uncommitted) session.
    import atlas.api.routers.factory as factory_router

    s = pg_session
    _seed(s)
    monkeypatch.setattr(ops_recipes, "session_scope", lambda: _test_session(s))
    monkeypatch.setattr(ops_recipes, "RECIPE_PATHS", 4)
    monkeypatch.setattr(ops_recipes, "_REPO", tmp_path)
    spec = RecipeSpec(name="ops-brd-real", rank_feature="momentum_12_1",
                      direction="desc", top_n=5, rebalance="monthly",
                      universe="pit-sp500", lineage="momentum",
                      rationale="Board-join fidelity check on the real audit "
                                "payload the gauntlet emits.",
                      kill_start=date(2013, 1, 2))
    ops_recipes._run(spec)
    monkeypatch.setattr(factory_router, "session_scope", lambda: _test_session(s))
    rows = [r for r in factory_router.recipes_board()
            if r["name"] == "ops-brd-real"]
    by_leg = {r["leg"]: r for r in rows}
    assert set(by_leg) == {"main", "kill"}
    for leg in by_leg.values():
        assert leg["completed"] is True                  # real event joined
        assert leg["gate_passed"] in (True, False)       # verbatim, not guessed
        assert leg["n_trials"] is not None
    _scrub("recipe-ops-brd-real")


# ------------------------------------------------------------- report -------

def test_report_endpoint_validates_and_404s(client, monkeypatch, tmp_path):
    import atlas.api.routers.factory as factory_router
    c, _s = client
    assert c.get("/v1/factory/recipes/NOT%20legal/report").status_code == 400
    monkeypatch.setattr(factory_router, "_REPORTS", tmp_path)
    assert c.get("/v1/factory/recipes/no-such-recipe/report").status_code == 404
    (tmp_path / "recipe-brd-ok.md").write_text("# hello gauntlet")
    r = c.get("/v1/factory/recipes/brd-ok/report")
    assert r.status_code == 200 and "hello gauntlet" in r.text
