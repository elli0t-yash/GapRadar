"""Fixtures for the opportunity-engine tests.

Signals are persisted the way the ingestion pipeline persists them --
Fix My Itch scores in the untrusted metadata payload, verbatim -- so
these tests read the same shape production reads.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Signal, Source
from app.domain.enums import CollectorStatus, RunStatus, SignalType, SourceType
from app.integrations.brightdata.fix_my_itch import (
    FIX_MY_ITCH_SOURCE,
    FIX_MY_ITCH_SOURCE_URL,
)

OBSERVED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def make_source(session: Session, *, name: str = "Fix My Itch") -> Source:
    source = Source(
        name=name,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
        active=True,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def make_collector(
    session: Session,
    source: Source,
    *,
    name: str = "Fix My Itch collector",
    external_collector_id: str = "c_fix_my_itch",
    status: CollectorStatus = CollectorStatus.ACTIVE,
) -> Collector:
    collector = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id=external_collector_id,
        name=name,
        status=status,
    )
    session.add(collector)
    session.commit()
    session.refresh(collector)
    return collector


def make_run(
    session: Session,
    collector: Collector,
    *,
    external_run_id: str = "j_run",
    record_count: int = 3,
    status: RunStatus = RunStatus.SUCCEEDED,
) -> CollectorRun:
    run = CollectorRun(
        collector_id=collector.id,
        external_run_id=external_run_id,
        status=status,
        started_at=OBSERVED_AT,
        completed_at=OBSERVED_AT,
        record_count=record_count,
        raw_metadata={
            "orchestration": {
                "stage": "completed",
                "fetched_record_count": record_count,
                "valid_record_count": record_count,
                "invalid_record_count": 0,
            }
        },
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def make_signal(
    session: Session,
    source: Source,
    run: CollectorRun,
    *,
    title: str = "Freelancers ghost projects",
    itch_score: float = 80,
    severity_score: float = 8,
    tam_score: float = 7,
    whitespace_score: float = 6,
    frequency_score: float = 5,
    industry: str = "B2B Services",
    signal_type: SignalType = SignalType.PROBLEM,
    metadata: dict[str, Any] | None = None,
) -> Signal:
    signal = Signal(
        source_id=source.id,
        collector_run_id=run.id,
        external_id=str(uuid.uuid4()),
        canonical_url=FIX_MY_ITCH_SOURCE_URL,
        title=title,
        body="A described problem.",
        signal_type=signal_type,
        signal_metadata=metadata
        if metadata is not None
        else {
            "source": FIX_MY_ITCH_SOURCE,
            "source_url": FIX_MY_ITCH_SOURCE_URL,
            "industry": industry,
            "itch_score": itch_score,
            "severity_score": severity_score,
            "tam_score": tam_score,
            "whitespace_score": whitespace_score,
            "frequency_score": frequency_score,
        },
        observed_at=OBSERVED_AT,
    )
    session.add(signal)
    session.commit()
    session.refresh(signal)
    return signal


@pytest.fixture
def source(db_session: Session) -> Source:
    return make_source(db_session)


@pytest.fixture
def collector(db_session: Session, source: Source) -> Collector:
    return make_collector(db_session, source)


@pytest.fixture
def run(db_session: Session, collector: Collector) -> CollectorRun:
    return make_run(db_session, collector)
