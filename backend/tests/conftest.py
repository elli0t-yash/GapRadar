from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import Collector, CollectorRun, Signal, Source  # noqa: F401
from app.factory import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_ENV="test", DATABASE_URL="", CORS_ORIGINS="http://localhost:5173"
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    return TestClient(app)


@pytest.fixture
def engine() -> Iterator[Engine]:
    # SQLite in-memory is used for fast unit tests only. It cannot fully
    # represent PostgreSQL: JSONB falls back to generic JSON text storage,
    # native UUID falls back to CHAR(32), and it does not enforce the same
    # constraint/locking semantics as PostgreSQL. Anything PostgreSQL-specific
    # is validated separately via the offline `alembic upgrade --sql` render,
    # not by these tests.
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
