"""Registering a collector next to one a live demo depends on.

The risk this guards is not a bad INSERT -- it is an INSERT that turns
out to be an UPDATE, or a second row for a collector that is already
registered. Both would land on a table holding the RecallGuard demo
collector, so the tests assert what the script leaves alone as carefully
as what it writes.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, Source
from app.domain.enums import CollectorStatus
from scripts.register_collector import (
    REGISTERED_STATUS,
    find_conflict,
    register,
)

DEMO_EXTERNAL_ID = "c_msya3ha629w2q9c62m"
STABLE_EXTERNAL_ID = "c_mswvtpby29tybc04dr"


@pytest.fixture
def demo_collector(db_session: Session, source: Source) -> Collector:
    """Stands in for the collector the RecallGuard incident is open on."""
    collector = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id=DEMO_EXTERNAL_ID,
        name="recallguard-demo",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(collector)
    db_session.commit()
    db_session.refresh(collector)
    return collector


def snapshot(collector: Collector) -> dict[str, object]:
    return {
        "id": collector.id,
        "source_id": collector.source_id,
        "provider": collector.provider,
        "external_collector_id": collector.external_collector_id,
        "name": collector.name,
        "status": collector.status,
    }


def collector_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Collector)).scalar_one()


# --- the conflict guard ----------------------------------------------------


def test_a_free_external_id_has_no_conflict(
    db_session: Session, demo_collector: Collector
) -> None:
    assert find_conflict(db_session, external_collector_id=STABLE_EXTERNAL_ID) is None


def test_an_already_registered_external_id_conflicts(
    db_session: Session, demo_collector: Collector
) -> None:
    conflict = find_conflict(db_session, external_collector_id=DEMO_EXTERNAL_ID)

    assert conflict is not None
    assert conflict.id == demo_collector.id


def test_the_conflict_check_ignores_the_provider(
    db_session: Session, source: Source, demo_collector: Collector
) -> None:
    """(provider, external_id) is unique in the database; this is stricter.

    A row the database constraint would happily accept -- same external
    id, different provider name -- is still reported as a conflict,
    because that is far more likely a typo than a real collision.
    """
    db_session.add(
        Collector(
            source_id=source.id,
            provider="some-other-provider",
            external_collector_id=STABLE_EXTERNAL_ID,
            name="registered-elsewhere",
            status=CollectorStatus.ACTIVE,
        )
    )
    db_session.commit()

    conflict = find_conflict(db_session, external_collector_id=STABLE_EXTERNAL_ID)

    assert conflict is not None
    assert conflict.provider == "some-other-provider"


def test_the_conflict_check_writes_nothing(
    db_session: Session, demo_collector: Collector
) -> None:
    before = snapshot(demo_collector)

    find_conflict(db_session, external_collector_id=DEMO_EXTERNAL_ID)

    db_session.expire_all()
    found = db_session.get(Collector, demo_collector.id)
    assert found is not None
    assert snapshot(found) == before
    assert collector_count(db_session) == 1


# --- the insert ------------------------------------------------------------


def test_registering_adds_one_active_collector(
    db_session: Session, source: Source, demo_collector: Collector
) -> None:
    collector = register(
        db_session,
        source=source,
        provider="brightdata",
        external_collector_id=STABLE_EXTERNAL_ID,
        name="gapradar-fix-my-itch",
    )

    assert collector.source_id == source.id
    assert collector.provider == "brightdata"
    assert collector.external_collector_id == STABLE_EXTERNAL_ID
    assert collector.name == "gapradar-fix-my-itch"
    assert collector.status is CollectorStatus.ACTIVE
    assert REGISTERED_STATUS is CollectorStatus.ACTIVE
    assert collector_count(db_session) == 2


def test_registering_leaves_every_existing_collector_untouched(
    db_session: Session, source: Source, demo_collector: Collector
) -> None:
    """The demo collector must survive this byte for byte."""
    before = snapshot(demo_collector)

    register(
        db_session,
        source=source,
        provider="brightdata",
        external_collector_id=STABLE_EXTERNAL_ID,
        name="gapradar-fix-my-itch",
    )

    db_session.expire_all()
    found = db_session.get(Collector, demo_collector.id)
    assert found is not None
    assert snapshot(found) == before


def test_both_collectors_share_the_one_source(
    db_session: Session, source: Source, demo_collector: Collector
) -> None:
    """Two collectors, one Source row -- no second source is created."""
    register(
        db_session,
        source=source,
        provider="brightdata",
        external_collector_id=STABLE_EXTERNAL_ID,
        name="gapradar-fix-my-itch",
    )

    sources = db_session.execute(select(func.count()).select_from(Source)).scalar_one()
    assert sources == 1
    registered = list(
        db_session.execute(
            select(Collector).where(Collector.source_id == source.id)
        ).scalars()
    )
    assert len(registered) == 2


def test_a_failed_insert_leaves_no_partial_row(
    db_session: Session, source: Source, demo_collector: Collector
) -> None:
    """The database constraint is the backstop behind the guard.

    find_conflict is what callers use; this proves that bypassing it
    cannot leave a half-written table behind.
    """
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError via SQLAlchemy
        register(
            db_session,
            source=source,
            provider="brightdata",
            external_collector_id=DEMO_EXTERNAL_ID,
            name="duplicate-attempt",
        )

    assert collector_count(db_session) == 1
    found = db_session.get(Collector, demo_collector.id)
    assert found is not None
    assert found.name == "recallguard-demo"
