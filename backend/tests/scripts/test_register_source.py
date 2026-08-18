"""Splitting one URL across two sources, on purpose.

The demo Source and the production Source point at the same site. That is
the isolation boundary -- signal identity is (source_id, external_id), so
two sources are what stop the production collector's records being
skipped as duplicates of the demo collector's. These tests pin that a
second source over one URL is permitted, that an exact re-run is not, and
that the demo Source survives either way.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, Signal, Source
from app.domain.enums import SignalType, SourceType
from app.integrations.brightdata.fix_my_itch import FIX_MY_ITCH_SOURCE_URL
from scripts.register_source import (
    REGISTERED_ACTIVE,
    describe,
    find_duplicate,
    find_same_url,
    register,
)
from tests.recallguard.conftest import DETECTED_AT, RunBuilder

PRODUCTION_SOURCE_NAME = "Fix My Itch Production"


def snapshot(source: Source) -> dict[str, object]:
    return {
        "id": source.id,
        "name": source.name,
        "source_type": source.source_type,
        "base_url": source.base_url,
        "active": source.active,
    }


def source_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Source)).scalar_one()


# --- the duplicate guard ---------------------------------------------------


def test_an_exact_duplicate_is_refused(db_session: Session, source: Source) -> None:
    """Same name and same URL is a re-run, never a decision."""
    duplicate = find_duplicate(db_session, name=source.name, base_url=source.base_url)

    assert duplicate is not None
    assert duplicate.id == source.id


def test_a_second_source_over_the_same_url_is_not_a_duplicate(
    db_session: Session, source: Source
) -> None:
    """The whole point: one URL, two sources, separate signal identity."""
    assert source.base_url == FIX_MY_ITCH_SOURCE_URL

    duplicate = find_duplicate(
        db_session, name=PRODUCTION_SOURCE_NAME, base_url=FIX_MY_ITCH_SOURCE_URL
    )

    assert duplicate is None


def test_the_same_name_over_a_different_url_is_not_a_duplicate(
    db_session: Session, source: Source
) -> None:
    duplicate = find_duplicate(
        db_session, name=source.name, base_url="https://example.test/other/"
    )

    assert duplicate is None


def test_sources_sharing_a_url_are_reported(
    db_session: Session, source: Source
) -> None:
    found = find_same_url(db_session, base_url=FIX_MY_ITCH_SOURCE_URL)

    assert [existing.id for existing in found] == [source.id]


def test_the_guards_write_nothing(db_session: Session, source: Source) -> None:
    before = snapshot(source)

    find_duplicate(db_session, name=PRODUCTION_SOURCE_NAME, base_url=source.base_url)
    find_same_url(db_session, base_url=source.base_url)

    db_session.expire_all()
    found = db_session.get(Source, source.id)
    assert found is not None
    assert snapshot(found) == before
    assert source_count(db_session) == 1


# --- the insert ------------------------------------------------------------


def test_registering_adds_one_active_source(
    db_session: Session, source: Source
) -> None:
    created = register(
        db_session,
        name=PRODUCTION_SOURCE_NAME,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
    )

    assert created.id != source.id
    assert created.name == PRODUCTION_SOURCE_NAME
    assert created.source_type is SourceType.WEB
    assert created.base_url == FIX_MY_ITCH_SOURCE_URL
    assert created.active is True
    assert REGISTERED_ACTIVE is True
    assert source_count(db_session) == 2


def test_registering_leaves_the_demo_source_untouched(
    db_session: Session, source: Source
) -> None:
    """The demo Source must survive this byte for byte."""
    before = snapshot(source)

    register(
        db_session,
        name=PRODUCTION_SOURCE_NAME,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
    )

    db_session.expire_all()
    found = db_session.get(Source, source.id)
    assert found is not None
    assert snapshot(found) == before


def test_registering_a_source_repoints_no_collector(
    db_session: Session, source: Source, collector: Collector
) -> None:
    """A new Source must not drag existing collectors onto it."""
    before_source_id = collector.source_id

    created = register(
        db_session,
        name=PRODUCTION_SOURCE_NAME,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
    )

    db_session.expire_all()
    found = db_session.get(Collector, collector.id)
    assert found is not None
    assert found.source_id == before_source_id == source.id
    assert found.source_id != created.id
    # The new source starts with no collectors at all.
    attached = db_session.execute(
        select(func.count())
        .select_from(Collector)
        .where(Collector.source_id == created.id)
    ).scalar_one()
    assert attached == 0


def test_the_two_sources_are_independent_signal_namespaces(
    db_session: Session, source: Source, runs: RunBuilder
) -> None:
    """Why the split exists, proved rather than asserted.

    Signals are unique on (source_id, external_id). The same external_id
    is ingested under each source here, which is exactly what stops
    production records being skipped as duplicates of the demo's -- and
    it would raise if the two rows shared a source.
    """
    created = register(
        db_session,
        name=PRODUCTION_SOURCE_NAME,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
    )
    run = runs.succeeded()
    shared_external_id = "fix-my-itch:why-do-freelancers-ghost-projects"

    for owning_source in (source, created):
        db_session.add(
            Signal(
                source_id=owning_source.id,
                collector_run_id=run.id,
                external_id=shared_external_id,
                canonical_url=FIX_MY_ITCH_SOURCE_URL,
                title="Why do freelancers ghost projects?",
                body="A described problem.",
                signal_type=SignalType.PROBLEM,
                observed_at=DETECTED_AT,
            )
        )
    db_session.commit()

    stored = db_session.execute(
        select(Signal).where(Signal.external_id == shared_external_id)
    ).scalars()
    assert {signal.source_id for signal in stored} == {source.id, created.id}


def test_describe_reports_every_field_that_was_decided(
    db_session: Session, source: Source
) -> None:
    created = register(
        db_session,
        name=PRODUCTION_SOURCE_NAME,
        source_type=SourceType.WEB,
        base_url=FIX_MY_ITCH_SOURCE_URL,
    )

    line = describe(created)

    assert str(created.id) in line
    assert PRODUCTION_SOURCE_NAME in line
    assert "web" in line
    assert FIX_MY_ITCH_SOURCE_URL in line
    assert "active=True" in line


def test_a_failed_insert_leaves_no_partial_row(
    db_session: Session, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit failure must not leave a pending source staged."""
    real_commit = db_session.commit

    def boom() -> None:
        raise RuntimeError("commit exploded")

    monkeypatch.setattr(db_session, "commit", boom)
    with pytest.raises(RuntimeError):
        register(
            db_session,
            name=PRODUCTION_SOURCE_NAME,
            source_type=SourceType.WEB,
            base_url=FIX_MY_ITCH_SOURCE_URL,
        )

    monkeypatch.setattr(db_session, "commit", real_commit)
    assert source_count(db_session) == 1
    assert not db_session.new
