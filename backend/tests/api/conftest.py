"""A TestClient wired to the in-memory database and a mocked provider.

The Bright Data dependency is always overridden with a client bound to an
httpx.MockTransport: no test on this surface may reach the real provider,
and the frontend it stands in for never holds a provider credential
either.
"""

from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.deps import get_brightdata_client
from app.config import Settings
from app.db.session import get_db
from app.factory import create_app
from app.integrations.brightdata.client import BrightDataClient
from tests.opportunity_engine.conftest import (  # noqa: F401 - re-exported fixtures
    collector,
    run,
    source,
)
from tests.pipeline.conftest import (  # noqa: F401 - re-exported fixtures
    production_records,
)


def refuse_provider_calls(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected Bright Data call: {request.url}")


@pytest.fixture
def brightdata_settings() -> Settings:
    return Settings(
        _env_file=None,
        APP_ENV="test",
        CORS_ORIGINS="http://localhost:5173",
        BRIGHTDATA_API_KEY="test-token-do-not-log",
        BRIGHTDATA_BASE_URL="https://api.brightdata.test",
    )


@pytest.fixture
def make_api_client(
    db_session: Session, brightdata_settings: Settings
) -> Iterator[Callable[..., TestClient]]:
    """Build a TestClient over a given provider handler."""
    clients: list[BrightDataClient] = []

    def build(
        handler: Callable[[httpx.Request], httpx.Response] = refuse_provider_calls,
    ) -> TestClient:
        app = create_app(settings=brightdata_settings)
        provider = BrightDataClient(
            settings=brightdata_settings, transport=httpx.MockTransport(handler)
        )
        clients.append(provider)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_brightdata_client] = lambda: provider
        return TestClient(app)

    yield build

    for provider in clients:
        provider.close()


@pytest.fixture
def api_client(make_api_client: Callable[..., TestClient]) -> TestClient:
    return make_api_client()
