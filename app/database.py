"""Database configuration and session management for Pulse-Check API.

Uses SQLAlchemy 2.x with SQLite. Configured with Write-Ahead Logging (WAL)
and busy timeouts to guarantee robust concurrent request handling.
"""

import os
from collections.abc import Generator
from datetime import datetime, timezone
from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Database URL defaulting to local SQLite database file
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pulse_check.db")

# SQLite connection arguments: allow multi-threaded access for FastAPI
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,
)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode, foreign keys, and busy timeout for concurrent safety in SQLite."""
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""

    pass


class UTCDateTime(TypeDecorator):
    """Ensures datetimes stored in SQLite are consistently timezone-aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value


def get_db() -> Generator[Session, None, None]:
    """Dependency provider that yields a database session per request and guarantees cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Creates database tables if they do not exist."""
    Base.metadata.create_all(bind=engine)
