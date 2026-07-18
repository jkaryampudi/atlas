"""Point-in-time Feature Store — the storage contract (ADR-0011 step 1).

One table of definitions (quant.feature_definitions) and one table of values
(quant.feature_values). A feature's identity is its NAME; its (version,
code_sha, spec) are pins on that identity, and its values are point-in-time
facts keyed by (feature, instrument, session_date, dataset_version).

THE POINT-IN-TIME ANCHOR. ``session_date`` is the session whose CLOSE the
value is knowable AT. A value stored for session S answers "what did this
feature read at S's close?" — nothing more. Reads are structurally bounded:
``feature_at(..., on=X)`` queries only rows with session_date <= X (the SQL
carries the bound), so a value computed for a later session physically cannot
be returned for an earlier ``on``.

AS-OF READ SEMANTICS (v1: carry = 0 trading sessions). Both v1 features
(momentum_12_1, SUE) are materialized DENSELY — a row at every session where
the value is defined — so the value "knowable at ``on``" is the row at the
last trading session <= ``on`` and NOTHING OLDER: feature_at returns the
newest row with session_date <= on only if ZERO trading sessions of the
feature's market have elapsed strictly after session_date up to ``on``
(same-session and weekend/holiday reads succeed; one session later returns
None). Serving a stale value as fresh is exactly the failure mode a feature
store exists to prevent; a feature that legitimately carries forward must
bake the carry into its OWN materialization (as SUE does with its 63-session
staleness window), never into the read path.

CODE_SHA — prompts-are-code discipline applied to features. Each definition
declares the ordered tuple of source files that constitute its computation
(its own compute module plus every signal/adjustment module it imports math
from). code_sha = sha256 over the concatenation of the per-file sha256
digests, in declared order. register_feature is idempotent for an identical
(name, version, code_sha, spec) and REFUSES a mismatch: changed feature math
must arrive as a reviewed change (new version under a new name, or a signed
redefinition), never silently write under an old definition.

DATASET_VERSION — the exact definition. For a materialization of feature F
over symbols S with target sessions up to END:

    extent          = F.input_extent(db, sorted(S), END)
                      (a JSON mapping describing, per symbol, the actual
                       input rows with event-date <= END: min date, max date,
                       row count — each feature module documents its own
                       extent fields; the requested window and wall-clock
                       time are deliberately EXCLUDED)
    dataset_version = sha256( canonical_json(
                        {"feature": F.name, "version": F.version,
                         "extent": extent}) )       # hex digest, 64 chars

where canonical_json is json.dumps(..., sort_keys=True,
separators=(",", ":"), default=str). Consequences, all deliberate:
  * two materializations over identical stored inputs agree on the version —
    re-running is a no-op against the same rows;
  * any new/backfilled input row dated <= END produces a NEW version (values
    may differ, so they must not collide with the old fact);
  * rows dated AFTER END cannot change any value knowable by END and do not
    change the version;
  * the symbol SET is part of the identity: a panel's version names one
    (feature, universe, data-vintage) triple.

APPEND-ONLY BY CONVENTION. A (feature, instrument, session, version) value is
a fact once computed. materialize() writes with ON CONFLICT DO NOTHING on the
natural key: re-materializing the same version is a no-op (never an UPDATE),
and recomputation under new data lands under a new dataset_version beside the
old facts — history is never rewritten.

STALE-FACT GUARD (in-place revision honesty). dataset_version hashes the
input EXTENT (per-symbol min/max/row-count), not bar CONTENT, so an ingest
that revises a bar IN PLACE (the production upsert path) leaves the version
unchanged while the true value moves. materialize() therefore RECOMPUTES AND
COMPARES wherever the natural key already exists: identical — counted as
existing (the honest no-op re-run); different — RuntimeError naming the row,
because an append-only store must never silently re-serve a stale fact.
RESIDUAL, stated honestly: the guard fires only at materialization — a
reader pinning an old dataset_version (feature_at / feature_panel) without
re-materializing gets no content check — and an in-place revision that
changes no computed value passes silently, which is harmless because the
stored value is then still correct.

Two-plane wall: this module is pure DCP (core + dcp imports only) and never
touches atlas/agents. Timestamps come from the injected Clock (invariant 6).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.market_data.calendars import trading_days_between

ComputeFn = Callable[[Session, str, UUID, list[date]], dict[date, float]]
"""(db, symbol, instrument_id, target sessions) -> {session_date: value}.
Must be point-in-time: the value for session S may read only inputs knowable
by S's close. Sessions with no defined value are simply absent."""

ExtentFn = Callable[[Session, list[str], date], dict[str, object]]
"""(db, sorted symbols, end) -> JSON-serializable extent of the actual input
rows with event-date <= end (see DATASET_VERSION in the module docstring)."""


class FeaturePinError(RuntimeError):
    """A feature was re-registered with a different version/code_sha/spec —
    feature math changed without a reviewed redefinition."""


@dataclass(frozen=True)
class FeatureDefinition:
    """A registered feature: identity (name), pins (version, spec, code
    files) and the two functions that make it computable and versionable."""

    name: str
    version: str
    market: str                     # exchange calendar for session arithmetic
    spec: Mapping[str, object]      # pinned parameters, stored as jsonb
    code_paths: tuple[Path, ...]    # ordered sources hashed into code_sha
    compute: ComputeFn
    input_extent: ExtentFn

    def code_sha(self) -> str:
        """sha256 over the per-file sha256 digests, in declared order."""
        outer = hashlib.sha256()
        for p in self.code_paths:
            outer.update(hashlib.sha256(p.read_bytes()).digest())
        return outer.hexdigest()


def _canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)


def dataset_version_for(feature: FeatureDefinition,
                        extent: Mapping[str, object]) -> str:
    """The deterministic input-vintage hash (module docstring, exactly)."""
    return hashlib.sha256(_canonical(
        {"feature": feature.name, "version": feature.version,
         "extent": extent}).encode()).hexdigest()


def register_feature(db: Session, feature: FeatureDefinition, *,
                     clock: Clock) -> UUID:
    """Idempotent by (name, version, code_sha, spec): an identical
    re-registration returns the existing id; ANY divergence raises
    FeaturePinError (changed feature math is a reviewed change, never a
    silent overwrite). created_at comes from the injected clock."""
    sha = feature.code_sha()
    spec_json = _canonical(dict(feature.spec))
    row = db.execute(text(
        "SELECT id, version, code_sha, spec FROM quant.feature_definitions "
        "WHERE name = :n"), {"n": feature.name}).first()
    if row is not None:
        stored_spec = _canonical(row.spec)
        if (str(row.version) == feature.version and str(row.code_sha) == sha
                and stored_spec == spec_json):
            fid: UUID = row.id
            return fid
        raise FeaturePinError(
            f"feature {feature.name!r} is already registered with "
            f"version={row.version!r} code_sha={row.code_sha[:12]}… and a "
            f"pinned spec; refusing to re-register with version="
            f"{feature.version!r} code_sha={sha[:12]}… — feature math is "
            "code (CLAUDE.md invariant 5): redefine it as a reviewed change")
    new_id: UUID = db.execute(text(
        "INSERT INTO quant.feature_definitions "
        "(name, version, spec, code_sha, created_at) "
        "VALUES (:n, :v, CAST(:s AS jsonb), :c, :ca) RETURNING id"),
        {"n": feature.name, "v": feature.version, "s": spec_json,
         "c": sha, "ca": clock.now()}).scalar_one()
    return new_id


@dataclass(frozen=True)
class MaterializeReport:
    feature: str
    dataset_version: str
    sessions: int                    # target sessions requested
    inserted: int                    # rows newly written this run
    existing: int                    # rows already present (append-only no-op)
    computed: Mapping[str, int]      # symbol -> defined values
    failed: tuple[str, ...]          # symbols skipped (fail-soft)
    failures: tuple[str, ...]        # one message per failed symbol


def materialize(db: Session, feature: FeatureDefinition, *, clock: Clock,
                symbols: list[str], sessions: list[date]) -> MaterializeReport:
    """Register (idempotently), version the input extent, compute per symbol
    and append values. Fail-soft per symbol: a missing/ambiguous instrument
    row or a compute error is recorded and the run continues — honest counts
    are the deliverable. Re-materializing an identical extent is a no-op
    (ON CONFLICT DO NOTHING on the natural key; never an UPDATE) — but ONLY
    after the recomputed value byte-matches the stored fact; a mismatch under
    the same version is an in-place input revision and raises RuntimeError
    (STALE-FACT GUARD, module docstring) — fail-LOUD, not fail-soft: a
    silently re-served stale fact would corrupt every consumer."""
    if not sessions:
        raise ValueError("materialize needs at least one target session")
    feature_id = register_feature(db, feature, clock=clock)
    ordered = sorted(sessions)
    extent = feature.input_extent(db, sorted(symbols), ordered[-1])
    version = dataset_version_for(feature, extent)

    inserted = existing = 0
    computed: dict[str, int] = {}
    failed: list[str] = []
    failures: list[str] = []
    for symbol in sorted(symbols):
        iids = db.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s"),
            {"s": symbol}).scalars().all()
        if len(iids) != 1:
            failed.append(symbol)
            failures.append(f"{feature.name} {symbol}: "
                            f"{len(iids)} instrument rows (need exactly 1)")
            continue
        try:
            values = feature.compute(db, symbol, iids[0], ordered)
        except Exception as exc:  # fail-soft: one bad series never kills a run
            failed.append(symbol)
            failures.append(f"{feature.name} {symbol}: compute failed: {exc}")
            continue
        computed[symbol] = len(values)
        stored: dict[date, float] = {
            r.session_date: float(r.value) for r in db.execute(text(
                "SELECT session_date, value FROM quant.feature_values "
                "WHERE feature_id = :f AND instrument_id = :i "
                "  AND dataset_version = :dv"),
                {"f": feature_id, "i": iids[0], "dv": version})}
        for session_date in sorted(values):
            have = stored.get(session_date)
            if have is not None:
                # STALE-FACT GUARD (module docstring): the natural key
                # already holds a fact for this exact vintage — the freshly
                # recomputed value must byte-match it, or an input was
                # revised IN PLACE without moving the extent hash.
                if have != values[session_date]:
                    raise RuntimeError(
                        f"stale stored fact: {feature.name} {symbol} "
                        f"{session_date} dataset_version {version} holds "
                        f"{have!r} but the same extent now computes "
                        f"{values[session_date]!r} — an input row was "
                        "revised IN PLACE (extent unchanged), and an "
                        "append-only store must never silently re-serve a "
                        "stale fact; land the revision as new rows (a new "
                        "dataset_version), never an in-place update")
                existing += 1
                continue
            got = db.execute(text(
                "INSERT INTO quant.feature_values "
                "(feature_id, instrument_id, session_date, value, "
                " dataset_version, computed_at) "
                "VALUES (:f, :i, :d, :v, :dv, :ca) "
                "ON CONFLICT (feature_id, instrument_id, session_date, "
                "             dataset_version) DO NOTHING RETURNING value"),
                # Decimal(str(v)) — NEVER a float bind: Postgres' float8 ->
                # numeric cast truncates to 15 significant digits, silently
                # breaking the byte-identity the equivalence tests pin
                # (str() is Python's shortest round-trip representation, so
                # float(read back) == v exactly). Same discipline as the
                # signal generators' Decimal(str(...)) binds.
                {"f": feature_id, "i": iids[0], "d": session_date,
                 "v": Decimal(str(values[session_date])), "dv": version,
                 "ca": clock.now()}).first()
            if got is not None:
                inserted += 1
            else:
                existing += 1
        # STALE-FACT GUARD, orphan direction (adversarial re-attack
        # 2026-07-18): the loop above compares only sessions the fresh
        # compute PRODUCED. An in-place revision can instead flip a value
        # to UNCOMPUTABLE (e.g. a close revised to 0 fails the compute's
        # own c_form > 0 fail-closed check) while leaving the extent hash
        # unmoved (extent counts close IS NOT NULL rows, and 0 is not
        # NULL) — orphaning the stored fact, which feature_panel would
        # then silently re-serve. A stored row under THIS version at a
        # session THIS run targets, which the same extent can no longer
        # compute, is the same in-place-revision divergence — fail LOUD.
        # Restricted to the run's target sessions so denser historical
        # materializations at non-target sessions never false-positive.
        target_set = set(ordered)
        orphaned = sorted(d for d in stored
                          if d in target_set and d not in values)
        if orphaned:
            raise RuntimeError(
                f"stale stored fact (orphaned): {feature.name} {symbol} "
                f"{orphaned[0]} dataset_version {version} holds "
                f"{stored[orphaned[0]]!r} but the same extent no longer "
                f"computes a value there ({len(orphaned)} orphaned "
                "row(s)) — an input row was revised IN PLACE to an "
                "uncomputable state (extent unchanged); land revisions "
                "as new rows (a new dataset_version), never an in-place "
                "update")
    return MaterializeReport(
        feature=feature.name, dataset_version=version, sessions=len(ordered),
        inserted=inserted, existing=existing, computed=computed,
        failed=tuple(failed), failures=tuple(failures))


def latest_dataset_version(db: Session, feature: FeatureDefinition) -> str | None:
    """The most recently computed vintage for a feature (deterministic:
    newest computed_at, version text as tie-break). Convenience for live
    reads; anything decision-grade pins its version explicitly."""
    got = db.execute(text(
        "SELECT fv.dataset_version FROM quant.feature_values fv "
        "JOIN quant.feature_definitions fd ON fd.id = fv.feature_id "
        "WHERE fd.name = :n "
        "ORDER BY fv.computed_at DESC, fv.dataset_version DESC LIMIT 1"),
        {"n": feature.name}).scalar()
    return None if got is None else str(got)


def feature_at(db: Session, feature: FeatureDefinition, symbol: str, *,
               on: date, dataset_version: str | None = None) -> float | None:
    """The value knowable AT ``on`` — structural no-look-ahead: the query is
    bounded to session_date <= on, and the as-of rule (module docstring)
    rejects anything older than the last completed trading session, so a
    value computed for session S is invisible at any on < S and expires the
    session after (v1 carry = 0). None = no defined value: unknown feature,
    unmaterialized vintage, ineligible name, or stale row alike."""
    version = (dataset_version if dataset_version is not None
               else latest_dataset_version(db, feature))
    if version is None:
        return None
    row = db.execute(text(
        "SELECT fv.session_date, fv.value FROM quant.feature_values fv "
        "JOIN quant.feature_definitions fd ON fd.id = fv.feature_id "
        "JOIN market.instruments i ON i.id = fv.instrument_id "
        "WHERE fd.name = :n AND i.symbol = :s AND fv.dataset_version = :dv "
        "  AND fv.session_date <= :on "          # the structural bound
        "ORDER BY fv.session_date DESC LIMIT 1"),
        {"n": feature.name, "s": symbol, "dv": version, "on": on}).first()
    if row is None:
        return None
    elapsed = len(trading_days_between(feature.market,
                                       row.session_date, on)) - 1
    if elapsed > 0:                              # stale: a session has closed
        return None
    return float(row.value)


def feature_panel(db: Session, feature: FeatureDefinition,
                  symbols: list[str], *, start: date, end: date,
                  dataset_version: str) -> dict[str, dict[date, float]]:
    """Backtest read: every stored value in [start, end] for a PINNED
    dataset_version (a backtest that cannot name its data vintage is not
    reproducible — no default). Each value is keyed by the session whose
    close it was knowable at; the runner's own clamp (bars[:i+1] discipline)
    governs what a strategy may see, and nothing past ``end`` is returned."""
    panel: dict[str, dict[date, float]] = {s: {} for s in symbols}
    rows = db.execute(text(
        "SELECT i.symbol, fv.session_date, fv.value "
        "FROM quant.feature_values fv "
        "JOIN quant.feature_definitions fd ON fd.id = fv.feature_id "
        "JOIN market.instruments i ON i.id = fv.instrument_id "
        "WHERE fd.name = :n AND i.symbol = ANY(:syms) "
        "  AND fv.dataset_version = :dv "
        "  AND fv.session_date BETWEEN :a AND :b "
        "ORDER BY i.symbol, fv.session_date"),
        {"n": feature.name, "syms": list(symbols), "dv": dataset_version,
         "a": start, "b": end})
    for r in rows:
        panel[str(r.symbol)][r.session_date] = float(r.value)
    return panel
