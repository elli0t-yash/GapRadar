"""Builders for RecallGuard tests.

Runs are built to look exactly like what app.collection writes, so these
tests exercise the real evidence contract rather than an invented one.
Failure cases are small and synthetic: copying the 133-row production
payload into every scenario would obscure what each test is about.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import CollectorStatus, RunStatus, SourceType
from app.integrations.brightdata.fix_my_itch import FIX_MY_ITCH_SOURCE_URL
from app.recallguard.schemas import BaselineProfile, profile_from_records
from tests.integrations.brightdata.conftest import HEALTHY_FIX_MY_ITCH_FIXTURE

DETECTED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeClock:
    """Monotonic clock; each read advances by a second so ordering is
    deterministic without any real waiting."""

    def __init__(self, start: datetime = DETECTED_AT) -> None:
        self.current = start

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


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


@pytest.fixture(scope="session")
def healthy_baseline(healthy_records: list[dict[str, Any]]) -> BaselineProfile:
    """Baseline captured from the verified healthy production payload.

    Derived, never hardcoded: the numbers are observations of that
    capture, not a contract the source is required to keep meeting.
    """
    return profile_from_records(healthy_records, label="fix_my_itch_healthy_v1")


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


@pytest.fixture
def other_collector(db_session: Session, source: Source) -> Collector:
    col = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id="c_other",
        name="Another collector",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


class RunBuilder:
    """Persists CollectorRun rows carrying orchestration evidence."""

    def __init__(self, session: Session, collector: Collector) -> None:
        self.session = session
        self.collector = collector
        self.sequence = 0

    def _add(
        self,
        *,
        status: RunStatus,
        orchestration: dict[str, Any],
        record_count: int,
        started_at: datetime,
    ) -> CollectorRun:
        self.sequence += 1
        run = CollectorRun(
            collector_id=self.collector.id,
            external_run_id=f"j_run_{self.sequence}",
            status=status,
            started_at=started_at,
            completed_at=started_at + timedelta(minutes=1),
            record_count=record_count,
            raw_metadata={
                "provider": {"collection_id": f"j_run_{self.sequence}"},
                "orchestration": orchestration,
            },
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def succeeded(
        self,
        *,
        record_count: int = 133,
        accepted: int | None = None,
        started_at: datetime = DETECTED_AT,
    ) -> CollectorRun:
        """A run exactly like a completed production collection."""
        return self._add(
            status=RunStatus.SUCCEEDED,
            record_count=record_count,
            started_at=started_at,
            orchestration={
                "stage": "completed",
                "fetched_record_count": record_count,
                "valid_record_count": record_count,
                "invalid_record_count": 0,
                "source_duplicate_count": 0,
                "ingestion": {
                    "accepted": record_count if accepted is None else accepted,
                    "duplicates": 0,
                },
            },
        )

    def failed(
        self,
        stage: str,
        *,
        extra: dict[str, Any] | None = None,
        started_at: datetime = DETECTED_AT,
    ) -> CollectorRun:
        return self._add(
            status=RunStatus.FAILED,
            record_count=0,
            started_at=started_at,
            orchestration={
                "stage": stage,
                "error": "TestFailure",
                "message": f"synthetic {stage} failure",
                **(extra or {}),
            },
        )

    def source_validation_failed(
        self,
        *,
        invalid_records: list[dict[str, Any]],
        fetched: int = 3,
        started_at: datetime = DETECTED_AT,
    ) -> CollectorRun:
        return self.failed(
            "source_validation",
            started_at=started_at,
            extra={
                "fetched_record_count": fetched,
                "valid_record_count": fetched - len(invalid_records),
                "invalid_record_count": len(invalid_records),
                "source_duplicate_count": 0,
                "invalid_records": invalid_records,
            },
        )


@pytest.fixture
def runs(db_session: Session, collector: Collector) -> RunBuilder:
    return RunBuilder(db_session, collector)


def invalid_record(
    *, index: int = 0, reason: str = "invalid_score", **raw_overrides: Any
) -> dict[str, Any]:
    """One entry as the Fix My Itch validator preserves it on a run."""
    raw: dict[str, Any] = {
        "problem": "Why do freelancers ghost projects?",
        "itch_score": 76,
        "industry": "B2B Services",
        "description": "A described problem.",
        "severity_score": 8,
        "tam_score": 7,
        "whitespace_score": 7.5,
        "frequency_score": 7,
        "source": "fix_my_itch",
        "source_url": FIX_MY_ITCH_SOURCE_URL,
    }
    raw.update(raw_overrides)
    return {
        "index": index,
        "reason": reason,
        "detail": f"{reason} at index {index}",
        "raw": raw,
    }
