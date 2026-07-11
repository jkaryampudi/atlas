"""SQLAlchemy session management. Application code receives sessions by injection."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from atlas.core.config import get_settings

_session_factory: sessionmaker[Session] | None = None


def _factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
