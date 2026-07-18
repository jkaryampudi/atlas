"""GICS sector backfill for current index members (ADR-0016 decision 2).

WHY: the ~400 inactive current S&P 500 members were seeded through the
validation-instrument mechanism (index_membership.write_member_seeds_csv),
which leaves ``sector_gics`` EMPTY (``''`` — note: empty string, not NULL; the
tool treats both as missing). L3 sector-cap aggregation, factor overlap and
stress all key on exact sector strings, so a name without a real sector must
never activate. This tool resolves sectors from vendor fundamentals BEFORE
activation; activate_universe then fails closed on anything still missing.

FIELD MAPPING (probed live 2026-07-18 on A.US / ABNB.US / ADM.US and
cross-checked against all 115 stored market.fundamentals payloads):

- ``General.GicSector`` — official GICS sector names matching the local
  11-value vocabulary VERBATIM ("Health Care", "Information Technology", ...).
  Preferred source. Absent on some names (probe: HONA, BNY).
- ``General.Sector`` — the vendor's alternate taxonomy ("Healthcare",
  "Technology", "Financial Services", "Consumer Cyclical", ...). Fallback,
  translated through the closed ``VENDOR_SECTOR_TO_GICS`` mapping.

Fail-closed rules:
- a value outside BOTH closed vocabularies is UNRESOLVABLE: reported, left
  missing, and the name then fails out of activation. Vendor free text never
  reaches the database except through these two closed vocabularies.
- a non-missing ``sector_gics`` is NEVER overwritten (idempotent re-runs; the
  vendor disagrees with reviewed seed values on e.g. UBER/GEV and must not
  clobber them).
- vendor-delisted members are skipped WITHOUT a fetch — they can never
  activate (AGN-class corpses stay dead) and a delisted name may 404 anyway.
- a per-name vendor failure is recorded and the run continues (fail-soft per
  symbol, like the index-membership backfill) — honest coverage numbers are
  the deliverable.

Vendor cost: ~10 credits per fundamentals call, ~400 candidates on dev.
A dry-run performs the SAME read-only vendor fetches (to show exactly what
would be written) but writes NOTHING — no UPDATE, no audit event. Apply emits
ONE audit event with the full updated/unresolved/skipped breakdown.

Usage:
  python -m atlas.tools.backfill_gics             # dry-run (default)
  python -m atlas.tools.backfill_gics --apply
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.eodhd import vendor_symbol

INDEX_CODE = "GSPC.INDX"

# The 11 official GICS sectors, exactly as existing market.instruments rows
# spell them (probed on dev 2026-07-18; pinned by unit test). "Broad" is the
# diversified-ETF marker, deliberately NOT includable by this tool.
GICS_SECTORS = frozenset({
    "Communication Services", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials",
    "Information Technology", "Materials", "Real Estate", "Utilities",
})

# The vendor's alternate General.Sector taxonomy -> GICS. CLOSED mapping:
# exactly the 11 values observed across the stored payloads and live probes;
# anything else is unresolvable (fail closed), never guessed.
VENDOR_SECTOR_TO_GICS: dict[str, str] = {
    "Basic Materials": "Materials",
    "Communication Services": "Communication Services",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Industrials": "Industrials",
    "Real Estate": "Real Estate",
    "Technology": "Information Technology",
    "Utilities": "Utilities",
}

# vendor code (e.g. "AVGO.US") -> raw fundamentals document
FetchFundamentals = Callable[[str], Mapping[str, object]]


def resolve_sector(payload: Mapping[str, object]) -> str | None:
    """General.GicSector verbatim when inside the GICS vocabulary; else
    General.Sector through the closed mapping; else None (fail closed).
    Non-string values in either slot are hostile/noise and resolve nothing."""
    general = payload.get("General")
    if not isinstance(general, Mapping):
        return None
    gic = general.get("GicSector")
    if isinstance(gic, str) and gic.strip() in GICS_SECTORS:
        return gic.strip()
    alt = general.get("Sector")
    if isinstance(alt, str):
        return VENDOR_SECTOR_TO_GICS.get(alt.strip())
    return None


@dataclass(frozen=True)
class GicsBackfillReport:
    updated: tuple[tuple[str, str], ...]      # (symbol, sector), symbol-sorted
    unresolved: tuple[tuple[str, str], ...]   # (symbol, reason), symbol-sorted
    skipped_delisted: tuple[str, ...]
    already_have_sector: int                  # idempotency skips (non-missing)


def backfill_gics(session: Session, fetch: FetchFundamentals, *, apply: bool,
                  audit: PostgresAuditLog | None = None,
                  index_code: str = INDEX_CODE,
                  pause_s: float = 0.0) -> GicsBackfillReport:
    """Resolve and (with apply=True) write sectors for current index members
    whose sector_gics is missing. apply=True requires an audit log and emits
    exactly ONE event; apply=False writes nothing at all."""
    if apply and audit is None:
        raise ValueError("apply=True requires an audit log — every material "
                         "action is audited (CLAUDE.md invariant 4)")
    rows = session.execute(text(
        "SELECT m.ticker, m.is_delisted, i.id AS iid, i.exchange, "
        "       i.sector_gics "
        "FROM validation.index_membership m "
        "JOIN market.instruments i ON i.symbol = m.ticker AND i.market = 'US' "
        "WHERE m.index_code = :ic AND m.is_active_now "
        "ORDER BY m.ticker, i.exchange"), {"ic": index_code}).mappings().all()
    by_ticker: dict[str, list[Mapping[str, object]]] = {}
    for r in rows:
        by_ticker.setdefault(str(r["ticker"]), []).append(r)

    updated: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    skipped_delisted: list[str] = []
    already = 0
    for ticker in sorted(by_ticker):
        matches = by_ticker[ticker]
        if len(matches) > 1:
            unresolved.append((ticker, "ambiguous-instrument: multiple "
                                       "market='US' rows"))
            continue
        row = matches[0]
        if row["sector_gics"] not in (None, ""):
            already += 1
            continue
        if row["is_delisted"]:
            skipped_delisted.append(ticker)
            continue
        if pause_s:
            time.sleep(pause_s)  # vendor politeness (CLI only)
        try:
            payload = fetch(vendor_symbol(ticker, str(row["exchange"])))
        except Exception as exc:  # fail-soft per symbol: recorded, never fatal
            unresolved.append((ticker, f"{type(exc).__name__}: {exc}"))
            continue
        sector = resolve_sector(payload)
        if sector is None:
            unresolved.append((ticker, "no resolvable sector in "
                                       "General.GicSector / General.Sector"))
            continue
        if apply:
            res = session.execute(text(
                "UPDATE market.instruments SET sector_gics = :sec "
                "WHERE id = :iid AND (sector_gics IS NULL "
                "                     OR sector_gics = '')"),
                {"sec": sector, "iid": row["iid"]})
            if res.rowcount != 1:
                raise RuntimeError(f"{ticker}: expected exactly 1 row updated, "
                                   f"got {res.rowcount} — refusing to continue")
        updated.append((ticker, sector))

    report = GicsBackfillReport(
        updated=tuple(updated), unresolved=tuple(unresolved),
        skipped_delisted=tuple(skipped_delisted), already_have_sector=already)
    if apply:
        assert audit is not None  # checked above; narrows the type
        audit.append(
            event_type="market.instruments.gics_backfilled",
            entity_type="market", entity_id=index_code,
            actor_type="human", actor_id="backfill_gics",
            payload={"index_code": index_code, "dry_run": False,
                     "updated": dict(report.updated),
                     "updated_count": len(report.updated),
                     "unresolved": {s: r for s, r in report.unresolved},
                     "unresolved_count": len(report.unresolved),
                     "skipped_delisted": list(report.skipped_delisted),
                     "already_have_sector": report.already_have_sector,
                     "source_fields": ["General.GicSector", "General.Sector"]})
    return report


def _print_report(report: GicsBackfillReport, *, apply: bool) -> None:
    verb = "updated" if apply else "would update"
    print(f"gics backfill ({'APPLY' if apply else 'dry-run'}): "
          f"{verb}={len(report.updated)} unresolved={len(report.unresolved)} "
          f"skipped_delisted={len(report.skipped_delisted)} "
          f"already_have_sector={report.already_have_sector}")
    for sym, sector in report.updated:
        print(f"  {verb} {sym}: {sector}")
    for sym, reason in report.unresolved:
        print(f"  UNRESOLVED {sym}: {reason} — left missing; will fail closed "
              "out of activation")
    for sym in report.skipped_delisted:
        print(f"  skipped {sym}: vendor-delisted (AGN-class; never activates)")


def main() -> int:
    from atlas.core.clock import SystemClock
    from atlas.core.config import get_settings
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter

    p = argparse.ArgumentParser(
        description="Backfill instruments.sector_gics for current index "
                    "members from vendor fundamentals (ADR-0016 decision 2). "
                    "Dry-run by default; --apply writes + audits.")
    p.add_argument("--apply", action="store_true",
                   help="write resolved sectors and append the audit event "
                        "(default: dry-run, which fetches but writes nothing)")
    p.add_argument("--index", default=INDEX_CODE)
    p.add_argument("--pause", type=float, default=0.05,
                   help="seconds between vendor calls (politeness)")
    a = p.parse_args()

    settings = get_settings()
    if not settings.eodhd_api_key:
        raise SystemExit("ATLAS_EODHD_API_KEY is not set — the GICS backfill "
                         "needs the real vendor (no fixture equivalent)")
    adapter = EodhdAdapter(settings.eodhd_api_key)  # explicit vendor-code mode

    with session_scope() as s:
        report = backfill_gics(
            s, adapter.fetch_fundamentals, apply=a.apply,
            audit=PostgresAuditLog(s, SystemClock()) if a.apply else None,
            index_code=a.index, pause_s=a.pause)
    _print_report(report, apply=a.apply)
    if not a.apply:
        print("dry-run: nothing written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
