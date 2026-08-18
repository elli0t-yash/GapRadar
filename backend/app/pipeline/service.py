"""The one orchestration seam the scheduler and the API both use.

    collect -> evaluate -> (repair) -> verify

Every stage is delegated to the module that already owns it:

- app.collection.service.run_fix_my_itch_collection executes the
  collection and finalizes the CollectorRun.
- app.recallguard.service.evaluate_collector_run judges that run and
  opens or updates the collector's single active incident.
- app.recallguard.healing.resume_or_execute_healing_attempt performs the
  whole repair -- it asks Bright Data whether a repair is already in
  flight and rejoins it if so, otherwise starts a new attempt, and either
  way owns the attempt budget, candidate preflight, approve/reject, and
  fresh independent verification. It is the only thing that can end in
  RECOVERED.

Nothing here re-implements any of that. What this module adds is the
decision of *whether* a repair attempt may start, and a single result
object describing what happened:

- A repair is requested only for a diagnosis that recommends one
  (REQUEST_HEAL). An outage, an internal ingestion failure, or an
  undiagnosed failure never reaches Bright Data's healer.
- An incident escalated to a human is left exactly where it is. An
  incident whose recorded status still says a repair is in flight is NOT
  skipped, because that is exactly the state a timed-out or crashed
  process leaves behind -- the healing layer asks the provider what is
  really happening and resumes or refuses accordingly.
- Exactly one repair attempt per pipeline run, and a resumed repair is
  not a new attempt. There is no retry loop here; the three-attempt
  budget lives in RecallGuard and is not duplicated, weakened, or worked
  around.
- A CollectionError is translated into a result, never hidden: the run
  it produced is still evaluated by RecallGuard, and the stage and
  message travel back in `collection_failure`. Anything that is not a
  CollectionError propagates untouched.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collection.errors import CollectionError
from app.collection.schemas import (
    DEFAULT_POLLING_POLICY,
    CollectionRunResult,
    PollingPolicy,
)
from app.collection.service import run_fix_my_itch_collection
from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.domain.enums import (
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
    RunStatus,
)
from app.integrations.brightdata.client import BrightDataClient
from app.pipeline.schemas import (
    CollectionFailure,
    PipelineOutcome,
    PipelineRunResult,
)
from app.recallguard.healing import (
    DEFAULT_SELF_HEALING_POLICY,
    HealingAttemptResult,
    HealingOutcome,
    SelfHealingPolicy,
    resume_or_execute_healing_attempt,
)
from app.recallguard.schemas import (
    DEFAULT_POLICY,
    BaselineProfile,
    ReliabilityEvaluation,
    ReliabilityPolicy,
)
from app.recallguard.service import collector_reliability_state, evaluate_collector_run

logger = logging.getLogger(__name__)

# Only this diagnosis justifies asking Bright Data to repair the scraper.
# Mirrors RecallGuard's own rule; start_healing enforces it again, so a
# mistake here cannot let a non-repairable incident through.
_REPAIRABLE_ACTIONS = frozenset({RecommendedAction.REQUEST_HEAL})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CollectFn(Protocol):
    def __call__(
        self,
        session: Session,
        client: BrightDataClient,
        *,
        collector: Collector,
        polling: PollingPolicy = ...,
        now: Callable[[], datetime] = ...,
        sleep: Callable[[float], None] = ...,
    ) -> CollectionRunResult: ...


class EvaluateFn(Protocol):
    def __call__(
        self,
        session: Session,
        *,
        run: CollectorRun,
        baseline: BaselineProfile | None = ...,
        policy: ReliabilityPolicy = ...,
        now: Callable[[], datetime] = ...,
    ) -> ReliabilityEvaluation: ...


class HealFn(Protocol):
    def __call__(
        self,
        session: Session,
        client: BrightDataClient,
        *,
        incident: ReliabilityIncident,
        collector: Collector,
        baseline: BaselineProfile | None = ...,
        reliability_policy: ReliabilityPolicy = ...,
        healing_policy: SelfHealingPolicy = ...,
        collection_polling: PollingPolicy = ...,
        now: Callable[[], datetime] = ...,
        sleep: Callable[[float], None] = ...,
    ) -> HealingAttemptResult: ...


def run_pipeline(
    session: Session,
    client: BrightDataClient,
    *,
    collector: Collector,
    baseline: BaselineProfile | None = None,
    reliability_policy: ReliabilityPolicy = DEFAULT_POLICY,
    healing_policy: SelfHealingPolicy = DEFAULT_SELF_HEALING_POLICY,
    collection_polling: PollingPolicy = DEFAULT_POLLING_POLICY,
    allow_healing: bool = True,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
    collect: CollectFn = run_fix_my_itch_collection,
    evaluate: EvaluateFn = evaluate_collector_run,
    heal: HealFn = resume_or_execute_healing_attempt,
) -> PipelineRunResult:
    """Collect once, judge the result, and repair it if RecallGuard says so.

    Dependencies are injected rather than imported at the call site so a
    test can drive the whole flow -- including the decision *not* to
    heal -- without a provider, a clock, or a real Bright Data call.
    """
    collection, failure = _collect(
        session,
        client,
        collector=collector,
        polling=collection_polling,
        now=now,
        sleep=sleep,
        collect=collect,
    )

    return evaluate_and_heal(
        session,
        client,
        collector=collector,
        collection=collection,
        failure=failure,
        baseline=baseline,
        reliability_policy=reliability_policy,
        healing_policy=healing_policy,
        collection_polling=collection_polling,
        allow_healing=allow_healing,
        now=now,
        sleep=sleep,
        evaluate=evaluate,
        heal=heal,
    )


def evaluate_and_heal(
    session: Session,
    client: BrightDataClient,
    *,
    collector: Collector,
    collection: CollectionRunResult | None,
    failure: CollectionFailure | None,
    collector_run_id: uuid.UUID | None = None,
    baseline: BaselineProfile | None = None,
    reliability_policy: ReliabilityPolicy = DEFAULT_POLICY,
    healing_policy: SelfHealingPolicy = DEFAULT_SELF_HEALING_POLICY,
    collection_polling: PollingPolicy = DEFAULT_POLLING_POLICY,
    allow_healing: bool = True,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
    evaluate: EvaluateFn = evaluate_collector_run,
    heal: HealFn = resume_or_execute_healing_attempt,
) -> PipelineRunResult:
    """Judge a finished collection and repair it if RecallGuard says so.

    Everything run_pipeline does after the collection itself, split out
    so an asynchronous executor -- which owns the waiting and therefore
    cannot call run_pipeline -- reaches the identical verdict through the
    identical code rather than a second implementation of the same rules.
    """
    # `collector_run_id` is the fallback for a resumed execution whose
    # collection finished in an earlier process: there is no result
    # object to carry the id, but the run is on disk and must still be
    # judged. Without it a resume would look like a trigger failure and
    # report an unevaluable collection that actually succeeded.
    run_id = (
        collection.collector_run_id
        if collection
        else failure.collector_run_id
        if failure
        else None
    ) or collector_run_id
    if run_id is None:
        # A trigger failure never receives a collection id, so no
        # CollectorRun exists to evaluate. Fail-closed: report the
        # collector's existing state and invent neither a run nor an
        # incident.
        return _unevaluable(session, collector=collector, failure=failure)

    run = session.get(CollectorRun, run_id)
    if run is None:  # pragma: no cover - defensive
        raise RuntimeError(f"collector run {run_id} vanished after collection")

    evaluation = evaluate(
        session, run=run, baseline=baseline, policy=reliability_policy, now=now
    )

    healing: HealingAttemptResult | None = None
    skipped: str | None = None
    if not evaluation.passed:
        incident = (
            session.get(ReliabilityIncident, evaluation.incident_id)
            if evaluation.incident_id
            else None
        )
        skipped = _healing_refusal(evaluation, incident, allow_healing=allow_healing)
        if skipped is None and incident is not None:
            healing = heal(
                session,
                client,
                incident=incident,
                collector=collector,
                baseline=baseline,
                reliability_policy=reliability_policy,
                healing_policy=healing_policy,
                collection_polling=collection_polling,
                now=now,
                sleep=sleep,
            )

    state = collector_reliability_state(session, collector_id=collector.id)
    result = _result(
        collector=collector,
        state=state,
        collection=collection,
        failure=failure,
        evaluation=evaluation,
        healing=healing,
        healing_skipped_reason=skipped,
    )
    logger.info(
        "pipeline_run_finished",
        extra={
            "collector_id": str(collector.id),
            "collector_run_id": str(result.collector_run_id),
            "outcome": result.outcome.value,
            "reliability_state": result.reliability_state.value,
            "trusted": result.trusted,
        },
    )
    return result


def baseline_from_history(
    session: Session,
    *,
    collector_id: uuid.UUID,
    exclude_run_id: uuid.UUID | None = None,
    label: str = "observed_history",
) -> BaselineProfile | None:
    """The largest dataset this collector has ever actually delivered.

    An observation, never a contract: RecallGuard's completeness check
    never penalizes growth, so the high-water mark of past SUCCEEDED runs
    is the conservative reference for spotting a collapse. Returns None
    when the collector has no successful history -- with nothing real to
    compare against, completeness is simply not evaluated rather than
    measured against a number nobody observed.
    """
    query = select(func.max(CollectorRun.record_count)).where(
        CollectorRun.collector_id == collector_id,
        CollectorRun.status == RunStatus.SUCCEEDED,
    )
    if exclude_run_id is not None:
        query = query.where(CollectorRun.id != exclude_run_id)

    observed = session.execute(query).scalar()
    if not observed:
        return None
    return BaselineProfile(label=label, record_count=observed)


# -- stages -----------------------------------------------------------------


def _collect(
    session: Session,
    client: BrightDataClient,
    *,
    collector: Collector,
    polling: PollingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    collect: CollectFn,
) -> tuple[CollectionRunResult | None, CollectionFailure | None]:
    """Run the collection, keeping a failure as a fact rather than an end.

    A CollectionError is caught only so the run it produced can still be
    judged by RecallGuard -- which is the entire point of evaluating
    failed runs. The stage, type, and message travel back in the result;
    nothing is discarded, and any other exception is left alone.
    """
    try:
        return (
            collect(
                session,
                client,
                collector=collector,
                polling=polling,
                now=now,
                sleep=sleep,
            ),
            None,
        )
    except CollectionError as exc:
        logger.warning(
            "pipeline_collection_failed",
            extra={
                "collector_id": str(collector.id),
                "stage": exc.stage,
                "error": type(exc).__name__,
            },
        )
        return None, CollectionFailure(
            stage=exc.stage,
            error=type(exc).__name__,
            message=str(exc),
            collector_run_id=exc.collector_run_id,
        )


def _healing_refusal(
    evaluation: ReliabilityEvaluation,
    incident: ReliabilityIncident | None,
    *,
    allow_healing: bool,
) -> str | None:
    """Why this failing run must not start a repair, or None if it may."""
    if incident is None:  # pragma: no cover - defensive
        return "no incident was opened for this failure"
    if not allow_healing:
        return "healing disabled for this pipeline run"
    if evaluation.recommended_action not in _REPAIRABLE_ACTIONS:
        action = evaluation.recommended_action
        return (
            f"diagnosis recommends {action.value if action else 'nothing'}; "
            "healing repairs extraction drift only"
        )
    if incident.status is IncidentStatus.MANUAL_REVIEW:
        return (
            "incident is with a human for manual review; autonomous repair "
            "does not take it back"
        )
    # Every other active status is passed through on purpose. DEGRADED is
    # the ordinary case; HEALING, VALIDATING and VERIFYING are what a
    # process that died mid-attempt leaves behind, and only the provider
    # can say whether that repair is still alive -- so the decision
    # belongs to the healing layer, not to a status check here.
    return None


# -- result assembly --------------------------------------------------------


def _unevaluable(
    session: Session, *, collector: Collector, failure: CollectionFailure | None
) -> PipelineRunResult:
    state = collector_reliability_state(session, collector_id=collector.id)
    logger.warning(
        "pipeline_run_unevaluable",
        extra={
            "collector_id": str(collector.id),
            "stage": failure.stage if failure else None,
        },
    )
    return PipelineRunResult(
        collector_id=collector.id,
        outcome=PipelineOutcome.COLLECTION_UNEVALUABLE,
        reliability_state=state,
        trusted=False,
        collection_failure=failure,
    )


def _result(
    *,
    collector: Collector,
    state: ReliabilityState,
    collection: CollectionRunResult | None,
    failure: CollectionFailure | None,
    evaluation: ReliabilityEvaluation,
    healing: HealingAttemptResult | None,
    healing_skipped_reason: str | None,
) -> PipelineRunResult:
    trusted_run_id = _trusted_run_id(
        state=state, evaluation=evaluation, healing=healing
    )
    return PipelineRunResult(
        collector_id=collector.id,
        outcome=_outcome(state=state, evaluation=evaluation, healing=healing),
        reliability_state=state,
        trusted=trusted_run_id is not None,
        collector_run_id=evaluation.collector_run_id,
        trusted_collector_run_id=trusted_run_id,
        incident_id=(healing.incident_id if healing else evaluation.incident_id),
        collection=collection,
        collection_failure=failure,
        evaluation=evaluation,
        healing=healing,
        healing_skipped_reason=healing_skipped_reason,
    )


def _trusted_run_id(
    *,
    state: ReliabilityState,
    evaluation: ReliabilityEvaluation,
    healing: HealingAttemptResult | None,
) -> uuid.UUID | None:
    """The run whose signals downstream may use -- or None.

    Two conditions, both required. The collector must currently be
    HEALTHY (no active incident of any kind), and the data must come from
    a run that passed every check: either this cycle's own collection, or
    the independent verification run that proved a repair. A degraded, in
    flight, or escalated collector yields None, so bad data can never
    become trusted downstream output.
    """
    if state is not ReliabilityState.HEALTHY:
        return None
    if healing is not None:
        return healing.verification_run_id if healing.recovered else None
    return evaluation.collector_run_id if evaluation.passed else None


def _outcome(
    *,
    state: ReliabilityState,
    evaluation: ReliabilityEvaluation,
    healing: HealingAttemptResult | None,
) -> PipelineOutcome:
    if healing is not None:
        if healing.recovered:
            return PipelineOutcome.RECOVERED
        if healing.outcome is HealingOutcome.ESCALATED:
            return PipelineOutcome.MANUAL_REVIEW
        if healing.outcome is HealingOutcome.LOCAL_TIMEOUT:
            # GapRadar stopped waiting; the provider repair is not known
            # to have failed. The next invocation resumes it.
            return PipelineOutcome.HEALING_IN_PROGRESS
        if healing.outcome is HealingOutcome.REFUSED:
            # Nothing was triggered and nothing changed, so the collector
            # is simply still degraded.
            return PipelineOutcome.DEGRADED
        return PipelineOutcome.HEALING_FAILED
    if state is ReliabilityState.MANUAL_REVIEW:
        return PipelineOutcome.MANUAL_REVIEW
    if evaluation.passed and state is ReliabilityState.HEALTHY:
        return PipelineOutcome.HEALTHY
    return PipelineOutcome.DEGRADED
