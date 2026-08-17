from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import CollectorStatus, RunStatus, SourceType


@pytest.fixture
def source(db_session: Session) -> Source:
    src = Source(
        name="Reddit r/startups",
        source_type=SourceType.FORUM,
        base_url="https://reddit.com/r/startups",
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
        external_collector_id="collector-1",
        name="Reddit collector",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


@pytest.fixture
def collector_run(db_session: Session, collector: Collector) -> CollectorRun:
    run = CollectorRun(
        collector_id=collector.id,
        external_run_id="run-1",
        status=RunStatus.SUCCEEDED,
        record_count=0,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


@pytest.fixture
def other_collector_run(
    db_session: Session, collector: Collector
) -> Iterator[CollectorRun]:
    run = CollectorRun(
        collector_id=collector.id,
        external_run_id="run-2",
        status=RunStatus.SUCCEEDED,
        record_count=0,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    yield run
