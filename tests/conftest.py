"""Integration-test database isolation.

ALL Postgres-backed tests run against a dedicated `atlas_test` database, never the
shared dev DB: fixtures here TRUNCATE tables, and the dev DB holds real backfilled
history and the live audit chain. The test DB is created and migrated on demand.
A hard guard refuses to hand out sessions when the connected database is not the
test database — destructive fixtures must be impossible to point at real data.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).parents[1]
ADMIN_URL = os.environ.get(
    "ATLAS_DATABASE_URL",
    "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas")
URL = os.environ.get(
    "ATLAS_TEST_DATABASE_URL",
    ADMIN_URL.rsplit("/", 1)[0] + "/atlas_test")
# The guarded name derives from the URL so concurrent workstreams can run
# against isolated databases (ATLAS_TEST_DATABASE_URL=.../atlas_test_<name>),
# but destructive fixtures stay structurally unable to touch the dev DB: any
# name that does not begin with "atlas_test" is refused outright.
TEST_DB_NAME = URL.rsplit("/", 1)[-1]
if not TEST_DB_NAME.startswith("atlas_test"):
    raise RuntimeError(f"ATLAS_TEST_DATABASE_URL points at {TEST_DB_NAME!r} — "
                       "test databases must be named atlas_test*")

_prepared = False


@pytest.fixture(autouse=True)
def _reset_llm_client_cache():
    """registry.build_client caches one LLM client per resolution for the
    process (leak fix, 2026-07-14). That cache is global state; clear it around
    every test so real-registry tests stay hermetic and order-independent."""
    from atlas.agents.runtime.registry import reset_client_cache
    reset_client_cache()
    yield
    reset_client_cache()


def _reachable() -> bool:
    try:
        create_engine(ADMIN_URL).connect().close()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _reachable(), reason="postgres not reachable")


def _ensure_test_db() -> None:
    """Create atlas_test if missing and migrate it to head. Runs once per
    session. SELF-HEALING: the migration-cycle tests burn Postgres's per-table
    lifetime column budget a little every run (each downgrade/upgrade re-adds
    columns, and dropped-column slots are never reclaimed — the 1600 limit
    counts them forever), so after enough full-suite runs an upgrade dies with
    TooManyColumns mid-flight. The test DB is disposable by design: on ANY
    upgrade failure, drop it, recreate it, and migrate once from scratch —
    a corrupted bootstrap must never require a human to remember the DROP."""
    global _prepared
    if _prepared:
        return

    def _create_if_missing() -> None:
        admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                exists = c.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": TEST_DB_NAME}).scalar()
                if not exists:
                    c.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        finally:
            admin.dispose()

    def _upgrade() -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "ATLAS_DATABASE_URL": URL}
        return subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    _create_if_missing()
    r = _upgrade()
    if r.returncode != 0:
        admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" '
                               f"WITH (FORCE)"))
        finally:
            admin.dispose()
        _create_if_missing()
        r = _upgrade()
        if r.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade on {TEST_DB_NAME} failed even on a freshly "
                f"created database:\n{r.stderr}")
    _scrub_committed_market_world()
    _prepared = True


def _scrub_committed_market_world() -> None:
    """Session-START hygiene: several test files legitimately COMMIT the seed
    world (ingest replay, the daily cycle — their assertions need committed
    gates/chains), and whatever the LAST file of a run committed greets the
    FIRST file of the next run. Two ghosts came from exactly this: a leftover
    ACTIVE 'SPY' made compute_models' relative-strength pick a coin-flip, and
    leftover bar-less seed instruments turned the backfill-inception gates all
    red. Every run therefore starts from a clean market world; within a run,
    committing files still scrub after themselves where later files are
    sensitive. Registry/audit hygiene stays per-file (delta-based tests own
    their families)."""
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text(
                "TRUNCATE market.price_bars_daily, market.corporate_actions, "
                "market.fx_rates_daily, market.data_quality_gates, "
                "market.fundamentals, market.earnings_calendar, "
                "market.earnings_surprises, market.estimate_snapshots, "
                "market.instruments RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


def _assert_test_db(session) -> None:
    """Guard: destructive fixtures must never touch anything but atlas_test."""
    name = session.execute(text("SELECT current_database()")).scalar()
    if name != TEST_DB_NAME:
        pytest.fail(f"REFUSING to run destructive test fixtures against database "
                    f"{name!r} — tests may only touch {TEST_DB_NAME!r}")


@pytest.fixture
def pg_session():
    _ensure_test_db()
    engine = create_engine(URL)
    s = sessionmaker(bind=engine)()
    _assert_test_db(s)
    yield s
    s.rollback()
    s.close()
    # dispose the pool, not just the session: an abandoned engine keeps its
    # pooled connections open until GC, and a long run (or a concurrent one)
    # exhausts Postgres max_connections mid-suite — same failure mode
    # reset_app_engine() guards against for the API fixtures.
    engine.dispose()


@pytest.fixture
def clean_audit(pg_session):
    _assert_test_db(pg_session)
    pg_session.execute(text(
        "TRUNCATE audit.decision_events, research.memos, research.agent_runs "
        "RESTART IDENTITY CASCADE"))
    pg_session.commit()
    yield pg_session


def reset_app_engine() -> None:
    """Drop the app's cached engine AND dispose its connection pool. Every
    API-test fixture must use this instead of nulling db._session_factory
    directly: an abandoned engine keeps its pooled connections open, and
    enough of them exhausts Postgres max_connections mid-suite."""
    import atlas.core.db as db
    if db._session_factory is not None:
        db._session_factory.kw["bind"].dispose()
    db._session_factory = None
