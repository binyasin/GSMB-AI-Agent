"""Database engine/session setup.

SQLite by default (see config.database_url). Swapping to PostgreSQL in
production is just setting DATABASE_URL — no code changes required, since
all queries go through the SQLAlchemy ORM in models.py.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Base

logger = logging.getLogger("calls")


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
    """Create all tables that don't already exist, then add any columns a
    since-updated model gained that an existing table (with real data
    already in it) is still missing. There's no formal migration framework
    here -- Base.metadata.create_all only creates whole new tables, it never
    alters an existing one, so a plain new `Mapped[...]` column on a model
    would otherwise silently never show up on a database file created
    before that column was added. Never drops or alters existing data."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # just created above with every current column
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                logger.info("adding missing column %s.%s (%s)", table.name, column.name, col_type)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


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
