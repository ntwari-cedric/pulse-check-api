"""Test configuration and fixtures for Pulse-Check API."""

from collections.abc import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

# ---------------------------------------------------------------------------
# In-memory SQLite engine — used for all single-threaded unit tests.
# StaticPool means every session shares the same connection so the in-memory
# database is visible across the whole test.
# ---------------------------------------------------------------------------
TEST_DATABASE_URL = "sqlite:///:memory:"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=test_engine,
)


@pytest.fixture(autouse=True)
def setup_database():
    """Builds clean database tables before each test and drops them afterwards."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provides a fresh isolated database session for direct model manipulation in tests."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Provides a FastAPI TestClient configured to use the isolated test database."""

    def override_get_db() -> Generator[Session, None, None]:
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# File-based SQLite engine — used ONLY for the concurrent heartbeat test.
# In-memory SQLite with StaticPool shares one sqlite3 connection across
# threads, which is not thread-safe (InterfaceError / bad API misuse).
# A file-based DB uses separate connections per thread via NullPool, which
# is correct for concurrency validation while still being ephemeral.
# ---------------------------------------------------------------------------
CONCURRENT_TEST_DATABASE_URL = "sqlite:///./test_concurrent.db"

concurrent_engine = create_engine(
    CONCURRENT_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
ConcurrentSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=concurrent_engine,
)


@pytest.fixture
def concurrent_client(tmp_path):
    """
    TestClient backed by a *file-based* SQLite database.

    Used exclusively for multi-threaded concurrency tests. File-based SQLite
    gives each thread its own connection, avoiding the sqlite3 thread-safety
    limitation of StaticPool/in-memory databases.
    """
    import os

    # Build the engine pointing at a fresh temp file
    db_path = tmp_path / "concurrent.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SessionFactory = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=engine,
    )
    Base.metadata.create_all(bind=engine)

    def override_get_db() -> Generator[Session, None, None]:
        session = SessionFactory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()

