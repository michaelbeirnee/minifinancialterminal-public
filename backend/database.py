"""SQLAlchemy engine / session wiring.

Defaults to a local SQLite file, but every model is portable — point
``MFT_DATABASE_URL`` at Postgres (``postgresql+psycopg://user:pw@host/db``) and
the same schema is created unchanged.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, Dict, List

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)

IS_SQLITE = settings.database_url.startswith("sqlite")

# ``check_same_thread`` is required for SQLite used across FastAPI threads.
_connect_args = {"check_same_thread": False} if IS_SQLITE else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Predictable constraint names, so a future Alembic migration can address them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


if IS_SQLITE:

    @event.listens_for(Engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """SQLite ignores foreign keys unless asked, and defaults to a slow journal."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def _server_default_for(column: Any) -> Any:
    """A literal usable as a DEFAULT when back-filling a NOT NULL column."""
    try:
        kind = column.type.python_type
    except (NotImplementedError, AttributeError):
        return None
    if kind is bool:
        return "0"
    if kind in (int, float):
        return "0"
    if kind is str:
        return "''"
    return None


def _sync_columns() -> List[str]:
    """Add columns that exist on a model but not yet in the database.

    ``create_all`` creates missing *tables* but never alters existing ones, so a
    database created before a model grew a field would fail on every query of
    it. This adds the missing columns in place — enough for the additive changes
    this project makes. Reach for Alembic before changing or dropping one.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: List[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all builds it from scratch
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                default = ""
                if not column.nullable:
                    # Existing rows need *some* value for a NOT NULL column.
                    literal = _server_default_for(column)
                    if literal is None:
                        log.warning(
                            "Skipping %s.%s: cannot add a NOT NULL column without a default",
                            table.name, column.name,
                        )
                        continue
                    default = " NOT NULL DEFAULT {}".format(literal)
                connection.execute(
                    text('ALTER TABLE "{}" ADD COLUMN "{}" {}{}'.format(
                        table.name, column.name, column.type.compile(engine.dialect), default))
                )
                applied.append("{}.{}".format(table.name, column.name))
    return applied


def init_db() -> None:
    """Create tables, then bring existing ones up to date."""
    from . import models  # noqa: F401  (registers ORM models on Base)

    Base.metadata.create_all(bind=engine)
    added = _sync_columns()
    if added:
        log.info("Added missing columns: %s", ", ".join(added))

    # Data migrations run after the schema is settled. This one is idempotent
    # and self-skipping, so it costs a single indexed scan on an already-fixed
    # database. Imported here to keep the thesis engine off the import path of
    # anything that only wants a session.
    from .thesis.memory import backfill_families

    moved = backfill_families()
    if moved.get("moved") or moved.get("merged"):
        log.info("Signal families backfilled: %s", moved)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def schema_overview() -> List[Dict[str, Any]]:
    """Table / column / row-count summary, surfaced at ``/api/system/database``."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    out: List[Dict[str, Any]] = []
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows = None
            if table.name in tables:
                try:
                    rows = connection.execute(
                        text('SELECT COUNT(*) FROM "{}"'.format(table.name))
                    ).scalar_one()
                except Exception:  # noqa: BLE001 - never let introspection 500
                    rows = None
            out.append(
                {
                    "table": table.name,
                    "rows": rows,
                    "columns": [
                        {"name": c["name"], "type": str(c["type"]), "nullable": bool(c["nullable"])}
                        for c in (inspector.get_columns(table.name) if table.name in tables else [])
                    ],
                }
            )
    return out
