import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

URL = "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas"


def _reachable() -> bool:
    try:
        create_engine(URL).connect().close()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _reachable(), reason="postgres not reachable")


@pytest.fixture
def pg_session():
    engine = create_engine(URL)
    s = sessionmaker(bind=engine)()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def clean_audit(pg_session):
    pg_session.execute(text(
        "TRUNCATE audit.decision_events, research.memos, research.agent_runs "
        "RESTART IDENTITY CASCADE"))
    pg_session.commit()
    yield pg_session
