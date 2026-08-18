import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import CollectorStatus, RunStatus, SourceType
from app.integrations.brightdata.client import BrightDataClient
from app.integrations.brightdata.fix_my_itch import FIX_MY_ITCH_SOURCE_URL


@pytest.fixture
def brightdata_settings() -> Settings:
    return Settings(
        _env_file=None,
        BRIGHTDATA_API_KEY="test-token-do-not-log",
        BRIGHTDATA_BASE_URL="https://api.brightdata.test",
    )


def make_client(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> BrightDataClient:
    return BrightDataClient(settings=settings, transport=httpx.MockTransport(handler))


# Verified production output of the Fix My Itch collector, committed as a
# fixture so source-contract regressions are caught without a network
# call. Read-only: tests must copy before mutating.
HEALTHY_FIX_MY_ITCH_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "external"
    / "brightdata"
    / "examples"
    / "fix_my_itch_healthy_v1.json"
)


@pytest.fixture(scope="session")
def fix_my_itch_healthy_records() -> list[dict[str, Any]]:
    return json.loads(HEALTHY_FIX_MY_ITCH_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fix_my_itch_record(fix_my_itch_healthy_records: list[dict[str, Any]]) -> dict:
    """One real production record, freshly copied so a test may mutate it."""
    return dict(fix_my_itch_healthy_records[0])


@pytest.fixture
def fix_my_itch_source(db_session: Session) -> Source:
    source = Source(
        name="Fix My Itch",
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
        active=True,
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


@pytest.fixture
def fix_my_itch_collector_run(
    db_session: Session, fix_my_itch_source: Source
) -> CollectorRun:
    collector = Collector(
        source_id=fix_my_itch_source.id,
        provider="brightdata",
        external_collector_id="fix-my-itch-collector",
        name="Fix My Itch collector",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(collector)
    db_session.commit()

    run = CollectorRun(
        collector_id=collector.id,
        external_run_id="fix-my-itch-run-1",
        status=RunStatus.SUCCEEDED,
        record_count=0,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run
