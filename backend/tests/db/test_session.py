import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import session as db_session


def test_get_db_yields_a_session(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: factory)

    gen = db_session.get_db()
    session = next(gen)

    assert isinstance(session, Session)
    assert session.is_active

    gen.close()


def test_get_db_closes_session_after_use(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: factory)

    gen = db_session.get_db()
    session = next(gen)

    closed = False
    original_close = session.close

    def spy_close() -> None:
        nonlocal closed
        closed = True
        original_close()

    session.close = spy_close  # type: ignore[method-assign]
    gen.close()

    assert closed is True


def test_get_engine_is_not_created_at_import_time() -> None:
    # Importing the session module must not eagerly construct an engine;
    # it is built lazily on first call to get_engine(). An empty
    # DATABASE_URL (the Settings default) would fail immediately if the
    # module tried to build one at import time.
    import importlib
    import sys

    sys.modules.pop("app.db.session", None)
    fresh_module = importlib.import_module("app.db.session")

    assert fresh_module.get_engine.cache_info().currsize == 0
