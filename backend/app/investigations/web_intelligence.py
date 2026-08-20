"""One investigation's web discovery phase, end to end.

    plan family queries
      -> run_web_searches           (bounded, concurrent, never raises)
      -> persist one search run per query        (observability)
      -> persist one hit per (search, url)       (provenance)
      -> dedupe candidates by url across queries
      -> classifier.classify                     (semantics, expensive)
      -> upsert evidence rows                    (one per url)

DISCOVERY ONLY. Nothing here opens a discovered URL. The provider returns
titles, snippets and ranks; those are evidence CANDIDATES, and the
classifier judges them on that basis alone. Reading selected pages deeply
is a separate later stage, and merging it in here would turn one search
into eleven fetches and make the cost of a run unpredictable.

SYNCHRONOUS, DELIBERATELY. The Phase 3 brief sketched an async port. This
backend is sync end to end -- sync SQLAlchemy, sync routes, sync
BackgroundTasks -- and app.research_intelligence.execution already proves
the pattern used here: only the network call runs on a worker thread,
persistence stays on the calling thread. An async island would need an
event loop per run and a second HTTP client lifecycle to buy the same
bound a four-worker pool gives directly. The requirement that actually
matters -- a hard concurrency cap rather than an unbounded fan-out -- is
met either way.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationWebSearchHit,
    InvestigationWebSearchRun,
)
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    WebSearchStatus,
)
from app.research_intelligence.schemas import ResearchSubject
from app.web_intelligence.acquisition import WebSearchProvider
from app.web_intelligence.classification import (
    CompetitorClassifier,
    DemandClassifier,
    classification_failures,
)
from app.web_intelligence.execution import (
    MAX_CONCURRENT_WEB_SEARCHES,
    PlannedWebSearch,
    WebSearchExecution,
    run_web_searches,
)
from app.web_intelligence.schemas import (
    MAX_RESULTS_PER_QUERY,
    SearchLocale,
    WebIntelligenceRecord,
    WebSearchFamily,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WebPhaseResult(BaseModel):
    """What one family's discovery actually did.

    Every number here is measured, and the ones that could be confused
    are kept apart:

        queries_total     -> searches the plan called for
        queries_completed -> searches that reached a terminal state
        queries_succeeded -> searches the provider actually served
        candidates        -> DISTINCT urls across every successful search
        judged            -> candidates the classifier returned a verdict on
        accepted          -> verdicts worth keeping (not IRRELEVANT)

    `candidates` is distinct, never the sum of per-query counts: a page
    found by two searches is one page, and summing is how a UI comes to
    report twice the evidence it has.
    """

    model_config = ConfigDict(frozen=True)

    family: WebSearchFamily
    queries_total: int = 0
    queries_completed: int = 0
    queries_succeeded: int = 0
    candidates: int = 0
    judged: int = 0
    accepted: int = 0
    # Verdicts by classification value, so the run can report "4 direct,
    # 5 adjacent" without a second pass over the database.
    by_classification: dict[str, int] = Field(default_factory=dict)
    # How many times the CLASSIFIER itself failed, as distinct from how
    # many pages it judged irrelevant. Non-zero is the only honest
    # evidence that an empty result says nothing about the market.
    classification_failures: int = 0

    @property
    def queries_failed(self) -> int:
        return self.queries_completed - self.queries_succeeded

    @property
    def is_partial(self) -> bool:
        """Some searches worked and some did not.

        Not the same as any search failing: a family where EVERY search
        failed is not partial, it is a failed family, and the run treats
        the two differently.
        """
        return 0 < self.queries_succeeded < self.queries_completed

    @property
    def is_failed(self) -> bool:
        """Every planned search failed, so nothing was learned."""
        return self.queries_total > 0 and self.queries_succeeded == 0


@dataclass
class _Candidate:
    """One distinct URL, with every search that found it."""

    record: WebIntelligenceRecord
    found_by: list[str] = field(default_factory=list)


def discover_web_evidence(
    session: Session,
    *,
    subject: ResearchSubject,
    investigation_id: uuid.UUID,
    run_id: uuid.UUID | None,
    searches: list[PlannedWebSearch],
    provider: WebSearchProvider,
    locale: SearchLocale,
    demand_classifier: DemandClassifier,
    competitor_classifier: CompetitorClassifier,
    provider_name: str = "unknown",
    provider_product: str = "unknown",
    limit: int = MAX_RESULTS_PER_QUERY,
    max_concurrency: int = MAX_CONCURRENT_WEB_SEARCHES,
    commit: bool = True,
    now: Callable[[], datetime] = _utcnow,
) -> dict[WebSearchFamily, WebPhaseResult]:
    """Run both web families and persist everything they produced.

    PARTIAL SUCCESS IS PRESERVED, ALWAYS. A failed search costs its own
    evidence and nothing else: the searches that returned are persisted,
    classified and kept. Discarding real findings because one query of
    six failed would be the worst of both worlds -- money spent, nothing
    delivered -- and the per-family result reports the gap rather than
    hiding it.

    Returns one result per family that was planned. A family with no
    planned searches is absent rather than reported as zero, because
    "not asked" and "asked and found nothing" are different facts.
    """
    executions = run_web_searches(
        searches,
        provider=provider,
        locale=locale,
        limit=limit,
        max_concurrency=max_concurrency,
        now=now,
    )

    results: dict[WebSearchFamily, WebPhaseResult] = {}
    for family in WebSearchFamily:
        family_executions = [e for e in executions if e.family is family]
        if not family_executions:
            continue
        results[family] = _persist_family(
            session,
            subject=subject,
            investigation_id=investigation_id,
            run_id=run_id,
            family=family,
            executions=family_executions,
            classifier=(
                demand_classifier
                if family is WebSearchFamily.DEMAND
                else competitor_classifier
            ),
            provider_name=provider_name,
            provider_product=provider_product,
        )

    if commit:
        session.commit()
    return results


def _persist_family(
    session: Session,
    *,
    subject: ResearchSubject,
    investigation_id: uuid.UUID,
    run_id: uuid.UUID | None,
    family: WebSearchFamily,
    executions: list[WebSearchExecution],
    classifier: DemandClassifier | CompetitorClassifier,
    provider_name: str,
    provider_product: str,
) -> WebPhaseResult:
    """Write one family's executions, hits, and judgements."""
    candidates: dict[str, _Candidate] = {}
    succeeded = 0

    for execution in executions:
        search_run = _record_execution(
            session,
            investigation_id=investigation_id,
            run_id=run_id,
            family=family,
            execution=execution,
            provider_name=provider_name,
            provider_product=provider_product,
        )
        if not execution.succeeded:
            continue
        succeeded += 1

        for record in execution.records:
            session.add(
                InvestigationWebSearchHit(
                    investigation_web_search_run_id=search_run.id,
                    url=record.url,
                    domain=record.domain,
                    title=record.title,
                    snippet=record.snippet,
                    position=record.position,
                    published_at=record.published_at,
                )
            )
            # CROSS-QUERY DEDUPE happens here and only here. The hit rows
            # above keep every query that found the page; the candidate
            # is judged once.
            existing = candidates.get(record.url)
            if existing is None:
                candidates[record.url] = _Candidate(
                    record=record, found_by=[record.query]
                )
            else:
                existing.found_by.append(record.query)

    session.flush()

    failures_before = classification_failures(classifier)
    judged = 0
    accepted = 0
    by_classification: dict[str, int] = {}

    for candidate in candidates.values():
        verdict = classifier.classify(subject=subject, record=candidate.record)
        if verdict is None:
            # DECLINED, not judged irrelevant. Nothing is written: a
            # classifier that could not answer has said nothing about
            # this page, and storing a default would invent a verdict.
            continue
        judged += 1
        key = verdict.classification.value
        by_classification[key] = by_classification.get(key, 0) + 1
        if not verdict.classification.is_accepted:
            # An IRRELEVANT page is judged and counted, and deliberately
            # NOT stored as evidence. Keeping it would put pages about
            # something else into a list a founder reads as findings.
            continue
        accepted += 1
        _upsert_evidence(
            session,
            investigation_id=investigation_id,
            family=family,
            record=candidate.record,
            verdict=verdict,
        )

    session.flush()

    return WebPhaseResult(
        family=family,
        queries_total=len(executions),
        # Every execution reaches a terminal state -- the runner
        # guarantees it -- so completed equals planned once this returns.
        queries_completed=len(executions),
        queries_succeeded=succeeded,
        candidates=len(candidates),
        judged=judged,
        accepted=accepted,
        by_classification=by_classification,
        classification_failures=classification_failures(classifier)
        - failures_before,
    )


def _record_execution(
    session: Session,
    *,
    investigation_id: uuid.UUID,
    run_id: uuid.UUID | None,
    family: WebSearchFamily,
    execution: WebSearchExecution,
    provider_name: str,
    provider_product: str,
) -> InvestigationWebSearchRun:
    """Persist ONE provider execution, successful or not.

    A failed search gets a row too. Without it the run would show three
    planned searches and two executions, and nobody could tell whether
    the third was never attempted or was attempted and refused.
    """
    if execution.succeeded:
        status = WebSearchStatus.SUCCEEDED
    elif execution.timed_out:
        status = WebSearchStatus.TIMED_OUT
    else:
        status = WebSearchStatus.FAILED

    search_run = InvestigationWebSearchRun(
        investigation_id=investigation_id,
        investigation_run_id=run_id,
        family=family.value,
        query=execution.query,
        provider=provider_name,
        product=provider_product,
        locale_country=execution.locale.country,
        locale_language=execution.locale.language,
        status=status,
        records_returned=execution.records_returned,
        latency_ms=execution.latency_ms,
        error=execution.error,
        # Null unless the provider issues one. The synchronous SERP API
        # does not, and fabricating an id would make an untraceable
        # request look traceable.
        provider_request_id=None,
    )
    session.add(search_run)
    session.flush()
    return search_run


def _upsert_evidence(
    session: Session,
    *,
    investigation_id: uuid.UUID,
    family: WebSearchFamily,
    record: WebIntelligenceRecord,
    verdict: object,
) -> None:
    """Write one verdict. One row per (investigation, url), forever.

    Re-running replaces the previous judgement rather than stacking a
    second, near-identical claim, so "what does GapRadar think of this
    page for this investigation" always has exactly one answer. The hit
    rows keep the history of how it was found.
    """
    model = (
        InvestigationDemandEvidence
        if family is WebSearchFamily.DEMAND
        else InvestigationCompetitor
    )
    existing = session.execute(
        select(model).where(
            model.investigation_id == investigation_id,
            model.url == record.url,
        )
    ).scalar_one_or_none()

    shared: dict[str, object] = {
        "domain": record.domain,
        "snippet": record.snippet,
        "classification": verdict.classification,  # type: ignore[attr-defined]
        "relevance_score": verdict.relevance_score,  # type: ignore[attr-defined]
        "reason": verdict.reason,  # type: ignore[attr-defined]
    }
    if family is WebSearchFamily.DEMAND:
        shared["title"] = record.title
        shared["published_at"] = record.published_at
    else:
        # The display identity, which is the page title unless the
        # classifier found something genuinely better.
        shared["name"] = verdict.name  # type: ignore[attr-defined]

    if existing is not None:
        for attribute, value in shared.items():
            setattr(existing, attribute, value)
        session.flush()
        return

    session.add(
        model(investigation_id=investigation_id, url=record.url, **shared)
    )
    session.flush()


def searches_that_found(
    session: Session, *, investigation_id: uuid.UUID, url: str
) -> list[InvestigationWebSearchRun]:
    """Every search that returned this URL for this investigation.

    THE PROVENANCE QUERY. How many independent search directions
    converged on one page is one of the few honest strength signals
    discovery produces, and it only exists because hits are kept per
    search rather than collapsed into the evidence row.
    """
    return list(
        session.execute(
            select(InvestigationWebSearchRun)
            .join(
                InvestigationWebSearchHit,
                InvestigationWebSearchHit.investigation_web_search_run_id
                == InvestigationWebSearchRun.id,
            )
            .where(
                InvestigationWebSearchRun.investigation_id == investigation_id,
                InvestigationWebSearchHit.url == url,
            )
            .order_by(InvestigationWebSearchRun.created_at)
        ).scalars()
    )


__all__ = [
    "CompetitorClassification",
    "DemandEvidenceClassification",
    "WebPhaseResult",
    "discover_web_evidence",
    "searches_that_found",
]
