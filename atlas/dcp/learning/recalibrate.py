"""Calibration computation (learning loop v1): the accumulated outcome labels
folded into per-conviction / per-specialist / per-source calibration rows via
the EXISTING Brier machinery (calibration.py — ADR-0003, Constitution 10.3
shrinkage, [0.5, 1.5] clip). Nothing here is reimplemented: brier_score and
conviction_weight are imported and called; this module only assembles their
Forecast inputs from learning.outcome_labels and persists snapshots.

SURFACING ONLY (v1): Article 10.1 places "agent conviction-weight updates" in
Tier 1 (automatic), so COMPUTING and STORING these weights nightly is squarely
permitted — but nothing in the codebase consumes a conviction weight today,
and wiring a consumer would touch the agents plane. v1 therefore computes and
stores; APPLICATION is a Principal decision (and its wiring a reviewed
follow-up). Because no behavior changes, these snapshots are measurements,
not Tier-1 adjustments — learning.adjustments stays empty and the 10.2
before/after obligation is met by prev_weight on each row plus the audit
event. A 10.4 learning freeze has nothing to suspend.

THREE ROW FAMILIES in learning.agent_calibration (agent_role keying; the 0002
columns keep their exact semantics — brier_score is always a Brier score,
conviction_weight always the clipped shrinkage weight of those forecasts):

- conviction:<LEVEL>  one Forecast per graded directional non-shadow memo
  label at that conviction: Forecast(level, direction_vindicated). The
  conviction ladder scored against what it claimed (CONVICTION_PROB).
- specialist:<role>   one Forecast per graded specialist label:
  Forecast(confidence.upper(), aligned) — the seat claims, with its stated
  confidence, that its directional stance is right; Brier scores that claim.
  Neutral stances carry no claim and are excluded (aligned IS NULL).
- source:<tag>        one Forecast per graded directional non-shadow memo
  label from that source (NULL source = 'desk nightly', the scorecard's
  label), at the memo's own conviction. Memos without a scoreable conviction
  (NULL / N/A) are excluded from the Brier but reported in the trust rates.

Rates that do NOT fit the 0002 columns are not crammed into them: specialist
alignment/flag-validation rates and per-source vindication-vs-dartboard are
computed by the pure functions below, carried on the snapshot's audit event,
and recomputed at read time by /v1/learning/summary from the same labels —
same inputs, same numbers, no schema abuse and no migration for derivable
values.

SNAPSHOTS are append-only: period = the injected clock's UTC date, regime =
'all' (regime slicing is future work), prev_weight = the latest prior
snapshot's weight for the same row (10.2's before/after). Rows are inserted
with ON CONFLICT DO NOTHING — a snapshot, once taken, is a fact. ONE
learning.calibration.snapshot audit event per run that wrote rows.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.learning.calibration import (CONVICTION_PROB, Forecast,
                                            brier_score, conviction_weight)
from atlas.dcp.learning.labeling import MemoLabel, SpecialistLabel
from atlas.dcp.scorecard import HORIZONS, dartboard_baseline

REGIME = "all"                         # regime-sliced calibration is future work
DESK_SOURCE = "desk nightly"           # the scorecard's label for source IS NULL
SPECIALIST_ROLES: tuple[str, ...] = ("quality", "growth", "macro")
CONVICTION_LEVELS: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")


@dataclass(frozen=True)
class CalibrationRow:
    """One learning.agent_calibration snapshot row, pre-insert."""
    agent_role: str
    n_forecasts: int
    brier: float
    weight: float
    prev_weight: float | None


@dataclass(frozen=True)
class SpecialistReliability:
    role: str
    n_graded: int                      # aligned IS NOT NULL
    n_aligned: int
    n_flagged: int                     # flag_validated IS NOT NULL
    n_flags_validated: int

    @property
    def alignment_rate(self) -> float | None:
        return self.n_aligned / self.n_graded if self.n_graded else None

    @property
    def flag_validation_rate(self) -> float | None:
        return self.n_flags_validated / self.n_flagged if self.n_flagged else None


@dataclass(frozen=True)
class HorizonTrust:
    """One source's graded record at one horizon, against the dart."""
    n_graded: int
    n_vindicated: int
    rate: float | None
    baseline: float | None             # mixed-direction dart (module docstring)
    edge: float | None                 # rate - baseline


@dataclass(frozen=True)
class CalibrationReport:
    period: str
    rows: tuple[CalibrationRow, ...]
    reliability: tuple[SpecialistReliability, ...]
    trust: dict[str, dict[int, HorizonTrust]]

    def summary(self) -> str:
        return (f"calibration snapshot {self.period} ({len(self.rows)} rows)"
                if self.rows else "calibration: no graded labels yet")


def conviction_forecasts(memo_labels: Sequence[MemoLabel],
                         ) -> dict[str, list[Forecast]]:
    """Family 1 (module docstring): graded directional non-shadow labels,
    bucketed by the memo's conviction level."""
    out: dict[str, list[Forecast]] = {}
    for ml in memo_labels:
        if ml.direction_vindicated is None or ml.shadow:
            continue
        if ml.conviction not in CONVICTION_PROB:
            continue
        out.setdefault(ml.conviction, []).append(
            Forecast(ml.conviction, ml.direction_vindicated))
    return out


def specialist_forecasts(spec_labels: Sequence[SpecialistLabel],
                         ) -> dict[str, list[Forecast]]:
    """Family 2: each graded stance is the seat's claim, at its stated
    confidence, that the stance is right — Forecast(confidence, aligned)."""
    out: dict[str, list[Forecast]] = {}
    for sl in spec_labels:
        if sl.aligned is None:
            continue
        level = sl.confidence.upper()
        if level not in CONVICTION_PROB:
            continue
        out.setdefault(sl.role, []).append(Forecast(level, sl.aligned))
    return out


def source_forecasts(memo_labels: Sequence[MemoLabel],
                     ) -> dict[str, list[Forecast]]:
    """Family 3: graded directional non-shadow labels bucketed by source
    (NULL = 'desk nightly'), each at the memo's own conviction."""
    out: dict[str, list[Forecast]] = {}
    for ml in memo_labels:
        if ml.direction_vindicated is None or ml.shadow:
            continue
        if ml.conviction not in CONVICTION_PROB:
            continue
        out.setdefault(ml.source or DESK_SOURCE, []).append(
            Forecast(ml.conviction, ml.direction_vindicated))
    return out


def specialist_reliability(spec_labels: Sequence[SpecialistLabel],
                           ) -> tuple[SpecialistReliability, ...]:
    """Per-role alignment and flag-validation arithmetic, canonical role
    order, roles with zero labels omitted (an empty seat has no record)."""
    out: list[SpecialistReliability] = []
    roles = [r for r in SPECIALIST_ROLES
             if any(sl.role == r for sl in spec_labels)]
    roles += sorted({sl.role for sl in spec_labels} - set(SPECIALIST_ROLES))
    for role in roles:
        mine = [sl for sl in spec_labels if sl.role == role]
        graded = [sl for sl in mine if sl.aligned is not None]
        flagged = [sl for sl in mine if sl.flag_validated is not None]
        out.append(SpecialistReliability(
            role=role, n_graded=len(graded),
            n_aligned=sum(1 for sl in graded if sl.aligned),
            n_flagged=len(flagged),
            n_flags_validated=sum(1 for sl in flagged if sl.flag_validated)))
    return tuple(out)


def source_trust(memo_labels: Sequence[MemoLabel],
                 ) -> dict[str, dict[int, HorizonTrust]]:
    """Per-source vindication vs the dartboard, per horizon. The dart's
    universe is EVERY memo label at the horizon (HOLD and shadow included —
    the scorecard's own rule, via the one dartboard_baseline). A source mixes
    BUYs and REJECTs, so its dart baseline is the mean of each graded memo's
    own direction-baseline — the score a direction-blind dart throwing the
    SAME mix of calls would get."""
    out: dict[str, dict[int, HorizonTrust]] = {}
    for h in HORIZONS:
        at_h = [ml for ml in memo_labels if ml.horizon_sessions == h]
        universe = [ml.excess for ml in at_h]
        graded_at_h = [ml for ml in at_h
                       if ml.direction_vindicated is not None and not ml.shadow]
        for src in sorted({ml.source or DESK_SOURCE for ml in graded_at_h}):
            mine = [ml for ml in graded_at_h
                    if (ml.source or DESK_SOURCE) == src]
            wins = sum(1 for ml in mine if ml.direction_vindicated)
            baselines = [dartboard_baseline(ml.recommendation, universe)
                         for ml in mine]
            usable = [b for b in baselines if b is not None]
            baseline = (float(sum(usable) / Decimal(len(usable)))
                        if usable and len(usable) == len(baselines) else None)
            rate = wins / len(mine) if mine else None
            out.setdefault(src, {})[h] = HorizonTrust(
                n_graded=len(mine), n_vindicated=wins, rate=rate,
                baseline=baseline,
                edge=(None if rate is None or baseline is None
                      else rate - baseline))
    return out


def read_labels(session: Session) -> tuple[list[MemoLabel],
                                           list[SpecialistLabel]]:
    """The label corpus as recorded (0030 columns), memo symbol joined for
    display — deterministic order. Shared by the snapshot and the read API so
    both compute from identical inputs."""
    memo_rows = session.execute(text(
        "SELECT l.thesis_memo_id, l.horizon_sessions, l.recommendation, "
        "       l.conviction, l.source, l.shadow, l.direction_vindicated, "
        "       l.excess, m.instrument_symbol AS symbol "
        "FROM learning.outcome_labels l "
        "JOIN research.memos m ON m.id = l.thesis_memo_id "
        "WHERE l.label_kind = 'memo' "
        "ORDER BY l.labeled_at, l.thesis_memo_id, l.horizon_sessions")).all()
    spec_rows = session.execute(text(
        "SELECT thesis_memo_id, horizon_sessions, specialist_role, "
        "       specialist_stance, specialist_confidence, n_red_flags, "
        "       aligned, flag_validated "
        "FROM learning.outcome_labels WHERE label_kind = 'specialist' "
        "ORDER BY labeled_at, thesis_memo_id, horizon_sessions, "
        "         specialist_role")).all()
    memos = [MemoLabel(
        memo_id=str(r.thesis_memo_id), symbol=r.symbol,
        horizon_sessions=int(r.horizon_sessions),
        recommendation=r.recommendation, conviction=r.conviction,
        source=r.source, shadow=bool(r.shadow), excess=Decimal(r.excess),
        direction_vindicated=r.direction_vindicated) for r in memo_rows]
    specs = [SpecialistLabel(
        memo_id=str(r.thesis_memo_id), horizon_sessions=int(r.horizon_sessions),
        role=r.specialist_role, stance=r.specialist_stance,
        confidence=r.specialist_confidence,
        n_red_flags=int(r.n_red_flags or 0), aligned=r.aligned,
        flag_validated=r.flag_validated) for r in spec_rows]
    return memos, specs


def plan_rows(memo_labels: Sequence[MemoLabel],
              spec_labels: Sequence[SpecialistLabel]) -> list[CalibrationRow]:
    """Pure snapshot planning: the three families through the EXISTING
    calibration math, canonical order, families with zero forecasts omitted
    (nothing to snapshot is nothing to store)."""
    rows: list[CalibrationRow] = []
    conv = conviction_forecasts(memo_labels)
    for level in CONVICTION_LEVELS:
        fs = conv.get(level)
        if fs:
            rows.append(CalibrationRow(
                agent_role=f"conviction:{level}", n_forecasts=len(fs),
                brier=brier_score(fs), weight=conviction_weight(fs),
                prev_weight=None))
    spec = specialist_forecasts(spec_labels)
    roles = [r for r in SPECIALIST_ROLES if r in spec]
    roles += sorted(set(spec) - set(SPECIALIST_ROLES))
    for role in roles:
        fs = spec[role]
        rows.append(CalibrationRow(
            agent_role=f"specialist:{role}", n_forecasts=len(fs),
            brier=brier_score(fs), weight=conviction_weight(fs),
            prev_weight=None))
    for src, fs in sorted(source_forecasts(memo_labels).items()):
        rows.append(CalibrationRow(
            agent_role=f"source:{src}", n_forecasts=len(fs),
            brier=brier_score(fs), weight=conviction_weight(fs),
            prev_weight=None))
    return rows


def _prev_weight(session: Session, agent_role: str, period: str) -> float | None:
    row = session.execute(text(
        "SELECT conviction_weight FROM learning.agent_calibration "
        "WHERE agent_role = :r AND regime = :g AND period < :p "
        "ORDER BY period DESC LIMIT 1"),
        {"r": agent_role, "g": REGIME, "p": period}).scalar()
    return None if row is None else float(row)


def snapshot_calibration(session: Session, clock: Clock) -> CalibrationReport:
    """Compute and persist tonight's calibration snapshot (module docstring).
    Append-only: period = the clock's UTC date; a re-run on the same date
    finds its rows already present (ON CONFLICT DO NOTHING) — a snapshot,
    once taken, is a fact. ONE audit event when rows were written."""
    now = clock.now()
    period = now.astimezone(UTC).date().isoformat()
    memo_labels, spec_labels = read_labels(session)
    planned = plan_rows(memo_labels, spec_labels)
    rows = tuple(CalibrationRow(
        agent_role=r.agent_role, n_forecasts=r.n_forecasts, brier=r.brier,
        weight=r.weight, prev_weight=_prev_weight(session, r.agent_role,
                                                  period))
        for r in planned)
    reliability = specialist_reliability(spec_labels)
    trust = source_trust(memo_labels)
    if not rows:
        return CalibrationReport(period=period, rows=(),
                                 reliability=reliability, trust=trust)
    for r in rows:
        session.execute(text(
            "INSERT INTO learning.agent_calibration "
            "(agent_role, period, regime, n_forecasts, brier_score, "
            " conviction_weight, prev_weight, updated_at) "
            "VALUES (:r, :p, :g, :n, :b, :w, :pw, :t) "
            "ON CONFLICT (agent_role, period, regime) DO NOTHING"),
            {"r": r.agent_role, "p": period, "g": REGIME, "n": r.n_forecasts,
             "b": r.brier, "w": r.weight, "pw": r.prev_weight, "t": now})
    PostgresAuditLog(session, clock).append(
        event_type="learning.calibration.snapshot", entity_type="learning",
        entity_id=period, actor_type="dcp", actor_id="learning",
        payload={
            "period": period, "rows": len(rows), "applied": False,
            "weights": {r.agent_role: {"weight": r.weight, "brier": r.brier,
                                       "n": r.n_forecasts,
                                       "prev_weight": r.prev_weight}
                        for r in rows},
            "specialist_reliability": {
                rel.role: {"alignment_rate": rel.alignment_rate,
                           "n_graded": rel.n_graded,
                           "flag_validation_rate": rel.flag_validation_rate,
                           "n_flagged": rel.n_flagged}
                for rel in reliability},
            "source_trust": {
                src: {str(h): {"rate": t.rate, "baseline": t.baseline,
                               "edge": t.edge, "n": t.n_graded}
                      for h, t in by_h.items()}
                for src, by_h in trust.items()}})
    return CalibrationReport(period=period, rows=rows,
                             reliability=reliability, trust=trust)
