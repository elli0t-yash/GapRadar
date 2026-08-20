"""Running one claimed investigation to a terminal state.

Phase 2 scope: RESEARCH ONLY. This drives the same research engine the
opportunity path drives -- `app.research_intelligence.orchestration
.research_subject` -- against an Investigation-shaped ResearchSubject.
There is no demand evidence, no competitor intelligence, no whitespace
scoring and no commercial verdict, and no counter pretending otherwise.

Nothing here re-implements the pipeline. Query generation, acquisition,
ingestion, ranking, judging and persistence all live in the engine and
are shared byte for byte with opportunity enrichment; this module owns
the run record, the pre-flight quality gate, and the failure reporting
around it -- the same three things
`app.research_intelligence.enrichment` owns for signals.

The failure taxonomy is deliberately the SAME ResearchOutcomeReason used
by enrichment. A user investigating their own idea and an operator
enriching an opportunity need identical answers to "can I press this
again", and two taxonomies would eventually disagree.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Investigation, InvestigationRun
from app.domain.enums import InvestigationRunStatus, ResearchOutcomeReason
from app.investigations.runs import ACTIVE_STATUSES, set_run_status
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.acquisition import ResearchCollector
from app.research_intelligence.enrichment import (
    QueryGenerationProviderError,
    ResearchPlanUnavailableError,
    build_plan_with_fallback,
)
from app.research_intelligence.execution import (
    RESEARCH_ACQUISITION_BUDGET_SECONDS,
    ResearchQueryExecution,
)
from app.research_intelligence.matching import SemanticMatcher
from app.research_intelligence.orchestration import research_subject
from app.research_intelligence.query_generation import (
    ResearchQueryGenerator,
)
from app.research_intelligence.reporting import (
    all_searches_failed_message,
    counters_from_result,
    is_semantic_outage,
    states_from_result,
    success_reason,
)
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _FixedPlanGenerator:
    """Hands the engine the plan this module already validated.

    Without it the plan would be generated twice -- once for the quality
    gate and once inside the engine -- and the gate would be guarding a
    different plan from the one that actually runs.
    """

    def __init__(self, plan: ResearchQueryPlan) -> None:
        self._plan = plan

    def generate(self, subject: ResearchSubject) -> ResearchQueryPlan:
        return self._plan


def execute_run(
    session: Session,
    *,
    run_id: uuid.UUID,
    collector: ResearchCollector,
    matcher: SemanticMatcher | None = None,
    generator: ResearchQueryGenerator | None = None,
    fallback_generator: ResearchQueryGenerator | None = None,
    now: Callable[[], datetime] = _utcnow,
    acquisition_budget_seconds: float = RESEARCH_ACQUISITION_BUDGET_SECONDS,
) -> InvestigationRun:
    """Run one claimed investigation to a terminal state.

    Every exit writes a terminal status. A crash between QUEUED and here
    leaves the row RUNNING or QUEUED forever, which is the honest failure
    mode of an in-process executor and is why the status is persisted at
    all -- app.investigations.runs.reconcile_stale_investigation_runs is
    what makes such a row terminal again.

    The plan is validated BEFORE the collector is touched, so a junk
    query fails the run without spending anything. That gate matters more
    here than on the opportunity path: an investigation's text is
    whatever a user typed, and "my startup idea" produces exactly the
    fallback-only plan the gate was built to refuse.
    """
    run = session.get(InvestigationRun, run_id)
    if run is None:
        raise LookupError(f"investigation run {run_id} not found")
    if run.status not in ACTIVE_STATUSES:
        return run

    investigation = session.get(Investigation, run.investigation_id)
    if investigation is None:  # pragma: no cover - FK makes this unreachable
        return _fail(
            session,
            run,
            "the investigation no longer exists",
            now=now,
            reason=ResearchOutcomeReason.OPPORTUNITY_MISSING,
        )

    set_run_status(session, run, InvestigationRunStatus.RUNNING)
    run.started_at = now()
    session.commit()
    logger.info("[investigation] running run=%s", run.id)

    subject = research_subject_from_investigation(investigation)
    try:
        plan = build_plan_with_fallback(
            subject, generator=generator, fallback=fallback_generator
        )
    except ResearchPlanUnavailableError as exc:
        # Both stages declined. Deterministic input produces a
        # deterministic answer, so this is NOT retryable and the UI must
        # not offer a button that repeats it.
        logger.info("[investigation] plan unavailable run=%s reason=%s", run.id, exc)
        return _fail(
            session,
            run,
            str(exc),
            now=now,
            reason=ResearchOutcomeReason.QUERY_PLAN_UNAVAILABLE,
        )
    except QueryGenerationProviderError as exc:
        # The fallback provider itself failed, so the plan was never
        # judged on its merits. Retrying can genuinely differ.
        logger.warning(
            "[investigation] query generation provider failed run=%s: %s", run.id, exc
        )
        return _fail(
            session,
            run,
            str(exc),
            now=now,
            reason=ResearchOutcomeReason.QUERY_GENERATION_PROVIDER_ERROR,
        )

    # Seed the per-query state so a client polling between RUNNING and the
    # first search already sees the queries as pending, rather than an
    # empty list whose shape it would have to guess.
    _publish_progress(
        session, run, [ResearchQueryExecution(query=query) for query in plan.queries]
    )

    try:
        result = research_subject(
            session,
            subject=subject,
            collector=collector,
            generator=_FixedPlanGenerator(plan),
            matcher=matcher,
            on_progress=lambda executions: _publish_progress(session, run, executions),
            acquisition_budget_seconds=acquisition_budget_seconds,
        )
    except Exception as exc:
        # Anything the engine could not absorb. Per-query provider
        # failures are handled inside it and do not reach here.
        logger.exception("investigation_run_failed", extra={"run_id": str(run.id)})
        return _fail(session, run, f"{type(exc).__name__}: {exc}", now=now)

    # -- the partial-result policy ------------------------------------
    # Every search failing is a failed run: there is nothing to show and
    # the user must be able to retry. But ONE search returning is enough
    # to be useful, and discarding real papers because a third query
    # timed out would be the worst of both worlds.
    if result.queries and not result.successful_queries:
        timed_out = len(result.timed_out_queries) == len(result.queries)
        return _fail(
            session,
            run,
            all_searches_failed_message(result),
            now=now,
            reason=(
                ResearchOutcomeReason.TIMEOUT
                if timed_out
                else ResearchOutcomeReason.ACQUISITION_FAILED
            ),
            query_states=states_from_result(result),
            counters=counters_from_result(result),
        )

    # -- semantic outage vs an honest zero result ----------------------
    counters = counters_from_result(result)
    if is_semantic_outage(result, counters):
        return _fail(
            session,
            run,
            "the research matcher could not be reached, so no paper was judged",
            now=now,
            reason=ResearchOutcomeReason.SEMANTIC_MATCHING_FAILED,
            query_states=states_from_result(result),
            counters=counters,
        )

    set_run_status(session, run, InvestigationRunStatus.SUCCEEDED)
    run.completed_at = now()
    run.error = None
    # Set alongside SUCCEEDED, never instead of it.
    run.warning = result.acquisition_warning
    run.query_states = states_from_result(result)
    run.counters = counters
    run.outcome_reason = success_reason(result)
    session.commit()
    session.refresh(run)
    logger.info(
        "[investigation] succeeded run=%s papers=%d matches=%d",
        run.id,
        result.candidate_paper_count,
        result.matches_created + result.matches_updated,
    )
    return run


def _publish_progress(
    session: Session,
    run: InvestigationRun,
    executions: Sequence[ResearchQueryExecution],
) -> None:
    """Persist per-query progress so the browser can poll something true.

    Called on the ENGINE's thread, never a search worker -- the runner
    guarantees that, and it is what makes writing through this Session
    safe.

    Committed immediately and on its own: progress that is only visible
    after the run finishes is not progress. A failure to write it is
    logged and swallowed, because losing a progress tick must never fail
    a run that is otherwise going fine.
    """
    try:
        run.query_states = [execution.to_state() for execution in executions]
        session.commit()
    except Exception:  # pragma: no cover - progress is best-effort
        logger.warning(
            "investigation_run_progress_write_failed",
            extra={"run_id": str(run.id)},
            exc_info=True,
        )
        session.rollback()


def _fail(
    session: Session,
    run: InvestigationRun,
    message: str,
    *,
    now: Callable[[], datetime],
    reason: ResearchOutcomeReason = ResearchOutcomeReason.UNEXPECTED_ERROR,
    query_states: list[dict[str, object]] | None = None,
    counters: dict[str, int] | None = None,
) -> InvestigationRun:
    """Close the run as FAILED, keeping why -- in both forms.

    `error` is the sentence a person reads; `outcome_reason` is the value
    the frontend branches on. Both are written together so they can never
    describe different failures.
    """
    set_run_status(session, run, InvestigationRunStatus.FAILED)
    run.completed_at = now()
    run.error = message
    run.outcome_reason = reason
    if query_states is not None:
        run.query_states = query_states
    if counters is not None:
        run.counters = counters
    session.commit()
    session.refresh(run)
    logger.warning(
        "[investigation] failed run=%s reason=%s detail=%s",
        run.id,
        reason.value,
        message,
    )
    return run
