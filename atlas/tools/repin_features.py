"""Re-pin registered feature definitions after a REVIEWED, spec-identical
refactor — the registry-side half of "widening the catalog is a reviewed
change".

register_feature (features/store.py) refuses ANY divergence from a registered
definition — deliberately: unreviewed drift in feature math must fail loudly.
But a reviewed refactor that MOVES a member's hashed source (the families/
restructure moved the momentum variants out of factory/features.py) changes
code_sha with the math untouched, leaving the registry pinned to a sha the
code can no longer produce. This tool is the deliberate, audited unlock:

  * ONLY code_sha may change. The stored name, version AND canonical spec
    must match the current catalog definition exactly — a spec or version
    divergence means the MATH moved, which is a redefinition (new name or an
    explicitly reviewed version bump with its own evidence), refused here.
  * VALUE VERIFICATION, not trust: before repinning, a sample of the
    feature's STORED values (latest vintage, several instruments) is
    recomputed with the CURRENT code and must match byte-for-byte — the same
    check as materialize's stale-fact guard. A same-spec MATH change (the
    one thing the metadata checks cannot see, because the spec is prose +
    parameters) fails here and is refused. This check reads only committed
    facts, so it cannot be weakened by the same diff that changed the code.
    A feature with no stored values is repinned with that fact recorded.
  * Every repin appends an audit event (feature.repinned: old sha, new sha,
    per-file source digests for independent verification, the sample size
    verified, the operator's --reason) — the chain records what moved, why,
    and what was checked.
  * Idempotent: an already-current pin is reported and skipped. Nothing
    prints until the batch COMMITS — a mid-batch refusal rolls back and says
    so; no line ever claims a repin that did not durably happen.

Usage:
  python -m atlas.tools.repin_features --features momentum_6_1,momentum_3_1 \
      --reason "families/ restructure: construction moved verbatim, math unchanged"
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from atlas.dcp.factory.features import RANKABLE_FEATURES
from atlas.dcp.features.store import FeatureDefinition, _canonical

VERIFY_SAMPLE_ROWS = 24     # stored facts recomputed before a repin is allowed


class RepinRefused(RuntimeError):
    """A divergence beyond code_sha — this tool must not paper over it."""


def _verify_stored_values(session: Session, definition: FeatureDefinition,
                          feature_id: object) -> int:
    """Recompute a sample of the feature's STORED values with the CURRENT code
    and demand byte-for-byte equality (the stale-fact guard's rule). Returns
    the number of facts verified (0 = nothing stored yet); raises RepinRefused
    on any mismatch — that is a MATH change wearing a refactor's clothes."""
    rows = session.execute(text(
        "SELECT fv.instrument_id, fv.session_date, fv.value, i.symbol "
        "FROM quant.feature_values fv "
        "JOIN market.instruments i ON i.id = fv.instrument_id "
        "WHERE fv.feature_id = :fid "
        "  AND fv.dataset_version = (SELECT dataset_version "
        "      FROM quant.feature_values WHERE feature_id = :fid "
        "      ORDER BY computed_at DESC LIMIT 1) "
        "ORDER BY fv.instrument_id, fv.session_date DESC LIMIT :n"),
        {"fid": feature_id, "n": VERIFY_SAMPLE_ROWS}).all()
    verified = 0
    by_instrument: dict[object, list] = {}
    for r in rows:
        by_instrument.setdefault((r.instrument_id, r.symbol), []).append(r)
    for (iid, symbol), facts in by_instrument.items():
        sessions: list[date] = [f.session_date for f in facts]
        recomputed = definition.compute(session, symbol, iid, sessions)
        for f in facts:
            got = recomputed.get(f.session_date)
            if got is None or float(f.value) != got:
                raise RepinRefused(
                    f"{definition.name}: stored value for {symbol} at "
                    f"{f.session_date} does not reproduce under the current "
                    f"code ({f.value} stored, {got!r} recomputed) — the MATH "
                    f"changed; a repin would silently corrupt the feature's "
                    f"identity. Redefine under a new name or a reviewed "
                    f"version bump.")
            verified += 1
    return verified


def _file_digests(definition: FeatureDefinition) -> dict[str, str]:
    """Per-file sha256 of the definition's sources — lands in the audit event
    so the repin is independently verifiable against any checkout."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in definition.code_paths}


def repin_feature(session: Session, clock: Clock, name: str, *,
                  reason: str) -> str:
    """Re-pin one catalog feature's registry row to the current code_sha.
    Returns a one-line report; raises RepinRefused on anything but a pure
    sha refresh (see module docstring)."""
    if name not in RANKABLE_FEATURES:
        raise RepinRefused(f"{name!r} is not in the catalog — nothing to pin")
    definition = RANKABLE_FEATURES[name]
    new_sha = definition.code_sha()
    row = session.execute(text(
        "SELECT id, version, code_sha, spec FROM quant.feature_definitions "
        "WHERE name = :n"), {"n": name}).first()
    if row is None:
        return f"{name}: not registered — nothing to repin (first use registers)"
    if str(row.version) != definition.version:
        raise RepinRefused(
            f"{name}: stored version {row.version!r} != catalog "
            f"{definition.version!r} — a version change is a redefinition, "
            f"not a repin; review it with its own evidence")
    if _canonical(row.spec) != _canonical(dict(definition.spec)):
        raise RepinRefused(
            f"{name}: stored spec differs from the catalog spec — the MATH "
            f"moved; a redefinition needs a new name or a reviewed version "
            f"bump, never a repin")
    if str(row.code_sha) == new_sha:
        return f"{name}: pin already current ({new_sha[:12]}…)"
    verified = _verify_stored_values(session, definition, row.id)
    session.execute(text(
        "UPDATE quant.feature_definitions SET code_sha = :sha "
        "WHERE id = :id"), {"sha": new_sha, "id": row.id})
    PostgresAuditLog(session, clock).append(
        event_type="feature.repinned", entity_type="feature",
        entity_id=name, actor_type="human", actor_id="repin_features",
        payload={"name": name, "version": definition.version,
                 "old_code_sha": str(row.code_sha), "new_code_sha": new_sha,
                 "code_paths": [str(p) for p in definition.code_paths],
                 "file_digests": _file_digests(definition),
                 "stored_values_verified": verified,
                 "spec": json.loads(_canonical(dict(definition.spec))),
                 "reason": reason})
    tail = (f"{verified} stored fact(s) reproduced byte-for-byte"
            if verified else "no stored values existed to verify")
    return (f"{name}: repinned {str(row.code_sha)[:12]}… -> {new_sha[:12]}… "
            f"({tail}; audited)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Audited re-pin of spec-identical feature definitions "
                    "after a reviewed refactor")
    p.add_argument("--features", required=True,
                   help="comma-separated catalog feature names")
    p.add_argument("--reason", required=True,
                   help="why the pin moved — lands verbatim in the audit event")
    a = p.parse_args(argv)
    names = [n.strip() for n in a.features.split(",") if n.strip()]
    if not names:
        p.error("no feature names given")
    clock = SystemClock()
    lines: list[str] = []
    try:
        with session_scope() as s:              # commits on clean exit
            for name in names:
                lines.append(repin_feature(s, clock, name, reason=a.reason))
    except RepinRefused as e:
        # the whole batch rolled back — no line may claim otherwise
        print(f"NOTHING repinned (batch rolled back): {e}")
        return 1
    for line in lines:                          # printed only ONCE durable
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
