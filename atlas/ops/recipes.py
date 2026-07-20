"""RESEARCH FACTORY console runner (phase 2 chassis): submit a pre-registered
recipe spec from the console, run the full gauntlet in the background, and poll
its status — no command line (the console is the sole control surface).

Mirrors atlas/ops/analyze.py / screen.py exactly: a non-blocking lock (one
recipe at a time — "busy" is an answer, not an error), a daemon worker thread,
and a module-level status dict the API polls. Wall clock never enters the
gauntlet: the run clock is derived from the last stored bar exactly like the
recipe_run CLI (deterministic re-runs; invariant 6).

WHAT THIS DOES NOT CHANGE. The spec grammar, the registration-before-run count
discipline, the gauntlet and its thresholds, and the demote-only kill leg all
live in atlas/dcp/factory/* and are invoked UNCHANGED by import — this module
adds a trigger and a status surface, never a second path around any of it. A
refused spec is refused with the grammar's own message, verbatim.

ANTI-FAT-FINGER, not a gate: a spec whose name already has a registered
`recipe-<name>` trial (console- or CLI-registered), or whose name is claimed
by a DIFFERENT docs/specs/<name>.json on disk, is refused — one name, one
experiment. Re-clicking RUN must never silently burn a second pair of lineage
trials for the same hypothesis; a genuinely new hypothesis gets a new name
(and its own burn). The check is re-run after the lock is acquired, closing
the in-process submit race — and the STRUCTURAL guarantee now lives at the
registration chokepoint itself (recipe_run.run_recipe: pg_advisory_xact_lock
on the family + a count under that lock refuse a duplicate family across ALL
processes unless rerun=True is passed explicitly; the console never passes
it). The guards here remain the friendly early refusal; the chokepoint is
the backstop. The former documented residual (CLI+console race double-burn)
is CLOSED.

Every accepted spec is persisted to docs/specs/<name>.json BEFORE the run
starts (the pre-registration record convention the first-light runs
established — a crashed run must still leave the record beside its durable
registry stub); the authoritative record remains the registry row (hypothesis
+ spec_hash, registered and committed BEFORE the gauntlet runs).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.core.db import session_scope
from atlas.dcp.factory.recipe_run import (
    render_recipe_report,
    run_recipe_gauntlet,
)
from atlas.dcp.factory.spec import RecipeSpec, RecipeSpecError, spec_from_mapping

_REPO = Path(__file__).resolve().parents[2]
RECIPE_PATHS = 1000          # the committed null-model path count (CLI default)
RECIPE_SEED = 7              # the committed seed (CLI default)

_recipe_lock = threading.Lock()
_status: dict[str, object] = {"phase": "idle", "name": None, "spec_hash": None,
                              "started_at": None, "finished_at": None,
                              "detail": None, "result": None}


def validate_spec(raw: Mapping[str, Any]) -> tuple[RecipeSpec | None, str | None]:
    """(spec, None) for a grammar-legal mapping, else (None, the grammar's own
    refusal message verbatim). Never coerces — refusal is the contract."""
    try:
        return spec_from_mapping(raw), None
    except RecipeSpecError as e:
        return None, str(e)


def _family_exists(name: str) -> bool:
    """True when `recipe-<name>` already has a registered trial (console or
    CLI): one name, one experiment — a re-run must be a deliberate new name."""
    with session_scope() as s:
        return bool(s.execute(text(
            "SELECT 1 FROM quant.trial_registry "
            "WHERE strategy_family = :f LIMIT 1"),
            {"f": f"recipe-{name}"}).scalar())


def _registered_count(name: str) -> int:
    """Committed trials for this recipe's families (main + kill leg) — what a
    failed run has ALREADY burned; the honest number for the failure line.
    The kill match is anchored to the -<year> namespace so a sibling recipe
    named '<name>-something' is never counted as this one's leg."""
    with session_scope() as s:
        return int(s.execute(text(
            "SELECT count(*) FROM quant.trial_registry "
            "WHERE strategy_family = :f "
            "   OR strategy_family ~ :k"),
            {"f": f"recipe-{name}",
             "k": f"^recipe-{re.escape(name)}-(19|20)[0-9]{{2}}$"}).scalar() or 0)


def _run(spec: RecipeSpec) -> None:
    with session_scope() as s:
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source = 'EodhdAdapter'")).scalar()
        if last_bar is None:
            raise RuntimeError("no real bars stored — backfill first")
        # the CLI's exact convention: clock derived from the data, not the wall
        clock = FrozenClock(datetime(last_bar.year, last_bar.month,
                                     last_bar.day, 22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        main_run, kill_run = run_recipe_gauntlet(
            s, audit, spec, clock=clock, paths=RECIPE_PATHS, seed=RECIPE_SEED)

    report = render_recipe_report(main_run, kill_run, paths=RECIPE_PATHS)
    report_path = _REPO / "docs" / "reports" / f"recipe-{spec.name}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    legs = []
    for r in (main_run, kill_run):
        legs.append({"family": r.family,
                     "passed": bool(r.gate.passed),
                     "reasons": list(r.gate.reasons),
                     "dsr": r.gate.dsr, "null_p": r.gate.null_p_value,
                     "n_trials": r.n_trials})
    # the demote-only rule, stated in the status: a kill-leg FAIL is a strike
    # even when the full window passed — the recipe does not advance
    if legs[0]["passed"] and not legs[1]["passed"]:
        verdict = ("STRIKE — main window passed, pre-committed kill leg "
                   "failed; does not advance")
    elif legs[0]["passed"]:
        verdict = "PASS — both legs (still advisory; approval is a separate workflow)"
    else:
        verdict = "FAIL"
    _status.update(
        phase="done", finished_at=datetime.now(UTC).isoformat(),
        detail=(f"{verdict}; report docs/reports/recipe-{spec.name}.md"),
        result={"verdict": verdict, "legs": legs,
                "spec_hash": spec.spec_hash(),
                "report": f"docs/reports/recipe-{spec.name}.md"})


def start_recipe(raw: Mapping[str, Any]) -> dict[str, object]:
    """Console trigger. Returns {started, refused?, note}: a grammar-illegal
    spec or an already-registered name is REFUSED (started=False, refused
    message set); a busy factory answers started=False without refusal (one at
    a time; nothing runs twice)."""
    spec, refusal = validate_spec(raw)
    if spec is None:
        return {"started": False, "refused": refusal,
                "note": "spec refused by the v1 grammar — nothing registered"}
    if _family_exists(spec.name):
        return {"started": False,
                "refused": (f"name '{spec.name}' already has a registered "
                            f"recipe trial — one name, one experiment; a new "
                            f"hypothesis gets a new name (and its own counted "
                            f"burn)"),
                "note": "nothing registered, nothing run"}
    # disk-record guard: a docs/specs/<name>.json with DIFFERENT content means
    # the name is claimed by another experiment's pre-registration record (e.g.
    # a CLI-era spec whose registry rows were scrubbed) — refuse rather than
    # silently clobber the record. An identical file is a harmless re-attempt.
    existing_spec = _REPO / "docs" / "specs" / f"{spec.name}.json"
    if existing_spec.is_file():
        try:
            on_disk = json.loads(existing_spec.read_text())
        except ValueError:
            on_disk = None
        canonical = json.loads(json.dumps(spec.canonical(), default=str))
        if on_disk != canonical:
            return {"started": False,
                    "refused": (f"docs/specs/{spec.name}.json already exists "
                                f"with different content — the name is claimed "
                                f"by another pre-registration record; pick a "
                                f"new name"),
                    "note": "nothing registered, nothing run"}
    if not _recipe_lock.acquire(blocking=False):
        return {"started": False,
                "note": "a recipe is already running — one at a time"}
    # re-check UNDER the lock: two rapid submits with the same fresh name must
    # not both pass the pre-lock read (the in-process TOCTOU)
    if _family_exists(spec.name):
        _recipe_lock.release()
        return {"started": False,
                "refused": (f"name '{spec.name}' was registered while this "
                            f"submit was in flight — one name, one experiment"),
                "note": "nothing registered, nothing run"}
    # persist the pre-registration record BEFORE anything runs: a crashed run
    # must leave the spec beside its durable registry stub
    spec_path = _REPO / "docs" / "specs" / f"{spec.name}.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec.canonical(), indent=2, default=str) + "\n")
    _status.update(phase="running", name=spec.name, spec_hash=spec.spec_hash(),
                   started_at=datetime.now(UTC).isoformat(), finished_at=None,
                   detail=(f"registering + running the gauntlet for "
                           f"'{spec.name}' (burns 2 trials against lineage "
                           f"'{spec.lineage}')"),
                   result=None)

    def _target() -> None:
        from atlas.dcp.factory.recipe_run import DuplicateFamilyError
        try:
            _run(spec)
        except DuplicateFamilyError as e:
            # a chokepoint refusal is NOT a crash: THIS run burned nothing;
            # any counted rows for the name belong to the surface that won
            # the race (or an earlier run) — never attribute them to us
            _status.update(phase="failed",
                           finished_at=datetime.now(UTC).isoformat(),
                           detail=(f"refused at the registration chokepoint: "
                                   f"{e} Rows already counted under this name "
                                   f"belong to whichever run registered them — "
                                   f"the board is the durable record."[:340]))
        except Exception as e:  # noqa: BLE001 — the ops layer survives anything
            # HONESTY on failure: registration-before-run means trials may
            # already be durably counted — say exactly how many, never let a
            # 'failed' line imply the burn was rolled back
            try:
                burned = _registered_count(spec.name)
            except Exception:  # noqa: BLE001 — the count is best-effort
                burned = None
            burned_line = (f"; {burned} registered trial(s) for "
                           f"'{spec.name}' remain COUNTED (registration-"
                           f"before-run) — the board is the durable record"
                           if burned else
                           "; no trial had been registered yet"
                           if burned == 0 else "")
            _status.update(phase="failed",
                           finished_at=datetime.now(UTC).isoformat(),
                           detail=(str(e)[:260] + burned_line))
        finally:
            _recipe_lock.release()

    thread = threading.Thread(target=_target, name="atlas-recipe", daemon=True)
    try:
        thread.start()
    except Exception as e:
        _status.update(phase="failed",
                       finished_at=datetime.now(UTC).isoformat(),
                       detail=f"worker thread failed to start: {e}"[:300])
        _recipe_lock.release()
        raise
    return {"started": True,
            "note": (f"gauntlet running for '{spec.name}' — poll "
                     f"/v1/factory/recipes/status")}


def recipe_status() -> dict[str, object]:
    """Snapshot copy (same discipline as analysis_status)."""
    out = dict(_status)
    if isinstance(out.get("result"), dict):
        out["result"] = dict(out["result"])  # type: ignore[arg-type]
    out["running"] = _recipe_lock.locked()
    return out
