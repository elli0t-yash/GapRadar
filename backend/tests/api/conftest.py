"""A TestClient wired to the in-memory database and a mocked provider.

The Bright Data dependency is always overridden with a client bound to an
httpx.MockTransport: no test on this surface may reach the real provider,
and the frontend it stands in for never holds a provider credential
either.
"""

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    get_brightdata_client,
    get_pipeline_scheduler,
    get_research_enrichment_scheduler,
)
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


class RecordingScheduler:
    """Stands in for the local background executor.

    The real one opens its OWN session and Bright Data client, so it
    would bypass the overrides below and reach the deployment's real
    database and the real provider -- from a test, through
    TestClient's synchronous BackgroundTasks. This records the claim
    instead, which is also what makes "the request did not do the work"
    an assertion rather than a hope.
    """

    def __init__(self) -> None:
        self.scheduled: list[uuid.UUID] = []

    def __call__(self, pipeline_run_id: uuid.UUID) -> None:
        self.scheduled.append(pipeline_run_id)


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
def scheduler() -> RecordingScheduler:
    """The claims the API handed to the background executor."""
    return RecordingScheduler()


@pytest.fixture
def enrichment_scheduler() -> RecordingScheduler:
    """The research enrichments the API handed to the background executor.

    Overridden for the same reason as the pipeline one: the real executor
    opens its OWN session and its OWN provider clients, so it would bypass
    the overrides below and reach the real database, Bright Data and the
    LLM provider -- from a test, through TestClient's synchronous
    BackgroundTasks. Recording the claim instead is also what makes "the
    request did not do the work" an assertion rather than a hope.
    """
    return RecordingScheduler()


@pytest.fixture
def make_api_client(
    db_session: Session,
    brightdata_settings: Settings,
    scheduler: RecordingScheduler,
    enrichment_scheduler: RecordingScheduler,
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
        app.dependency_overrides[get_pipeline_scheduler] = lambda: scheduler
        app.dependency_overrides[get_research_enrichment_scheduler] = (
            lambda: enrichment_scheduler
        )
        return TestClient(app)

    yield build

    for provider in clients:
        provider.close()


@pytest.fixture
def api_client(make_api_client: Callable[..., TestClient]) -> TestClient:
    return make_api_client()
