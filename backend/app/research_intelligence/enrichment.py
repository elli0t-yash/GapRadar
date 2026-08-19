"""On-demand research enrichment: claim it, run it out of band, report it.

    POST /research/enrich  ->  claim + 202        (this module: start_enrichment)
                               background work    (this module: run_enrichment)
    GET  /research/enrichment -> watch it         (this module: latest_enrichment)
    GET  /research            -> read the result  (service.get_research_intelligence)

The read endpoint stays a pure persisted read. This module is the ONLY
path that spends a provider call for research, and it is only ever
reached by an explicit user action.

Nothing here re-implements the pipeline. `enrich_opportunity_with_research`
already owns query generation, acquisition, ingestion, ranking, judging and
persistence; this adds the job record, the deduplication, the pre-flight
quality gate, and the failure reporting around it.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ResearchEnrichmentRun, Signal
from app.db.models.research_enrichment_run import ACTIVE_ENRICHMENT_STATUSES
from app.domain.enums import ResearchEnrichmentStatus
from app.research_intelligence.acquisition import ResearchCollector
from app.research_intelligence.execution import (
    RESEARCH_ACQUISITION_BUDGET_SECONDS,
    ResearchQueryExecution,
)
from app.research_intelligence.matching import SemanticMatcher
from app.research_intelligence.orchestration import (
    ResearchEnrichmentResult,
    enrich_opportunity_with_research,
)
from app.research_intelligence.query_generation import (
    ConceptQueryGenerator,
    ResearchQueryGenerationError,
    ResearchQueryGenerator,
)
from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan
from app.research_intelligence.service import market_context_from_signal

logger = logging.getLogger(__name__)

ACTIVE_STATUSES: frozenset[ResearchEnrichmentStatus] = frozenset(
    ACTIVE_ENRICHMENT_STATUSES
)


class ResearchPlanRejectedError(Exception):
    """The generated plan is not worth spending provider calls on.

    Raised BEFORE acquisition. A query-generation audit over the whole
    trusted corpus found plans built entirely from an industry-name
    fallback -- "travel systems optimization", "beauty systems demand
    forecasting" -- which are not terms anything is published under. Those
    plans cost three Bright Data jobs and return nothing usable, so they
    are refused with a reason rather than run.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -- the quality gate -------------------------------------------------------

# The generator's fallback for an industry it has no mapping for is
# "<token> systems". A plan whose concepts are ALL of that shape was built
# from the industry name alone: the problem wording contributed nothing,
# and searching it retrieves noise.
_FALLBACK_CONCEPT_SUFFIX = " systems"

# Concepts that end in " systems" but are genuine research vocabulary, so a
# plan containing them is not fallback-only.
_REAL_SYSTEMS_CONCEPTS = frozenset(
    {
        "payment systems",
        "energy systems",
        "recommender systems",
        "clinical decision support systems",
    }
)


def _is_fallback_concept(concept: str) -> bool:
    return (
        concept.endswith(_FALLBACK_CONCEPT_SUFFIX)
        and concept not in _REAL_SYSTEMS_CONCEPTS
    )


def validate_plan(plan: ResearchQueryPlan) -> None:
    """Refuse a plan that would spend provider calls on nothing.

    Deliberately narrow. This does not judge whether a plan is GOOD -- it
    rejects the shapes already shown to be worthless, and lets everything
    else through:

    - no queries at all, or a blank one;
    - a query that repeats a token ("demand forecasting demand
      forecasting"), which is malformed rather than narrow;
    - a plan with no concepts, meaning nothing was recognised;
    - a plan whose concepts are ENTIRELY the industry-name fallback,
      meaning the problem wording contributed nothing.

    Raises ResearchPlanRejectedError with a reason safe to show an
    operator. The message never contains a credential.
    """
    if not plan.queries:
        raise ResearchPlanRejectedError("no research queries could be generated")

    for query in plan.queries:
        if not query.strip():
            raise ResearchPlanRejectedError("a generated query was blank")
        tokens = query.split()
        if len(tokens) != len(set(tokens)):
            raise ResearchPlanRejectedError(
                f"generated query repeats a term and is malformed: {query!r}"
            )

    if not plan.concepts:
        raise ResearchPlanRejectedError(
            "no research concepts could be derived from this problem"
        )

    if all(_is_fallback_concept(concept) for concept in plan.concepts):
        raise ResearchPlanRejectedError(
            "this problem's wording matched no known research vocabulary, so "
            "every query would be built from its industry name alone"
        )


def build_plan(
    context: MarketContext, generator: ResearchQueryGenerator | None = None
) -> ResearchQueryPlan:
    """Generate a plan and refuse it if it is not worth running."""
    plan = (generator or ConceptQueryGenerator()).generate(context)
    validate_plan(plan)
    return plan


# -- claiming ---------------------------------------------------------------


def active_enrichment(
    session: Session, *, signal_id: uuid.UUID
) -> ResearchEnrichmentRun | None:
    """This opportunity's enrichment that has not finished, if any."""
    return session.execute(
        select(ResearchEnrichmentRun)
        .where(
            ResearchEnrichmentRun.signal_id == signal_id,
            ResearchEnrichmentRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(ResearchEnrichmentRun.created_at.desc())
        .limit(1)
    ).scalar()


def latest_enrichment(
    session: Session, *, signal_id: uuid.UUID
) -> ResearchEnrichmentRun | None:
    """The most recent enrichment attempt for this opportunity, any status."""
    return session.execute(
        select(ResearchEnrichmentRun)
        .where(ResearchEnrichmentRun.signal_id == signal_id)
        .order_by(ResearchEnrichmentRun.created_at.desc())
        .limit(1)
    ).scalar()


def start_enrichment(
    session: Session,
    *,
    signal: Signal,
    now: Callable[[], datetime] = _utcnow,
) -> tuple[ResearchEnrichmentRun, bool]:
    """Claim one enrichment for this opportunity.

    Returns `(run, already_running)`. Makes NO provider call, so it is
    safe inside an HTTP request and cannot be the thing that takes
    minutes.

    Deduplication is the point: a second click while one is in flight
    returns the running job untouched. The lookup below is an
    optimization, not the guarantee -- two concurrent requests can both
    find nothing and both try to insert, and the partial unique index
    `uq_research_enrichment_runs_active_signal` fails the loser, which
    then reads the winner's row. The race costs one rolled-back INSERT
    and can never produce two provider runs, because no provider call
    happens anywhere in this function.
    """
    existing = active_enrichment(session, signal_id=signal.id)
    if existing is not None:
        return existing, True

    run = ResearchEnrichmentRun(
        signal_id=signal.id, status=ResearchEnrichmentStatus.QUEUED
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # The rollback is required, not cosmetic: the session is unusable
        # for further queries until the failed transaction is discarded.
        session.rollback()
        winner = active_enrichment(session, signal_id=signal.id)
        if winner is None:
            # The constraint fired but nothing explains it -- a different
            # violation entirely. Raising beats returning a run that does
            # not correspond to what the caller asked for.
            raise
        logger.info(
            "research_enrichment_claim_lost_race",
            extra={"signal_id": str(signal.id), "enrichment_id": str(winner.id)},
        )
        return winner, True

    session.refresh(run)
    logger.info(
        "[research-enrichment] queued id=%s signal=%s", run.id, signal.id
    )
    return run, False


# -- running ----------------------------------------------------------------


def execute_enrichment(
    session: Session,
    *,
    enrichment_id: uuid.UUID,
    collector: ResearchCollector,
    matcher: SemanticMatcher | None = None,
    generator: ResearchQueryGenerator | None = None,
    now: Callable[[], datetime] = _utcnow,
    acquisition_budget_seconds: float = RESEARCH_ACQUISITION_BUDGET_SECONDS,
) -> ResearchEnrichmentRun:
    """Run one claimed enrichment to a terminal state.

    Every exit writes a terminal status. A crash between QUEUED and here
    leaves the row RUNNING or QUEUED forever, which is the honest failure
    mode of an in-process executor and is why the status is persisted at
    all -- see app.research_intelligence.background.

    The plan is validated BEFORE the collector is touched, so a junk plan
    fails the job without spending anything.
    """
    run = session.get(ResearchEnrichmentRun, enrichment_id)
    if run is None:
        raise LookupError(f"research enrichment {enrichment_id} not found")
    if run.status not in ACTIVE_STATUSES:
        return run

    signal = session.get(Signal, run.signal_id)
    if signal is None:  # pragma: no cover - defensive
        return _fail(session, run, "the opportunity no longer exists", now=now)

    run.status = ResearchEnrichmentStatus.RUNNING
    run.started_at = now()
    session.commit()
    logger.info("[research-enrichment] running id=%s", run.id)

    try:
        plan = build_plan(market_context_from_signal(signal), generator)
    except ResearchPlanRejectedError as exc:
        logger.info(
            "research_enrichment_plan_rejected",
            extra={"enrichment_id": str(run.id), "reason": str(exc)},
        )
        return _fail(session, run, str(exc), now=now)
    except ResearchQueryGenerationError as exc:
        # The generator itself refused -- the wording matched nothing and
        # it declined to pad the plan. Same outcome, different author.
        return _fail(session, run, str(exc), now=now)

    logger.info(
        "[research-enrichment] generated %d queries id=%s", len(plan.queries), run.id
    )
    # Seed the per-query state so a client polling between RUNNING and the
    # first search already sees the three queries as pending, rather than
    # an empty list it would have to guess the shape of.
    _publish_progress(
        session,
        run,
        [ResearchQueryExecution(query=query) for query in plan.queries],
    )

    started = now()
    try:
        result = enrich_opportunity_with_research(
            session,
            signal=signal,
            collector=collector,
            generator=_FixedPlanGenerator(plan),
            matcher=matcher,
            on_progress=lambda executions: _publish_progress(session, run, executions),
            acquisition_budget_seconds=acquisition_budget_seconds,
        )
    except Exception as exc:
        # Anything the orchestration could not absorb. Per-query provider
        # failures are already handled inside it and do not reach here.
        logger.exception(
            "research_enrichment_failed", extra={"enrichment_id": str(run.id)}
        )
        return _fail(session, run, f"{type(exc).__name__}: {exc}", now=now)

    # -- the partial-result policy ------------------------------------
    #
    # Every search failing is a failed enrichment: there is nothing to
    # show and the user must be able to retry. But ONE search returning
    # is enough to be useful, and discarding 25 real papers because a
    # third query timed out would be the worst of both worlds -- money
    # spent, nothing delivered.
    if result.queries and not result.successful_queries:
        return _fail(
            session,
            run,
            _all_searches_failed_message(result),
            now=now,
            query_states=_states_from_result(result),
        )

    duration = round((now() - started).total_seconds(), 2)
    run.status = ResearchEnrichmentStatus.SUCCEEDED
    run.completed_at = now()
    run.error = None
    # Set alongside SUCCEEDED, never instead of it.
    run.warning = result.acquisition_warning
    run.query_states = _states_from_result(result)
    session.commit()
    session.refresh(run)
    logger.info(
        "[research-enrichment] succeeded id=%s papers=%d matches=%d duration=%ss%s",
        run.id,
        result.candidate_paper_count,
        result.matches_created + result.matches_updated,
        duration,
        f" warning={run.warning!r}" if run.warning else "",
    )
    return run


class _FixedPlanGenerator:
    """Hands the orchestration the plan this module already validated.

    Without it the plan would be generated twice -- once for the quality
    gate and once inside the orchestration -- and the gate would be
    guarding a different plan from the one that actually runs.
    """

    def __init__(self, plan: ResearchQueryPlan) -> None:
        self._plan = plan

    def generate(self, context: MarketContext) -> ResearchQueryPlan:
        return self._plan


def _publish_progress(
    session: Session,
    run: ResearchEnrichmentRun,
    executions: Sequence[ResearchQueryExecution],
) -> None:
    """Persist per-query progress so the browser can poll something true.

    Called on the ORCHESTRATION's thread, never a search worker -- the
    runner guarantees that, and it is what makes writing through this
    Session safe.

    Committed immediately and on its own: progress that is only visible
    after the run finishes is not progress. A failure to write it is
    logged and swallowed, because losing a progress tick must never fail
    an enrichment that is otherwise going fine.
    """
    try:
        run.query_states = [execution.to_state() for execution in executions]
        session.commit()
    except Exception:  # pragma: no cover - progress is best-effort
        logger.warning(
            "research_enrichment_progress_write_failed",
            extra={"enrichment_id": str(run.id)},
            exc_info=True,
        )
        session.rollback()


def _states_from_result(result: ResearchEnrichmentResult) -> list[dict[str, object]]:
    """Per-query state rebuilt from the finished orchestration result.

    The final write comes from the result rather than the live executions
    so the persisted state carries the post-ingestion paper counts, which
    only exist once ingestion has run.
    """
    return [
        {
            "query": outcome.query,
            "status": outcome.status.value,
            "provider_job_id": outcome.provider_job_id,
            "records_received": outcome.records_received,
            "papers_returned": outcome.papers_returned,
            "error": outcome.error,
            "elapsed_seconds": outcome.elapsed_seconds,
        }
        for outcome in result.queries
    ]


def _all_searches_failed_message(result: ResearchEnrichmentResult) -> str:
    """Why a run with no usable search is being failed.

    Names the dominant reason so an operator can tell "the provider is
    down" from "these queries are bad" without opening the logs.
    """
    total = len(result.queries)
    timed_out = len(result.timed_out_queries)
    if timed_out == total:
        return (
            f"all {total} research searches timed out before returning; "
            "the provider jobs may still be running and this can be retried"
        )
    if timed_out:
        return (
            f"no research search returned: {timed_out} of {total} timed out and "
            f"{total - timed_out} failed"
        )
    return f"all {total} research searches failed before returning any papers"


def _fail(
    session: Session,
    run: ResearchEnrichmentRun,
    message: str,
    *,
    now: Callable[[], datetime],
    query_states: list[dict[str, object]] | None = None,
) -> ResearchEnrichmentRun:
    """Close the enrichment as FAILED, keeping why."""
    run.status = ResearchEnrichmentStatus.FAILED
    run.completed_at = now()
    run.error = message
    if query_states is not None:
        run.query_states = query_states
    session.commit()
    session.refresh(run)
    logger.warning("[research-enrichment] failed id=%s reason=%s", run.id, message)
    return run


# -- reconciliation ---------------------------------------------------------

# How long an enrichment may stay active before it is considered
# abandoned. Comfortably above the bounded acquisition budget plus a
# semantic-matching pass, so a genuinely slow run is never killed by it;
# the only rows this catches are ones whose executor no longer exists.
STALE_ENRICHMENT_AFTER_SECONDS = 900.0


def reconcile_stale_enrichments(
    session: Session,
    *,
    now: Callable[[], datetime] = _utcnow,
    stale_after_seconds: float = STALE_ENRICHMENT_AFTER_SECONDS,
) -> int:
    """Fail enrichments whose executor died, and return how many.

    FastAPI BackgroundTasks live in the Uvicorn process. A reload, a
    crash, or a deploy kills the wait while the Bright Data jobs carry on
    remotely -- leaving a row RUNNING with nothing left to finish it. The
    partial unique index then blocks the opportunity from ever being
    enriched again, so a stuck row is not cosmetic: it permanently
    disables the feature for that opportunity.

    This is the reconciler for that, and it is deliberately the dumbest
    one that works: age it out. It does NOT resume by provider job id --
    that is a larger piece of work -- it just makes the row terminal so
    the user can retry, which is the behaviour the UI needs.

    Safe to call on every status read: it only touches rows older than
    the budget, so an in-flight enrichment is never affected.
    """
    cutoff = now() - timedelta(seconds=stale_after_seconds)
    stale = list(
        session.execute(
            select(ResearchEnrichmentRun).where(
                ResearchEnrichmentRun.status.in_(ACTIVE_STATUSES),
                ResearchEnrichmentRun.created_at < cutoff,
            )
        ).scalars()
    )
    for run in stale:
        run.status = ResearchEnrichmentStatus.FAILED
        run.completed_at = now()
        run.error = (
            "the worker running this enrichment stopped before it finished "
            "(most likely a backend restart); no provider job was cancelled, "
            "and this can be retried"
        )
        logger.warning(
            "[research-enrichment] reconciled stale run id=%s signal=%s",
            run.id,
            run.signal_id,
        )
    if stale:
        session.commit()
    return len(stale)
