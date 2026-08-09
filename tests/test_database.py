"""_add_missing_columns: the lightweight, no-framework schema migration that
adds any column a model gained after a database file was already created
with real data in it (see app/database.py's init_db docstring)."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.database import _add_missing_columns


def test_add_missing_columns_adds_new_columns_to_an_existing_table(tmp_path):
    db_path = tmp_path / "old_schema.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Simulate a database created before alternate_owner_contact/
    # payment_contact_number existed on the Consumer model: a `consumers`
    # table with only a bare-minimum subset of real columns.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE consumers (id INTEGER PRIMARY KEY, consumer_no TEXT)"))
        conn.execute(text("INSERT INTO consumers (id, consumer_no) VALUES (1, 'CN-001')"))

    import app.database as database_module

    original_engine = database_module.engine
    database_module.engine = engine
    try:
        _add_missing_columns()
    finally:
        database_module.engine = original_engine

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(consumers)"))}
        # The existing row must survive untouched.
        row = conn.execute(text("SELECT consumer_no FROM consumers WHERE id=1")).fetchone()

    assert "alternate_owner_contact" in columns
    assert "payment_contact_number" in columns
    assert "outstanding_amount" in columns  # a pre-existing column, unrelated to this feature
    assert row[0] == "CN-001"


def test_add_missing_columns_is_idempotent(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")

    import app.database as database_module

    original_engine = database_module.engine
    database_module.engine = engine
    try:
        from app.models import Base

        Base.metadata.create_all(bind=engine)
        _add_missing_columns()  # every column already present -- must not error
    finally:
        database_module.engine = original_engine
