"""Instrument / issuer identity resolution (F-002).

Answers the question F-001's date-overlap guard could only answer by proxy: *who
is the issuer behind this instrument, as of a date, and can we vouch for it?*
The identity is keyed on a permanent identifier — ISIN (primary), CUSIP
(secondary) — read from ``market.fundamentals.payload->'General'`` (real data;
ISINs are unique per instrument and present for the active tradeable set).

Fail-closed is the whole point. The reused-ticker danger (ADT / VAL / MNK and the
delisted history generally) lands exactly on the rows that have no permanent
identifier — EODHD serves no current fundamentals for a delisted ticker — so an
unresolved identity resolves to ``None`` and can never be silently trusted.

Honest scope (see migration 0037): we hold ONE vendor snapshot, so there is one
OPEN identity per instrument with a conservative ``valid_from`` (the first stored
bar — the span we can attest) and ``history_complete = false``. ``resolve_identity``
is therefore point-in-time correct at the interface — asked for a date before
``valid_from`` it fails closed rather than pretending the current identity applied
historically — but the *dated change-history* (multiple closed rows across a
ticker's life) needs a vendor symbol-change feed we do not ingest. The schema
accepts that history later without rework.

This module is pure DCP: no agent imports, no sizing/pricing outputs — it only
resolves identity and refuses when it cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

IDENTITY_SOURCE = "eodhd_fundamentals"


@dataclass(frozen=True)
class IssuerIdentity:
    """A resolved (or explicitly unresolved) instrument identity as of a date."""
    instrument_id: str
    isin: str | None
    cusip: str | None
    figi: str | None
    primary_ticker: str | None
    identity_currency: str | None
    country_iso: str | None
    valid_from: date
    valid_to: date | None
    known_from: datetime
    identity_source: str
    history_complete: bool
    is_resolved: bool


class IssuerDriftError(RuntimeError):
    """A held position's instrument no longer resolves to its pinned issuer."""


# --------------------------------------------------------------------------- #
# permanent-key helpers
# --------------------------------------------------------------------------- #
def _permanent_key(idn: IssuerIdentity | None) -> str | None:
    """The canonical permanent identifier, ISIN preferred then CUSIP, or None
    when the identity carries neither (unresolved)."""
    if idn is None or not idn.is_resolved:
        return None
    if idn.isin:
        return f"ISIN:{idn.isin}"
    if idn.cusip:
        return f"CUSIP:{idn.cusip}"
    return None


def identity_key(idn: IssuerIdentity) -> str:
    """The permanent key, or raise — an unresolved identity has no key and must
    never be used as if it identified an issuer."""
    key = _permanent_key(idn)
    if key is None:
        raise ValueError(f"instrument {idn.instrument_id} has no permanent "
                         "identifier — identity is unresolved")
    return key


def same_issuer(a: IssuerIdentity | None, b: IssuerIdentity | None) -> bool:
    """True only when both identities are resolved and share a permanent
    identifier. Fails closed (False) on any unresolved side — two unknowns are
    NOT assumed equal."""
    ka, kb = _permanent_key(a), _permanent_key(b)
    return ka is not None and ka == kb


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _row_to_identity(row: Any) -> IssuerIdentity:
    return IssuerIdentity(
        instrument_id=str(row.instrument_id), isin=row.isin, cusip=row.cusip,
        figi=row.figi, primary_ticker=row.primary_ticker,
        identity_currency=(row.identity_currency.strip()
                           if row.identity_currency else None),
        country_iso=row.country_iso, valid_from=row.valid_from,
        valid_to=row.valid_to, known_from=row.known_from,
        identity_source=row.identity_source,
        history_complete=row.history_complete, is_resolved=row.is_resolved)


def resolve_identity(session: Session, instrument_id: str, *,
                     as_of: date | None = None) -> IssuerIdentity | None:
    """The issuer identity behind ``instrument_id``, or None (fail-closed).

    ``as_of=None`` asks for the current identity (the OPEN row). A date asks for
    the identity valid on that day: ``valid_from <= as_of < valid_to`` (open
    ``valid_to`` treated as +inf). Returns None when there is no identity row,
    when it is unresolved (no permanent identifier), or when ``as_of`` falls
    before the span we can attest — we never claim the current identity applied
    to an era we cannot vouch for (the reused-ticker guard, made explicit)."""
    row = session.execute(text("""
        SELECT instrument_id, isin, cusip, figi, primary_ticker, identity_currency,
               country_iso, valid_from, valid_to, known_from, identity_source,
               history_complete, is_resolved
        FROM market.instrument_identity
        WHERE instrument_id = :iid
          AND is_resolved = true
          AND ( (CAST(:as_of AS date) IS NULL AND valid_to IS NULL)
             OR (CAST(:as_of AS date) IS NOT NULL
                 AND valid_from <= CAST(:as_of AS date)
                 AND (valid_to IS NULL OR CAST(:as_of AS date) < valid_to)) )
        ORDER BY valid_from DESC
        LIMIT 1
    """), {"iid": instrument_id, "as_of": as_of}).first()
    return _row_to_identity(row) if row is not None else None


def _instrument_id_for_symbol(session: Session, symbol: str) -> str | None:
    """Resolve a symbol to a single instrument id, or None when absent OR
    ambiguous (more than one instrument carries the symbol) — ambiguity fails
    closed, it is never silently first-row-wins."""
    rows = session.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :s"),
        {"s": symbol}).all()
    return str(rows[0].id) if len(rows) == 1 else None


def resolve_by_symbol(session: Session, symbol: str, *,
                      as_of: date | None = None) -> IssuerIdentity | None:
    """Convenience: resolve identity for a ticker symbol. Fails closed when the
    symbol is absent or ambiguous."""
    iid = _instrument_id_for_symbol(session, symbol)
    return resolve_identity(session, iid, as_of=as_of) if iid is not None else None


# --------------------------------------------------------------------------- #
# held-position issuer pin (forward capital-safety, add-only / fail-closed)
# --------------------------------------------------------------------------- #
def pin_issuer(session: Session, instrument_id: str, *,
               as_of: date | None = None) -> str:
    """The permanent key to pin at position open. Raises when identity is
    unresolved — a position must not be opened against an instrument whose issuer
    we cannot identify."""
    idn = resolve_identity(session, instrument_id, as_of=as_of)
    if idn is None:
        raise IssuerDriftError(
            f"instrument {instrument_id} has no resolved issuer identity "
            f"(as_of={as_of}) — refusing to pin/open")
    return identity_key(idn)


def issuer_drifted(session: Session, instrument_id: str, pinned_key: str, *,
                   as_of: date | None = None) -> bool:
    """True when the instrument no longer resolves to its pinned issuer (a
    ticker reassigned under a held position, or identity lost). None resolution
    is treated as drift — we cannot vouch that it is still the same issuer."""
    cur = resolve_identity(session, instrument_id, as_of=as_of)
    if cur is None:
        return True
    return identity_key(cur) != pinned_key


def require_stable_issuer(session: Session, instrument_id: str, pinned_key: str, *,
                          as_of: date | None = None) -> None:
    """Raise IssuerDriftError if the held position's issuer has drifted."""
    if issuer_drifted(session, instrument_id, pinned_key, as_of=as_of):
        raise IssuerDriftError(
            f"instrument {instrument_id} no longer resolves to pinned issuer "
            f"{pinned_key} (as_of={as_of}) — issuer drift, fail closed")


def held_position_issuer_drifted(session: Session, instrument_id: str,
                                 opened_at: datetime) -> bool:
    """F-002 capital-safety: True when an instrument HELD since ``opened_at`` no
    longer resolves to the same issuer it did at open — its ticker was reassigned
    to a different company (a break refresh_identity versioned), or its identity
    was lost. Fails closed only when the open-era issuer WAS resolvable; an
    unassessable legacy position (no identity row covering its open date) is not
    flagged, so the check never fires spuriously on pre-identity-system holdings.
    Consumed by the daily reconciliation to halt the book on issuer drift."""
    at_open = resolve_identity(session, instrument_id, as_of=opened_at.date())
    if _permanent_key(at_open) is None:
        return False
    return not same_issuer(at_open, resolve_identity(session, instrument_id))


def reused_ticker_is_identity_unvouched(session: Session, symbol: str, *,
                                        as_of: date) -> bool:
    """F-001/F-002 corroboration: a reused-ticker series excluded by the
    membership date-overlap guard must ALSO be unvouchable by issuer identity as
    of the (stale) membership era — resolution returns None. True confirms the
    exclusion is an identity failure, not merely a date miss."""
    return resolve_by_symbol(session, symbol, as_of=as_of) is None


# --------------------------------------------------------------------------- #
# population (idempotent) — from real fundamentals, no fabricated identifiers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IdentityPopulateOutcome:
    with_fundamentals: int   # instruments that got an identity row (resolved or not)
    resolved: int            # of those, carrying a permanent identifier
    unresolved: int          # of those, with fundamentals but no ISIN/CUSIP


def _clean(value: object) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


# One OPEN identity per instrument (partial-unique target), refreshed in place.
_UPSERT_CONFLICT = """
ON CONFLICT (instrument_id) WHERE valid_to IS NULL DO UPDATE SET
    isin = excluded.isin, cusip = excluded.cusip, figi = excluded.figi,
    primary_ticker = excluded.primary_ticker,
    identity_currency = excluded.identity_currency,
    country_iso = excluded.country_iso, valid_from = excluded.valid_from,
    known_from = excluded.known_from, identity_source = excluded.identity_source,
    is_resolved = excluded.is_resolved
"""


def refresh_identity(session: Session, instrument_id: str,
                     payload: dict[str, object], *, known_from: datetime,
                     source: str = IDENTITY_SOURCE) -> bool:
    """Upsert one instrument's OPEN identity from a fundamentals payload in hand
    (the ingest hook). ``valid_from`` = first stored bar, else the ingest date.
    Returns is_resolved.

    F-002 identity-break versioning: if the instrument already has a RESOLVED open
    identity and the fresh payload carries a DIFFERENT permanent key (the vendor
    reassigned the ticker to another issuer — a reused-ticker splice), do NOT
    silently overwrite the open row's ISIN while keeping its old ``valid_from``
    (that mis-vouches the NEW issuer over the OLD issuer's era). Instead CLOSE the
    old row at the break date and OPEN a new one from the break date. Bitemporally
    correct: ``resolve_identity(as_of=<old era>)`` still returns the old issuer,
    and the break is a durable, queryable multi-row history (``issuer_breaks``)."""
    gen = payload.get("General") if isinstance(payload, dict) else None
    gen = gen if isinstance(gen, dict) else {}
    isin, cusip = _clean(gen.get("ISIN")), _clean(gen.get("CUSIP"))
    resolved = bool(isin or cusip)
    new_key = (f"ISIN:{isin}" if isin else f"CUSIP:{cusip}" if cusip else None)
    fields = {"iid": instrument_id, "isin": isin, "cusip": cusip,
              "prim": _clean(gen.get("PrimaryTicker")),
              "ccy": _clean(gen.get("CurrencyCode")),
              "ctry": _clean(gen.get("CountryISO")),
              "fallback": known_from.date(), "known_from": known_from,
              "source": source, "resolved": resolved}

    cur = resolve_identity(session, instrument_id, as_of=None)   # current OPEN row
    cur_key = _permanent_key(cur)
    if cur_key is not None and new_key is not None and cur_key != new_key:
        # IDENTITY BREAK — version, never overwrite.
        break_date = known_from.date()
        session.execute(text(
            "UPDATE market.instrument_identity SET valid_to = :bd "
            "WHERE instrument_id = :iid AND valid_to IS NULL"),
            {"bd": break_date, "iid": instrument_id})
        session.execute(text(
            "INSERT INTO market.instrument_identity "
            "(instrument_id, isin, cusip, figi, primary_ticker, identity_currency, "
            " country_iso, valid_from, valid_to, known_from, identity_source, "
            " history_complete, is_resolved) "
            "VALUES (:iid, :isin, :cusip, NULL, :prim, :ccy, :ctry, :bd, NULL, "
            " :known_from, :source, false, :resolved)"),
            {**fields, "bd": break_date})
        return resolved

    session.execute(text(f"""
        INSERT INTO market.instrument_identity
            (instrument_id, isin, cusip, figi, primary_ticker, identity_currency,
             country_iso, valid_from, valid_to, known_from, identity_source,
             history_complete, is_resolved)
        VALUES (:iid, :isin, :cusip, NULL, :prim, :ccy, :ctry,
            COALESCE((SELECT min(bar_date) FROM market.price_bars_daily
                      WHERE instrument_id = :iid), :fallback),
            NULL, :known_from, :source, false, :resolved)
        {_UPSERT_CONFLICT}
    """), fields)
    return resolved


def issuer_break_count(session: Session, instrument_id: str) -> int:
    """F-002: how many CLOSED (versioned) identity rows an instrument carries —
    i.e. how many times its ticker was reassigned to a different issuer. 0 for a
    clean instrument; >0 flags a reused-ticker splice for operator follow-up."""
    return int(session.execute(text(
        "SELECT count(*) FROM market.instrument_identity "
        "WHERE instrument_id = :iid AND valid_to IS NOT NULL"),
        {"iid": instrument_id}).scalar() or 0)


def populate_identities(session: Session, *, known_from: datetime,
                        source: str = IDENTITY_SOURCE) -> IdentityPopulateOutcome:
    """Bulk backfill/refresh from the latest fundamentals snapshot per instrument.
    Idempotent (re-runnable). Instruments with no fundamentals row get NO identity
    (resolve fails closed on them). No identifiers are fabricated — every value is
    read straight from the vendor payload."""
    session.execute(text(f"""
        INSERT INTO market.instrument_identity
            (instrument_id, isin, cusip, figi, primary_ticker, identity_currency,
             country_iso, valid_from, valid_to, known_from, identity_source,
             history_complete, is_resolved)
        SELECT i.id,
               NULLIF(f.payload->'General'->>'ISIN','')          AS isin,
               NULLIF(f.payload->'General'->>'CUSIP','')         AS cusip,
               NULL                                              AS figi,
               NULLIF(f.payload->'General'->>'PrimaryTicker','') AS primary_ticker,
               NULLIF(f.payload->'General'->>'CurrencyCode','')  AS identity_currency,
               NULLIF(f.payload->'General'->>'CountryISO','')    AS country_iso,
               COALESCE(b.first_bar, f.as_of, i.created_at::date) AS valid_from,
               NULL::date                                        AS valid_to,
               :known_from                                        AS known_from,
               :source                                            AS identity_source,
               false                                              AS history_complete,
               (NULLIF(f.payload->'General'->>'ISIN','') IS NOT NULL
                 OR NULLIF(f.payload->'General'->>'CUSIP','') IS NOT NULL) AS is_resolved
        FROM market.instruments i
        JOIN LATERAL (
            SELECT payload, as_of FROM market.fundamentals ff
            WHERE ff.instrument_id = i.id ORDER BY as_of DESC LIMIT 1) f ON true
        LEFT JOIN LATERAL (
            SELECT min(bar_date) AS first_bar FROM market.price_bars_daily pb
            WHERE pb.instrument_id = i.id) b ON true
        {_UPSERT_CONFLICT}
    """), {"known_from": known_from, "source": source})

    counts = session.execute(text("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE is_resolved) AS resolved,
               count(*) FILTER (WHERE NOT is_resolved) AS unresolved
        FROM market.instrument_identity WHERE valid_to IS NULL
    """)).first()
    assert counts is not None                     # count(*) always returns one row
    return IdentityPopulateOutcome(
        with_fundamentals=counts.total, resolved=counts.resolved,
        unresolved=counts.unresolved)
