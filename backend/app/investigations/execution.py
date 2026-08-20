"""Running one claimed investigation to a terminal state.

Phase 3 scope: PLANNING -> RESEARCH -> DEMAND -> COMPETITORS.

    planning     app.investigations.planning        (no provider call)
    research     the shared academic engine, arXiv  (unchanged)
    demand       web discovery, SERP                (new)
    competitors  web discovery, SERP                (new)

There is still no whitespace scoring and no commercial verdict, and no
counter pretending otherwise.

THE PHASES ARE INDEPENDENT ON PURPOSE. A run whose demand searches all
failed still keeps its research and its competitors; a run with no web
provider configured still does its research and reports the web phases
SKIPPED. A phase failing is never allowed to discard what the others
already produced -- money was spent on those.

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
from app.domain.enums import (
    InvestigationRunStatus,
    PhaseState,
    ResearchOutcomeReason,
)
from app.investigations.planning import (
    InvestigationPlan,
    InvestigationPlanRejectedError,
    InvestigationWebQueryGenerator,
    build_investigation_plan,
)
from app.investigations.progress import (
    InvestigationRunPhases,
    PlanningProgress,
    ResearchPhaseProgress,
    WebPhaseProgress,
)
from app.investigations.runs import ACTIVE_STATUSES, set_run_status
from app.investigations.web_intelligence import discover_web_evidence
from app.research_intelligence.acquisition import ResearchCollector
from app.research_intelligence.enrichment import (
    QueryGenerationProviderError,
    ResearchPlanUnavailableError,
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
from app.web_intelligence.acquisition import WebSearchProvider
from app.web_intelligence.classification import (
    CompetitorClassifier,
    DemandClassifier,
    LexicalCompetitorClassifier,
    LexicalDemandClassifier,
)
from app.web_intelligence.execution import MAX_CONCURRENT_WEB_SEARCHES

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
    web_provider: WebSearchProvider | None = None,
    web_generator: InvestigationWebQueryGenerator | None = None,
    web_fallback_generator: InvestigationWebQueryGenerator | None = None,
    demand_classifier: DemandClassifier | None = None,
    competitor_classifier: CompetitorClassifier | None = None,
    provider_name: str = "unknown",
    provider_product: str = "unknown",
    max_web_concurrency: int = MAX_CONCURRENT_WEB_SEARCHES,
    now: Callable[[], datetime] = _utcnow,
    acquisition_budget_seconds: float = RESEARCH_ACQUISITION_BUDGET_SECONDS,
) -> InvestigationRun:
    """Run one claimed investigation to a terminal state.

    Every exit writes a terminal status. A crash between QUEUED and here
    leaves the row RUNNING or QUEUED forever, which is the honest failure
    mode of an in-process executor and is why the status is persisted at
    all -- app.investigations.runs.reconcile_stale_investigation_runs is
    what makes such a row terminal again.

    The whole plan -- research, demand and competitor families -- is
    built and validated BEFORE any provider is touched, so a junk plan
    fails the run without spending anything. That gate matters more here
    than on the opportunity path: an investigation's text is whatever a
    user typed, and "my startup idea" produces exactly the fallback-only
    plan the gate was built to refuse.

    `web_provider` is optional. Without one the web phases are SKIPPED,
    which is a third state distinct from complete and from failed: no
    evidence and no failure. A deployment with no SERP zone configured
    still gets its research rather than a run that refuses to start.
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

    phases = InvestigationRunPhases()
    _publish_phases(session, run, phases)

    try:
        plan = build_investigation_plan(
            investigation,
            research_generator=generator,
            research_fallback=fallback_generator,
            web_generator=web_generator,
            web_fallback=web_fallback_generator,
        )
    except InvestigationPlanRejectedError as exc:
        # The web families could not be planned. Deterministic input
        # produces a deterministic answer, so this is NOT retryable.
        logger.info("[investigation] plan rejected run=%s reason=%s", run.id, exc)
        return _fail(
            session,
            run,
            str(exc),
            now=now,
            reason=ResearchOutcomeReason.QUERY_PLAN_UNAVAILABLE,
            phases=phases.model_copy(
                update={"planning": PlanningProgress(state=PhaseState.FAILED)}
            ),
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
            phases=phases.model_copy(
                update={"planning": PlanningProgress(state=PhaseState.FAILED)}
            ),
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
            phases=phases.model_copy(
                update={"planning": PlanningProgress(state=PhaseState.FAILED)}
            ),
        )

    subject = plan.subject
    phases = phases.model_copy(
        update={
            "planning": PlanningProgress(
                state=PhaseState.COMPLETE,
                research_queries=len(plan.research_queries),
                demand_queries=len(plan.demand_queries),
                competitor_queries=len(plan.competitor_queries),
            ),
            "research": ResearchPhaseProgress(
                state=PhaseState.RUNNING,
                queries_total=len(plan.research_queries),
            ),
            "demand": WebPhaseProgress(
                state=PhaseState.PENDING,
                queries_total=len(plan.demand_queries),
            ),
            "competitors": WebPhaseProgress(
                state=PhaseState.PENDING,
                queries_total=len(plan.competitor_queries),
            ),
        }
    )
    _publish_phases(session, run, phases)

    # Seed the per-query state so a client polling between RUNNING and the
    # first search already sees the queries as pending, rather than an
    # empty list whose shape it would have to guess.
    _publish_progress(
        session,
        run,
        [ResearchQueryExecution(query=query) for query in plan.research_queries],
    )

    try:
        result = research_subject(
            session,
            subject=subject,
            collector=collector,
            generator=_FixedPlanGenerator(plan.research_plan),
            matcher=matcher,
            on_progress=lambda executions: _publish_progress(session, run, executions),
            acquisition_budget_seconds=acquisition_budget_seconds,
        )
    except Exception as exc:
        # Anything the engine could not absorb. Per-query provider
        # failures are handled inside it and do not reach here.
        logger.exception("investigation_run_failed", extra={"run_id": str(run.id)})
        return _fail(
            session,
            run,
            f"{type(exc).__name__}: {exc}",
            now=now,
            phases=_research_failed(phases),
        )

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
            phases=_research_failed(phases, result=result),
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
            phases=_research_failed(phases, result=result),
        )

    # -- research is done; record it before the web phases start -------
    phases = phases.model_copy(
        update={
            "research": ResearchPhaseProgress(
                state=(
                    PhaseState.PARTIAL if result.is_partial else PhaseState.COMPLETE
                ),
                queries_total=len(result.queries),
                queries_completed=len(result.queries),
                discovered=counters["discovered"],
                selected=counters["selected"],
                judged=counters["judged"],
                matched=counters["matched"],
            )
        }
    )
    _publish_phases(session, run, phases)

    # -- web discovery -------------------------------------------------
    #
    # Runs AFTER research and never gates it: a web provider outage must
    # not discard papers already found, judged and persisted. Every
    # failure below is absorbed into phase state, so the run still
    # SUCCEEDS with whatever it learned.
    phases = _run_web_phases(
        session,
        run=run,
        plan=plan,
        phases=phases,
        provider=web_provider,
        demand_classifier=demand_classifier or LexicalDemandClassifier(),
        competitor_classifier=competitor_classifier or LexicalCompetitorClassifier(),
        provider_name=provider_name,
        provider_product=provider_product,
        max_concurrency=max_web_concurrency,
        now=now,
    )

    set_run_status(session, run, InvestigationRunStatus.SUCCEEDED)
    run.completed_at = now()
    run.error = None
    # Set alongside SUCCEEDED, never instead of it. Research partiality
    # and web partiality are both reported; neither turns the run into a
    # failure while it still holds real evidence.
    run.warning = _combined_warning(result.acquisition_warning, phases)
    run.query_states = states_from_result(result)
    run.counters = counters
    run.phases = phases.to_payload()
    run.outcome_reason = success_reason(result)
    session.commit()
    session.refresh(run)
    logger.info(
        "[investigation] succeeded run=%s papers=%d matches=%d demand=%d "
        "competitors=%d",
        run.id,
        result.candidate_paper_count,
        result.matches_created + result.matches_updated,
        phases.demand.accepted,
        phases.competitors.accepted,
    )
    return run


def _run_web_phases(
    session: Session,
    *,
    run: InvestigationRun,
    plan: InvestigationPlan,
    phases: InvestigationRunPhases,
    provider: WebSearchProvider | None,
    demand_classifier: DemandClassifier,
    competitor_classifier: CompetitorClassifier,
    provider_name: str,
    provider_product: str,
    max_concurrency: int,
    now: Callable[[], datetime],
) -> InvestigationRunPhases:
    """Discovery for both web families. Never fails the run.

    THE ISOLATION IS THE POINT. By the time this is called the research
    phase has already spent provider budget and persisted papers and
    verdicts. A SERP outage is a reason to report two phases as failed,
    never a reason to throw that away -- so every exit from here returns
    phase state rather than raising.

    No provider means SKIPPED: no evidence and no failure, which is
    honest and is what a deployment with no SERP zone configured should
    read.
    """
    if provider is None:
        logger.info("[investigation] web discovery skipped run=%s: no provider", run.id)
        skipped = phases.model_copy(
            update={
                "demand": phases.demand.model_copy(
                    update={"state": PhaseState.SKIPPED}
                ),
                "competitors": phases.competitors.model_copy(
                    update={"state": PhaseState.SKIPPED}
                ),
            }
        )
        _publish_phases(session, run, skipped)
        return skipped

    running = phases.model_copy(
        update={
            "demand": phases.demand.model_copy(update={"state": PhaseState.RUNNING}),
            "competitors": phases.competitors.model_copy(
                update={"state": PhaseState.RUNNING}
            ),
        }
    )
    _publish_phases(session, run, running)

    try:
        results = discover_web_evidence(
            session,
            subject=plan.subject,
            investigation_id=run.investigation_id,
            run_id=run.id,
            searches=plan.web_searches,
            provider=provider,
            locale=plan.locale,
            demand_classifier=demand_classifier,
            competitor_classifier=competitor_classifier,
            provider_name=provider_name,
            provider_product=provider_product,
            max_concurrency=max_concurrency,
            now=now,
        )
    except Exception as exc:  # pragma: no cover - defensive
        # Per-query provider failures are absorbed by the runner and
        # never reach here. Anything that does is a defect on our side,
        # and it still must not discard the research phase.
        logger.exception(
            "investigation_web_discovery_failed", extra={"run_id": str(run.id)}
        )
        session.rollback()
        failed = running.model_copy(
            update={
                "demand": running.demand.model_copy(
                    update={"state": PhaseState.FAILED}
                ),
                "competitors": running.competitors.model_copy(
                    update={"state": PhaseState.FAILED}
                ),
            }
        )
        logger.warning("[investigation] web phases failed: %s", exc)
        _publish_phases(session, run, failed)
        return failed

    updated = running.with_web_results(results)
    _publish_phases(session, run, updated)
    return updated


def _research_failed(
    phases: InvestigationRunPhases, *, result: object | None = None
) -> InvestigationRunPhases:
    """Mark research FAILED, leaving the web phases as they were.

    They were never started, so they stay PENDING rather than being
    reported as failures they did not have.
    """
    return phases.model_copy(
        update={
            "research": phases.research.model_copy(
                update={"state": PhaseState.FAILED}
            )
        }
    )


def _combined_warning(
    research_warning: str | None, phases: InvestigationRunPhases
) -> str | None:
    """One sentence covering every phase that is incomplete, or None.

    Stated rather than hidden: a run that succeeded on two phases and
    lost two is still a success, and the gap is exactly what a reader
    needs in order to judge how much the answer is worth.
    """
    parts = [research_warning] if research_warning else []
    for name, phase in (("demand", phases.demand), ("competitors", phases.competitors)):
        if phase.state is PhaseState.FAILED:
            parts.append(f"every {name} search failed, so none was collected")
        elif phase.state is PhaseState.PARTIAL:
            parts.append(
                f"{phase.queries_succeeded} of {phase.queries_total} {name} "
                "searches completed"
            )
    return "; ".join(parts) if parts else None


def _publish_phases(
    session: Session,
    run: InvestigationRun,
    phases: InvestigationRunPhases,
) -> None:
    """Persist phase progress so the browser can poll something true.

    Written on the CALLING thread, never a search worker. `counters` is
    written from the same snapshot so the two representations of the
    research funnel cannot disagree.

    Committed immediately and on its own: progress that is only visible
    after the run finishes is not progress. A failure to write it is
    logged and swallowed -- losing a progress tick must never fail a run
    that is otherwise going fine.
    """
    try:
        run.phases = phases.to_payload()
        run.counters = phases.research.counters.model_dump()
        session.commit()
    except Exception:  # pragma: no cover - progress is best-effort
        logger.warning(
            "investigation_run_phase_write_failed",
            extra={"run_id": str(run.id)},
            exc_info=True,
        )
        session.rollback()


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
    phases: InvestigationRunPhases | None = None,
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
    if phases is not None:
        run.phases = phases.to_payload()
    session.commit()
    session.refresh(run)
    logger.warning(
        "[investigation] failed run=%s reason=%s detail=%s",
        run.id,
        reason.value,
        message,
    )
    return run
