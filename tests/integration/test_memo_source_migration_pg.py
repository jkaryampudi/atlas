"""Migration 0017 cycle: research.memos.source — the external-origin tag for
on-demand analyses (ANALYZE-ANY-TICKER). NULL = the desk's own work; a value
(e.g. 'investing.com') is stored VERBATIM. The column is deliberately nullable
text with no CHECK: the tag is free-form provenance that never enters a prompt
(cio.py), so the only contract worth pinning structurally is round-trip
fidelity."""
from __future__ import annotations

import os
import subprocess

from sqlalchemy import create_engine, text

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _column_exists() -> bool:
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return c.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'research' AND table_name = 'memos' "
                "  AND column_name = 'source'")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0017_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _column_exists()          # at head, the column is there
        _alembic("downgrade", "0016")
        assert not _column_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _column_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")      # never leave the test DB below head


def test_source_is_nullable_and_stored_verbatim(pg_session):
    s = pg_session
    plain = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs) "
        "VALUES ('committee', 'ZMSR', 'WATCHLIST', 'LOW', 'no tag', '[]') "
        "RETURNING source")).scalar()
    assert plain is None                 # NULL = the desk's own work

    tag = "investing.com — top picks #3 (verbatim!)"  # 40 chars, odd chars kept
    assert len(tag) <= 40
    stored = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs, source) "
        "VALUES ('committee', 'ZMSR', 'WATCHLIST', 'LOW', 'tagged', '[]', :src) "
        "RETURNING source"), {"src": tag}).scalar()
    assert stored == tag                 # verbatim, no normalisation
