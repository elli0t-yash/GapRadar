import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Collector,
    CollectorRun,
    CollectorStatus,
    RunStatus,
    Signal,
    SignalType,
    Source,
    SourceType,
)


def make_source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "name": "Reddit r/startups",
        "source_type": SourceType.FORUM,
        "base_url": "https://reddit.com/r/startups",
        "active": True,
    }
    defaults.update(overrides)
    return Source(**defaults)


def make_collector(source: Source, **overrides: object) -> Collector:
    defaults: dict[str, object] = {
        "source": source,
        "provider": "brightdata",
        "external_collector_id": "collector-123",
        "name": "Reddit collector",
        "status": CollectorStatus.ACTIVE,
    }
    defaults.update(overrides)
    return Collector(**defaults)


def make_collector_run(collector: Collector, **overrides: object) -> CollectorRun:
    defaults: dict[str, object] = {
        "collector": collector,
        "external_run_id": "run-1",
        "status": RunStatus.SUCCEEDED,
        "record_count": 0,
    }
    defaults.update(overrides)
    return CollectorRun(**defaults)


def make_signal(
    source: Source, collector_run: CollectorRun, **overrides: object
) -> Signal:
    defaults: dict[str, object] = {
        "source": source,
        "collector_run": collector_run,
        "external_id": "post-1",
        "canonical_url": "https://reddit.com/r/startups/post-1",
        "title": "I wish there was a tool for X",
        "body": "Full complaint text goes here.",
        "signal_type": SignalType.COMPLAINT,
        "observed_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Signal(**defaults)


def test_source_created_with_uuid_and_timestamps(db_session: Session) -> None:
    source = make_source()
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    assert isinstance(source.id, uuid.UUID)
    assert source.created_at is not None
    assert source.updated_at is not None
    assert source.active is True


def test_source_collector_signal_relationships(db_session: Session) -> None:
    source = make_source()
    collector = make_collector(source)
    run = make_collector_run(collector)
    signal = make_signal(source, run)

    db_session.add_all([source, collector, run, signal])
    db_session.commit()

    db_session.refresh(source)
    db_session.refresh(collector)

    assert collector in source.collectors
    assert signal in source.signals
    assert run in collector.runs
    assert signal in run.signals
    assert signal.source_id == source.id
    assert signal.collector_run_id == run.id


def test_enum_values_round_trip(db_session: Session) -> None:
    source = make_source(source_type=SourceType.SOCIAL)
    db_session.add(source)
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(Source, source.id)
    assert reloaded is not None
    assert reloaded.source_type is SourceType.SOCIAL
    assert isinstance(reloaded.source_type, SourceType)


def test_collector_requires_source(db_session: Session) -> None:
    collector = Collector(
        provider="brightdata",
        external_collector_id="collector-x",
        name="Orphan collector",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(collector)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_collector_external_id_unique_per_provider(db_session: Session) -> None:
    source = make_source()
    db_session.add(source)
    db_session.commit()

    first = make_collector(source, external_collector_id="dup-id")
    db_session.add(first)
    db_session.commit()

    second = make_collector(
        source, name="Second collector", external_collector_id="dup-id"
    )
    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_collector_external_id_reusable_across_providers(db_session: Session) -> None:
    source = make_source()
    db_session.add(source)
    db_session.commit()

    first = make_collector(
        source, provider="brightdata", external_collector_id="shared-id"
    )
    second = make_collector(
        source, provider="other-provider", external_collector_id="shared-id"
    )
    db_session.add_all([first, second])

    # Same external_collector_id under different providers is allowed because
    # the uniqueness constraint is scoped to (provider, external_collector_id),
    # not globally unique.
    db_session.commit()


def test_signal_external_id_unique_per_source(db_session: Session) -> None:
    source = make_source()
    collector = make_collector(source)
    run = make_collector_run(collector)
    db_session.add_all([source, collector, run])
    db_session.commit()

    first = make_signal(source, run, external_id="dup-signal")
    db_session.add(first)
    db_session.commit()

    second = make_signal(
        source, run, external_id="dup-signal", canonical_url="https://example.com/other"
    )
    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_signal_metadata_json_round_trips(db_session: Session) -> None:
    source = make_source()
    collector = make_collector(source)
    run = make_collector_run(collector)
    signal = make_signal(
        source, run, signal_metadata={"raw_score": 0.87, "tags": ["urgent"]}
    )
    db_session.add_all([source, collector, run, signal])
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(Signal, signal.id)
    assert reloaded is not None
    assert reloaded.signal_metadata == {"raw_score": 0.87, "tags": ["urgent"]}


def test_updated_at_changes_on_update(db_session: Session) -> None:
    source = make_source()
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    original_updated_at = source.updated_at

    source.name = "Renamed source"
    db_session.commit()
    db_session.refresh(source)

    assert source.updated_at >= original_updated_at


@pytest.mark.parametrize("signal_type", list(SignalType))
def test_signal_type_round_trips_for_every_taxonomy_value(
    db_session: Session, signal_type: SignalType
) -> None:
    # signals.signal_type is a plain VARCHAR(32) holding the enum member
    # name, so every value -- pre-existing and newly added -- must store
    # and reload unchanged.
    source = make_source()
    collector_run = make_collector_run(make_collector(source))
    signal = make_signal(source, collector_run, signal_type=signal_type)
    db_session.add(signal)
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(Signal, signal.id)
    assert reloaded is not None
    assert reloaded.signal_type is signal_type
