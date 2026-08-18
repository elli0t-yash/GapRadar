import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Collector, Source
from app.domain.enums import CollectorStatus, SourceType
from app.integrations.brightdata.fix_my_itch import FIX_MY_ITCH_SOURCE_URL
from tests.integrations.brightdata.conftest import HEALTHY_FIX_MY_ITCH_FIXTURE

COLLECTION_ID = "j_test_collection"
BUILDING = {"status": "building"}


class FakeClock:
    """Deterministic clock whose only way to advance is sleeping.

    Injected as `now`/`sleep` so the polling loop's local timeout is
    exercised without any real time passing.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current += timedelta(seconds=seconds)


class ScriptedBrightData:
    """httpx.MockTransport handler scripting one Bright Data collection.

    GET /dca/dataset serves both polling and the final fetch, exactly as
    the real API does, so responses are consumed in order and the last
    one repeats once the script runs out.
    """

    def __init__(
        self,
        *,
        get_responses: list[httpx.Response],
        trigger_response: httpx.Response | None = None,
    ) -> None:
        self.get_responses = get_responses
        self.trigger_response = trigger_response or httpx.Response(
            200, json={"collection_id": COLLECTION_ID}
        )
        self.requests: list[httpx.Request] = []
        self.get_count = 0

    @property
    def trigger_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/dca/trigger"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/dca/trigger":
            return self.trigger_response
        if request.url.path == "/dca/dataset":
            index = min(self.get_count, len(self.get_responses) - 1)
            self.get_count += 1
            return self.get_responses[index]
        raise AssertionError(f"unexpected request path: {request.url.path}")


def building_then(dataset: Any, *, building_polls: int = 0) -> list[httpx.Response]:
    return [
        *[httpx.Response(200, json=BUILDING) for _ in range(building_polls)],
        httpx.Response(200, json=dataset),
    ]


@pytest.fixture
def brightdata_settings() -> Settings:
    return Settings(
        _env_file=None,
        BRIGHTDATA_API_KEY="test-token-do-not-log",
        BRIGHTDATA_BASE_URL="https://api.brightdata.test",
    )


@pytest.fixture(scope="session")
def healthy_records() -> list[dict[str, Any]]:
    return json.loads(HEALTHY_FIX_MY_ITCH_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def dataset(healthy_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A small, valid production dataset.

    Deliberately not the whole fixture: production record counts are
    dynamic, so nothing in these tests may depend on a particular size.
    """
    return [dict(record) for record in healthy_records[:3]]


@pytest.fixture
def source(db_session: Session) -> Source:
    src = Source(
        name="Fix My Itch",
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
        active=True,
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture
def collector(db_session: Session, source: Source) -> Collector:
    col = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id="c_fix_my_itch",
        name="Fix My Itch collector",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col
