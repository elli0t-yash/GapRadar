from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, event
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


def _enable_sqlite_transaction_support(engine: Engine) -> None:
    """Make SQLite honor transactions, rollbacks, and SAVEPOINTs.

    The pysqlite driver opens connections in its own legacy autocommit
    mode and emits BEGIN only for some statements, which breaks SAVEPOINT
    and lets flushed rows survive a Session.rollback(). Without this,
    tests asserting transactional behavior (atomic ingestion, savepoint
    isolation of a duplicate insert) would pass or fail for reasons
    unrelated to the code under test.

    This is SQLAlchemy's documented workaround: disable the driver's
    implicit BEGIN and emit it explicitly instead.
    """

    @event.listens_for(engine, "connect")
    def disable_pysqlite_implicit_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def emit_explicit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


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
    _enable_sqlite_transaction_support(test_engine)
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
