"""Database engine/session setup.

SQLite by default (see config.database_url). Swapping to PostgreSQL in
production is just setting DATABASE_URL — no code changes required, since
all queries go through the SQLAlchemy ORM in models.py.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Base


def _make_engine(url: str | None = None):
    settings = get_settings()
    url = url or settings.database_url
    connect_args = {}
    kwargs = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if url in ("sqlite:///:memory:", "sqlite://"):
            # A plain in-memory URL opens a *new* empty DB per connection
            # unless pinned to a single shared connection via StaticPool.
            kwargs["poolclass"] = StaticPool
        else:
            db_path = url.split(":///", 1)[1]
            if db_path:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, connect_args=connect_args, future=True, **kwargs)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables that don't already exist. Never drops data."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
