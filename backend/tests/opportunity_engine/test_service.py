"""Only signals from a currently healthy collector are ever exposed."""

import uuid

from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident, Source
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    SignalType,
)
from app.opportunity_engine.service import (
    count_signals,
    count_trusted_signals,
    get_opportunity,
    list_opportunities,
)
from tests.opportunity_engine.conftest import (
    OBSERVED_AT,
    make_collector,
    make_run,
    make_signal,
)


def open_incident(
    session: Session,
    collector: Collector,
    *,
    status: IncidentStatus = IncidentStatus.DEGRADED,
) -> ReliabilityIncident:
    incident = ReliabilityIncident(
        collector_id=collector.id,
        status=status,
        classification=FailureClassification.EXTRACTION_DRIFT,
        recommended_action=RecommendedAction.REQUEST_HEAL,
        repair_attempts=0,
        detected_at=OBSERVED_AT,
        evidence={"occurrences": []},
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def test_trusted_signals_are_listed_and_ranked_by_score(
    db_session: Session, source: Source, run: CollectorRun
) -> None:
    make_signal(db_session, source, run, title="low", itch_score=10)
    make_signal(db_session, source, run, title="high", itch_score=100)

    opportunities = list_opportunities(db_session)

    assert [item.title for item in opportunities] == ["high", "low"]
    assert opportunities[0].opportunity_score > opportunities[1].opportunity_score


def test_a_degraded_collector_contributes_no_opportunities(
    db_session: Session, source: Source, collector: Collector, run: CollectorRun
) -> None:
    signal = make_signal(db_session, source, run)
    open_incident(db_session, collector)

    assert list_opportunities(db_session) == []
    assert get_opportunity(db_session, signal_id=signal.id) is None
    assert count_trusted_signals(db_session) == 0
    # The signal still exists; it is simply not trusted right now.
    assert count_signals(db_session) == 1


def test_every_active_incident_status_withholds_the_collector(
    db_session: Session, source: Source, collector: Collector, run: CollectorRun
) -> None:
    """Healing, validating and verifying are not "nearly healthy"."""
    make_signal(db_session, source, run)
    incident = open_incident(db_session, collector)

    for status in (
        IncidentStatus.HEALING,
        IncidentStatus.VALIDATING,
        IncidentStatus.VERIFYING,
        IncidentStatus.MANUAL_REVIEW,
    ):
        incident.status = status
        db_session.commit()
        assert list_opportunities(db_session) == [], status


def test_a_recovered_incident_restores_trust(
    db_session: Session, source: Source, collector: Collector, run: CollectorRun
) -> None:
    make_signal(db_session, source, run)
    incident = open_incident(db_session, collector)
    incident.status = IncidentStatus.RECOVERED
    db_session.commit()

    assert len(list_opportunities(db_session)) == 1
    assert count_trusted_signals(db_session) == 1


def test_one_degraded_collector_does_not_withhold_a_healthy_one(
    db_session: Session, source: Source, collector: Collector, run: CollectorRun
) -> None:
    make_signal(db_session, source, run, title="untrusted")
    open_incident(db_session, collector)

    healthy = make_collector(
        db_session, source, name="second", external_collector_id="c_second"
    )
    healthy_run = make_run(db_session, healthy, external_run_id="j_second")
    make_signal(db_session, source, healthy_run, title="trusted")

    assert [item.title for item in list_opportunities(db_session)] == ["trusted"]


def test_only_problem_signals_are_treated_as_opportunities(
    db_session: Session, source: Source, run: CollectorRun
) -> None:
    make_signal(db_session, source, run, title="problem")
    make_signal(
        db_session, source, run, title="research", signal_type=SignalType.RESEARCH
    )

    assert [item.title for item in list_opportunities(db_session)] == ["problem"]


def test_an_unscorable_signal_is_listed_last_not_given_a_score(
    db_session: Session, source: Source, run: CollectorRun
) -> None:
    make_signal(db_session, source, run, title="scored", itch_score=10)
    make_signal(db_session, source, run, title="unscorable", metadata={})

    opportunities = list_opportunities(db_session)

    assert [item.title for item in opportunities] == ["scored", "unscorable"]
    assert opportunities[-1].opportunity_score is None


def test_the_limit_is_respected(
    db_session: Session, source: Source, run: CollectorRun
) -> None:
    for index in range(5):
        make_signal(db_session, source, run, title=f"p{index}", itch_score=index * 10)

    assert len(list_opportunities(db_session, limit=2)) == 2


def test_an_unknown_signal_id_is_simply_absent(db_session: Session) -> None:
    assert get_opportunity(db_session, signal_id=uuid.uuid4()) is None


def test_the_component_scores_are_passed_through_verbatim(
    db_session: Session, source: Source, run: CollectorRun
) -> None:
    signal = make_signal(db_session, source, run, tam_score=7, itch_score=76)

    opportunity = get_opportunity(db_session, signal_id=signal.id)

    assert opportunity is not None
    assert opportunity.tam_score == 7
    assert opportunity.itch_score == 76
    assert opportunity.industry == "B2B Services"
    assert opportunity.source == "fix_my_itch"
    assert opportunity.problem == opportunity.title
