"""Claiming, resuming and finishing one logical pipeline execution.

The properties under test here are the ones that make an asynchronous
pipeline safe rather than merely fast:

- one logical execution triggers at most one Bright Data collection, no
  matter how many times it is resumed;
- GapRadar running out of patience is not evidence that Bright Data
  failed, and never becomes an incident;
- a refresh that is in flight, or that fails, never disturbs data that
  was already trusted.

Every provider call goes through an httpx.MockTransport. The trigger
count is asserted directly, because "did we scrape twice" is the question
the whole design exists to answer.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collection.service import start_fix_my_itch_collection
from app.config import Settings
from app.db.models import Collector, CollectorRun, PipelineRun, Source
from app.domain.enums import PipelineRunStatus, RunStatus
from app.integrations.brightdata.client import BrightDataClient
from app.opportunity_engine.service import list_opportunities
from app.pipeline.executor import (
    active_pipeline_run,
    drive_pipeline_run,
    resume_pipeline_run,
    resume_unfinished_pipeline_runs,
    start_pipeline_run,
)
from tests.recallguard.healing_fakes import ScriptedProvider, done

BUILDING = {"status": "building"}


class CountingProvider(ScriptedProvider):
    """Scripted provider that counts collection triggers and can stall.

    `building_polls` makes the collection report itself still running for
    that many status requests before the dataset appears, which is how a
    genuinely slow scrape is reproduced without any real waiting.
    """

    def __init__(
        self,
        *,
        dataset: list[dict[str, Any]],
        building_polls: int = 0,
    ) -> None:
        super().__init__(progress=[done()], dataset=dataset)
        self.building_polls = building_polls
        self.trigger_count = 0
        self.status_polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dca/trigger":
            self.requests.append(request)
            self.trigger_count += 1
            return httpx.Response(
                200, json={"collection_id": f"j_counting_{self.trigger_count}"}
            )
        if request.url.path == "/dca/dataset":
            self.requests.append(request)
            self.status_polls += 1
            if self.status_polls <= self.building_polls:
                return httpx.Response(200, json=BUILDING)
            return httpx.Response(200, json=self.dataset)
        return super().__call__(request)


@pytest.fixture
def provider_settings() -> Settings:
    return Settings(
        _env_file=None,
        BRIGHTDATA_API_KEY="test-token-do-not-log",
        BRIGHTDATA_BASE_URL="https://api.brightdata.test",
    )


def client_for(provider: ScriptedProvider, settings: Settings) -> BrightDataClient:
    return BrightDataClient(settings=settings, transport=httpx.MockTransport(provider))


def no_sleep(_seconds: float) -> None:
    return None


# -- claiming ---------------------------------------------------------------


def test_claiming_writes_a_queued_execution_and_calls_no_provider(
    db_session: Session, collector: Collector
) -> None:
    run, already_running = start_pipeline_run(db_session, collector=collector)

    assert already_running is False
    assert run.status is PipelineRunStatus.QUEUED
    assert run.provider_job_id is None
    assert run.trusted is None
    assert db_session.get(PipelineRun, run.id) is not None


def test_a_collector_with_an_active_execution_is_not_claimed_twice(
    db_session: Session, collector: Collector
) -> None:
    first, _ = start_pipeline_run(db_session, collector=collector)
    second, already_running = start_pipeline_run(db_session, collector=collector)

    assert already_running is True
    assert second.id == first.id
    assert db_session.query(PipelineRun).count() == 1


@pytest.mark.parametrize(
    "status",
    [
        PipelineRunStatus.QUEUED,
        PipelineRunStatus.COLLECTING,
        PipelineRunStatus.WAITING_PROVIDER,
        PipelineRunStatus.VALIDATING,
        PipelineRunStatus.INGESTING,
        PipelineRunStatus.VERIFYING,
    ],
)
def test_every_active_state_blocks_a_second_claim(
    db_session: Session, collector: Collector, status: PipelineRunStatus
) -> None:
    """Any state short of a verdict means work is already in flight."""
    existing, _ = start_pipeline_run(db_session, collector=collector)
    existing.status = status
    db_session.commit()

    joined, already_running = start_pipeline_run(db_session, collector=collector)

    assert already_running is True
    assert joined.id == existing.id


@pytest.mark.parametrize(
    "status",
    [
        PipelineRunStatus.COMPLETED,
        PipelineRunStatus.DEGRADED,
        PipelineRunStatus.FAILED,
    ],
)
def test_a_terminal_execution_does_not_block_a_new_one(
    db_session: Session, collector: Collector, status: PipelineRunStatus
) -> None:
    finished, _ = start_pipeline_run(db_session, collector=collector)
    finished.status = status
    db_session.commit()

    fresh, already_running = start_pipeline_run(db_session, collector=collector)

    assert already_running is False
    assert fresh.id != finished.id


def test_an_execution_key_makes_a_repeated_request_a_no_op(
    db_session: Session, collector: Collector
) -> None:
    """What a daily scheduler will rely on.

    The key resolves to the same execution even once that execution is
    finished, so a second invocation in the same window does not scrape
    again. This is the mechanism; choosing the window is the scheduler's
    job and is deliberately not wired up here.
    """
    first, _ = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-18"
    )
    first.status = PipelineRunStatus.COMPLETED
    first.trusted = True
    db_session.commit()

    again, already_running = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-18"
    )

    assert already_running is True
    assert again.id == first.id
    assert db_session.query(PipelineRun).count() == 1


def test_a_different_window_claims_a_new_execution(
    db_session: Session, collector: Collector
) -> None:
    first, _ = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-18"
    )
    first.status = PipelineRunStatus.COMPLETED
    db_session.commit()

    tomorrow, already_running = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-19"
    )

    assert already_running is False
    assert tomorrow.id != first.id


# -- one execution, at most one provider job -------------------------------


def test_a_full_cycle_triggers_exactly_one_collection(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    provider = CountingProvider(dataset=good_records)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        finished = drive_pipeline_run(
            db_session, client, pipeline_run_id=run.id, sleep=no_sleep
        )

    assert finished.status is PipelineRunStatus.COMPLETED
    assert finished.trusted is True
    assert provider.trigger_count == 1
    assert finished.provider_job_id == "j_counting_1"


def test_resuming_a_waiting_execution_repolls_and_never_retriggers(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """The invariant, stated as directly as it can be.

    The execution is left waiting on a provider job that is still
    running, then resumed three times. Every resume must re-poll THAT
    job; not one of them may start a second collection.
    """
    provider = CountingProvider(dataset=good_records, building_polls=3)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        # First step triggers and records the provider job.
        resume_pipeline_run(db_session, client, pipeline_run_id=run.id)
        assert run.status is PipelineRunStatus.WAITING_PROVIDER
        assert run.provider_job_id == "j_counting_1"
        assert provider.trigger_count == 1

        # Each further resume polls the same job while it is building.
        for _ in range(3):
            resumed = resume_pipeline_run(db_session, client, pipeline_run_id=run.id)
            assert resumed.status is PipelineRunStatus.WAITING_PROVIDER
            assert resumed.provider_job_id == "j_counting_1"
            assert provider.trigger_count == 1

        # The dataset appears; the same execution finishes on that job.
        finished = resume_pipeline_run(db_session, client, pipeline_run_id=run.id)

    assert finished.status is PipelineRunStatus.COMPLETED
    assert provider.trigger_count == 1
    assert db_session.query(CollectorRun).count() == 1


def test_a_run_whose_collection_already_finished_is_not_recollected(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """A crash after the collection succeeded resumes into RecallGuard.

    The collector run is already SUCCEEDED, so resuming must go straight
    to evaluation rather than asking the provider for the dataset again.
    """
    provider = CountingProvider(dataset=good_records)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        resume_pipeline_run(db_session, client, pipeline_run_id=run.id)
        # Finish the collection, then rewind the execution to look like a
        # process that died between finishing and evaluating.
        resume_pipeline_run(db_session, client, pipeline_run_id=run.id)
        assert run.status is PipelineRunStatus.COMPLETED
        collector_run = db_session.get(CollectorRun, run.collector_run_id)
        assert collector_run is not None
        assert collector_run.status is RunStatus.SUCCEEDED

        polls_before = provider.status_polls
        run.status = PipelineRunStatus.VERIFYING
        run.completed_at = None
        db_session.commit()

        finished = resume_pipeline_run(db_session, client, pipeline_run_id=run.id)

    assert finished.status is PipelineRunStatus.COMPLETED
    assert provider.trigger_count == 1
    # No further provider traffic: the dataset was not re-fetched.
    assert provider.status_polls == polls_before


def test_a_terminal_execution_resumes_to_a_no_op(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    provider = CountingProvider(dataset=good_records)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        drive_pipeline_run(db_session, client, pipeline_run_id=run.id, sleep=no_sleep)
        triggers = provider.trigger_count
        requests = len(provider.requests)

        again = resume_pipeline_run(db_session, client, pipeline_run_id=run.id)

    assert again.status is PipelineRunStatus.COMPLETED
    assert provider.trigger_count == triggers
    assert len(provider.requests) == requests


def test_an_execution_interrupted_before_the_job_was_recorded_fails_closed(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """The one genuinely ambiguous crash, and why it must not retrigger.

    COLLECTING with no provider job id means the process died around the
    trigger. "It never landed" and "it landed and we lost the id" look
    identical from here, so resuming refuses to trigger rather than risk
    a second collection running against the same source.
    """
    provider = CountingProvider(dataset=good_records)
    run, _ = start_pipeline_run(db_session, collector=collector)
    run.status = PipelineRunStatus.COLLECTING
    db_session.commit()

    with client_for(provider, provider_settings) as client:
        resumed = resume_pipeline_run(db_session, client, pipeline_run_id=run.id)

    assert resumed.status is PipelineRunStatus.FAILED
    assert provider.trigger_count == 0
    assert resumed.error is not None
    assert "not retriggering" in resumed.error
    # An execution that could not be carried out reached no verdict.
    assert resumed.trusted is None


def test_resuming_an_unknown_execution_raises(
    db_session: Session, provider_settings: Settings
) -> None:
    provider = CountingProvider(dataset=[])
    with (
        client_for(provider, provider_settings) as client,
        pytest.raises(LookupError),
    ):
        resume_pipeline_run(db_session, client, pipeline_run_id=uuid.uuid4())


# -- local patience is not provider failure --------------------------------


def test_running_out_of_local_patience_leaves_the_execution_resumable(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """GapRadar stopped waiting. Bright Data did not fail.

    The execution must stay WAITING_PROVIDER with its job id intact, with
    no error, no incident and no trust verdict -- and a later resume must
    finish it on the same collection rather than starting another.
    """
    provider = CountingProvider(dataset=good_records, building_polls=50)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        stalled = drive_pipeline_run(
            db_session,
            client,
            pipeline_run_id=run.id,
            timeout_seconds=0.0,
            sleep=no_sleep,
        )

        assert stalled.status is PipelineRunStatus.WAITING_PROVIDER
        assert stalled.provider_job_id == "j_counting_1"
        assert stalled.error is None
        assert stalled.trusted is None
        assert stalled.completed_at is None
        # Not a failure, so RecallGuard was never asked and no incident
        # exists to blame the provider for our own impatience.
        assert stalled.incident_id is None
        assert stalled.reliability_state is None

        provider.building_polls = 0
        finished = drive_pipeline_run(
            db_session, client, pipeline_run_id=run.id, sleep=no_sleep
        )

    assert finished.status is PipelineRunStatus.COMPLETED
    assert finished.trusted is True
    assert provider.trigger_count == 1


def test_a_provider_failure_is_a_verdict_not_a_pause(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
) -> None:
    """A provider error terminates the execution; a local timeout does not."""

    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dca/trigger":
            return httpx.Response(200, json={"collection_id": "j_failing"})
        return httpx.Response(500, json={"error": "provider exploded"})

    run, _ = start_pipeline_run(db_session, collector=collector)

    with BrightDataClient(
        settings=provider_settings, transport=httpx.MockTransport(failing)
    ) as client:
        finished = drive_pipeline_run(
            db_session, client, pipeline_run_id=run.id, sleep=no_sleep
        )

    assert finished.status is PipelineRunStatus.DEGRADED
    assert finished.trusted is False
    # RecallGuard judged it: an outage is a real incident, unlike a local
    # timeout, which never reaches RecallGuard at all.
    assert finished.incident_id is not None


# -- restart recovery -------------------------------------------------------


def test_unfinished_executions_are_picked_up_without_retriggering(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """What the daily job does for work a dead process abandoned.

    The in-process background task does not survive a restart, but the
    execution row and its provider job id do -- so the resume pass must
    rejoin that collection, not start a second one.
    """
    provider = CountingProvider(dataset=good_records, building_polls=1)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        # Simulate the process dying right after the trigger landed.
        resume_pipeline_run(db_session, client, pipeline_run_id=run.id)
        assert run.status is PipelineRunStatus.WAITING_PROVIDER
        assert provider.trigger_count == 1

        recovered = resume_unfinished_pipeline_runs(db_session, client, sleep=no_sleep)

    assert [item.id for item in recovered] == [run.id]
    assert recovered[0].status is PipelineRunStatus.COMPLETED
    assert provider.trigger_count == 1


def test_the_resume_pass_ignores_finished_executions(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    provider = CountingProvider(dataset=good_records)
    run, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        drive_pipeline_run(db_session, client, pipeline_run_id=run.id, sleep=no_sleep)
        assert active_pipeline_run(db_session, collector_id=collector.id) is None

        recovered = resume_unfinished_pipeline_runs(db_session, client, sleep=no_sleep)

    assert recovered == []
    assert provider.trigger_count == 1


# -- trusted data survives a refresh ---------------------------------------


def test_a_refresh_in_flight_keeps_serving_previously_trusted_data(
    db_session: Session,
    source: Source,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """Starting a refresh must not empty the product."""
    provider = CountingProvider(dataset=good_records)
    first, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        drive_pipeline_run(db_session, client, pipeline_run_id=first.id, sleep=no_sleep)
        trusted_before = list_opportunities(db_session)
        assert len(trusted_before) == len(good_records)

        # A second refresh, deliberately left mid-flight.
        provider.building_polls = 50
        second, _ = start_pipeline_run(db_session, collector=collector)
        resume_pipeline_run(db_session, client, pipeline_run_id=second.id)
        assert second.status is PipelineRunStatus.WAITING_PROVIDER

        during = list_opportunities(db_session)

    assert [item.id for item in during] == [item.id for item in trusted_before]


def test_a_failed_refresh_does_not_poison_previously_trusted_data(
    db_session: Session,
    source: Source,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
    drifted_records: list[dict[str, Any]],
) -> None:
    """A refresh that returns garbage withholds itself, not the old data.

    The drifted dataset violates the source contract, so not one of its
    records is ingested. The signals from the previous good run are still
    persisted -- what changes is that RecallGuard opens an incident, and
    the trust filter then withholds the collector's data on purpose.
    Nothing is deleted, and the previous run's signals come straight back
    when the incident closes.
    """
    provider = CountingProvider(dataset=good_records)
    first, _ = start_pipeline_run(db_session, collector=collector)

    with client_for(provider, provider_settings) as client:
        drive_pipeline_run(db_session, client, pipeline_run_id=first.id, sleep=no_sleep)
        trusted_before = [item.id for item in list_opportunities(db_session)]
        assert trusted_before

        provider.dataset = drifted_records
        provider.repair_in_flight = False
        second, _ = start_pipeline_run(db_session, collector=collector)
        failed = drive_pipeline_run(
            db_session, client, pipeline_run_id=second.id, sleep=no_sleep
        )

    assert failed.status is PipelineRunStatus.DEGRADED
    assert failed.trusted is False
    # The earlier signals were never deleted or overwritten.
    surviving = (
        db_session.query(CollectorRun).filter_by(status=RunStatus.SUCCEEDED).count()
    )
    assert surviving == 1


def test_a_second_collector_is_unaffected_by_another_s_execution(
    db_session: Session,
    source: Source,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """Production and the RecallGuard demo collector claim independently.

    Deduplication is per collector, so an execution in flight for one
    never blocks or joins the other.
    """
    demo = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id="c_demo",
        name="recallguard-demo",
        status=collector.status,
    )
    db_session.add(demo)
    db_session.commit()

    production_run, _ = start_pipeline_run(db_session, collector=collector)
    demo_run, already_running = start_pipeline_run(db_session, collector=demo)

    assert already_running is False
    assert demo_run.id != production_run.id
    assert active_pipeline_run(db_session, collector_id=collector.id) is not None
    assert active_pipeline_run(db_session, collector_id=demo.id) is not None


# -- the collection layer's own guarantee ----------------------------------


def test_starting_a_collection_persists_the_job_id_before_any_waiting(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """The anchor everything else resumes from.

    start_fix_my_itch_collection commits the run with its provider job id
    and returns without polling, so an in-flight collection is durable
    the instant it exists.
    """
    provider = CountingProvider(dataset=good_records, building_polls=99)

    with client_for(provider, provider_settings) as client:
        run = start_fix_my_itch_collection(db_session, client, collector=collector)

    assert run.status is RunStatus.RUNNING
    assert run.external_run_id == "j_counting_1"
    assert provider.trigger_count == 1
    # No status request was made: this function does not wait.
    assert provider.status_polls == 0


# -- database-enforced deduplication ---------------------------------------


def test_the_database_refuses_a_second_active_run_for_one_collector(
    db_session: Session, collector: Collector
) -> None:
    """The guarantee itself, asserted against the database rather than the service.

    Bypasses start_pipeline_run entirely: the point is that even code
    that never consults the service cannot put two active executions on
    one collector.
    """
    db_session.add(
        PipelineRun(collector_id=collector.id, status=PipelineRunStatus.QUEUED)
    )
    db_session.commit()

    db_session.add(
        PipelineRun(
            collector_id=collector.id, status=PipelineRunStatus.WAITING_PROVIDER
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize(
    "terminal",
    [
        PipelineRunStatus.COMPLETED,
        PipelineRunStatus.DEGRADED,
        PipelineRunStatus.FAILED,
    ],
)
def test_terminal_runs_are_outside_the_constraint(
    db_session: Session, collector: Collector, terminal: PipelineRunStatus
) -> None:
    """The index is partial, so run history is unbounded.

    Any number of finished executions may coexist with one active
    execution; only the active ones are constrained.
    """
    for _ in range(3):
        db_session.add(PipelineRun(collector_id=collector.id, status=terminal))
    db_session.add(
        PipelineRun(collector_id=collector.id, status=PipelineRunStatus.QUEUED)
    )
    db_session.commit()

    assert db_session.query(PipelineRun).count() == 4


def test_two_collectors_may_each_have_an_active_run(
    db_session: Session, source: Source, collector: Collector
) -> None:
    """The constraint is per collector, so the demo collector is unaffected."""
    demo = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id="c_demo_constraint",
        name="recallguard-demo",
        status=collector.status,
    )
    db_session.add(demo)
    db_session.commit()

    db_session.add(
        PipelineRun(collector_id=collector.id, status=PipelineRunStatus.QUEUED)
    )
    db_session.add(PipelineRun(collector_id=demo.id, status=PipelineRunStatus.QUEUED))
    db_session.commit()

    assert db_session.query(PipelineRun).count() == 2


def blind_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pre-INSERT lookup miss exactly once.

    That single miss IS the race: two transactions both look, both see
    nothing, and both go on to insert. Every later call -- including the
    recovery read after the unique index rejects the loser -- behaves
    normally, which is what a real losing transaction sees once it
    rolls back and re-reads.
    """
    from app.pipeline import executor

    real = executor._existing_claim
    calls = {"n": 0}

    def blinded(session: Session, **kwargs: Any) -> PipelineRun | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(session, **kwargs)

    monkeypatch.setattr(executor, "_existing_claim", blinded)


def test_a_lost_claim_race_joins_the_winner_instead_of_claiming(
    db_session: Session,
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interleaving the index exists for, reproduced deterministically.

    Both requests look for an active run and both find nothing. The
    second INSERT then loses to the partial unique index, and the service
    must recover by returning the winner rather than raising or creating
    a competing execution.
    """
    winner, already_running = start_pipeline_run(db_session, collector=collector)
    assert already_running is False

    blind_once(monkeypatch)
    loser, already_running = start_pipeline_run(db_session, collector=collector)

    assert already_running is True
    assert loser.id == winner.id
    # The losing INSERT was rolled back, so no orphan row survives.
    assert db_session.query(PipelineRun).count() == 1
    # And the lost claim never reached a provider: start_pipeline_run
    # makes no provider call on any path, so a lost race cannot scrape.
    assert loser.provider_job_id is None
    assert loser.status is PipelineRunStatus.QUEUED


def test_a_lost_race_on_the_execution_key_joins_the_winner(
    db_session: Session,
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double-fired cron resolves to one execution, not two.

    The winner here is already terminal, so the active-collector index is
    not what fires -- the idempotency key's own unique index is. Recovery
    has to cope with either, because either can be the constraint that
    loses the race.
    """
    winner, _ = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-18"
    )
    winner.status = PipelineRunStatus.COMPLETED
    winner.trusted = True
    db_session.commit()

    blind_once(monkeypatch)
    loser, already_running = start_pipeline_run(
        db_session, collector=collector, idempotency_key="daily:2026-08-18"
    )

    assert already_running is True
    assert loser.id == winner.id
    assert loser.status is PipelineRunStatus.COMPLETED
    assert db_session.query(PipelineRun).count() == 1


def test_a_genuinely_unexplained_integrity_error_is_not_swallowed(
    db_session: Session,
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constraint failure with no matching claim must surface.

    Recovery returns the winner only when there IS one. If the lookup
    still finds nothing, the violation was something else entirely, and
    reporting a run the caller never asked for would hide a real fault.
    """
    from app.pipeline import executor

    monkeypatch.setattr(executor, "_existing_claim", lambda session, **kwargs: None)
    db_session.add(
        PipelineRun(collector_id=collector.id, status=PipelineRunStatus.QUEUED)
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        start_pipeline_run(db_session, collector=collector)
    db_session.rollback()


def test_a_stranded_collecting_run_is_diagnosable_and_never_retriggers(
    db_session: Session,
    collector: Collector,
    provider_settings: Settings,
    good_records: list[dict[str, Any]],
) -> None:
    """The ambiguous crash window, now that the constraint blocks new claims.

    A run stranded in COLLECTING with no provider job id is active, so it
    holds the collector's one active slot and a fresh claim joins it
    rather than starting a competing scrape. That is deliberate: the
    provider may already be running the original request.

    It does not loop forever. The resume pass reaches it, refuses to
    retrigger, and closes it FAILED with an explanation -- which both
    releases the slot for the next claim and leaves the reason on the
    record for an operator.
    """
    provider = CountingProvider(dataset=good_records)
    stranded, _ = start_pipeline_run(db_session, collector=collector)
    stranded.status = PipelineRunStatus.COLLECTING
    db_session.commit()

    # While stranded, it blocks a competing claim rather than scraping again.
    joined, already_running = start_pipeline_run(db_session, collector=collector)
    assert already_running is True
    assert joined.id == stranded.id

    with client_for(provider, provider_settings) as client:
        recovered = resume_unfinished_pipeline_runs(db_session, client, sleep=no_sleep)

        assert [item.id for item in recovered] == [stranded.id]
        assert recovered[0].status is PipelineRunStatus.FAILED
        assert recovered[0].error is not None
        assert "not retriggering" in recovered[0].error
        assert provider.trigger_count == 0

        # The slot is free again, and the next claim is a genuinely new run.
        fresh, already_running = start_pipeline_run(db_session, collector=collector)
        assert already_running is False
        assert fresh.id != stranded.id
