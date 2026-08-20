"""Running several web searches at once, under a hard bound.

Mirrors app.research_intelligence.execution's discipline, which this
codebase has already proven: only the NETWORK call runs on a worker
thread, and it touches no Session. Persistence happens on the calling
thread, in plan order, because a SQLAlchemy Session is not thread-safe
and making it concurrent would trade a slow run for a corrupt one.

THE BOUND IS THE POINT. Up to six web searches can be planned for one
investigation, and an unbounded fan-out would open six simultaneous
billable provider requests from a single click. Four at a time keeps the
wall clock close to the slowest search while capping how much a runaway
plan can spend at once.

Synchronous, deliberately -- see the module note in
app.investigations.web_intelligence.
"""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.web_intelligence.acquisition import (
    WebSearchError,
    WebSearchProvider,
    WebSearchTimeoutError,
)
from app.web_intelligence.schemas import (
    MAX_RESULTS_PER_QUERY,
    SearchLocale,
    WebIntelligenceRecord,
    WebSearchFamily,
)

logger = logging.getLogger(__name__)

# How many provider requests may be in flight at once, across all
# families. Configurable through the orchestration's argument; four is
# the bound the acquisition pilot recommended.
MAX_CONCURRENT_WEB_SEARCHES = 4


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WebSearchExecution:
    """The observable outcome of ONE web search.

    Carries `succeeded` explicitly rather than letting callers infer it
    from `len(records)`. A search that returned nothing and a search that
    failed both have zero records, and everything downstream depends on
    telling them apart.
    """

    query: str
    family: WebSearchFamily
    locale: SearchLocale
    succeeded: bool = False
    # Only ever populated on success. An empty list on a successful
    # execution is a real answer: the engine looked and found nothing.
    records: list[WebIntelligenceRecord] = field(default_factory=list)
    # Only ever populated on failure, and written for a person to read:
    # never a credential, a URL, or a provider payload.
    error: str | None = None
    timed_out: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def latency_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def records_returned(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class PlannedWebSearch:
    """One query the planner produced, with the family that motivated it."""

    query: str
    family: WebSearchFamily


def run_web_searches(
    searches: Sequence[PlannedWebSearch],
    *,
    provider: WebSearchProvider,
    locale: SearchLocale,
    limit: int = MAX_RESULTS_PER_QUERY,
    max_concurrency: int = MAX_CONCURRENT_WEB_SEARCHES,
    now: Callable[[], datetime] = _utcnow,
) -> list[WebSearchExecution]:
    """Run every planned search under the bound; return each one's outcome.

    Returns in the SAME ORDER as `searches`, whatever order they finished
    in, so persistence and cross-query dedupe stay deterministic across
    runs.

    NEVER RAISES for a provider problem. A search that fails or times out
    comes back as a terminal execution carrying the reason, and the
    surviving searches keep their records -- discarding real evidence
    because one query of six failed would be the worst of both worlds:
    money spent, nothing delivered.

    An unexpected exception from a provider is caught for the same
    reason, and recorded as a failure rather than being allowed to take
    down the whole phase.
    """
    executions = [
        WebSearchExecution(query=search.query, family=search.family, locale=locale)
        for search in searches
    ]
    if not executions:
        return executions

    bound = max(1, min(max_concurrency, MAX_CONCURRENT_WEB_SEARCHES))

    def run_one(execution: WebSearchExecution) -> None:
        # Runs on a worker thread. It mutates ONLY its own execution and
        # never touches a Session -- that is what makes this safe.
        execution.started_at = now()
        try:
            execution.records = provider.search_web(
                execution.query, limit=limit, locale=execution.locale
            )
            execution.succeeded = True
        except WebSearchTimeoutError as exc:
            execution.error = exc.message
            execution.timed_out = True
        except WebSearchError as exc:
            execution.error = exc.message
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("web_search_unexpected_failure")
            execution.error = f"{type(exc).__name__}: {exc}"
        finally:
            execution.finished_at = now()

    with ThreadPoolExecutor(max_workers=bound) as pool:
        list(pool.map(run_one, executions))

    succeeded = sum(1 for e in executions if e.succeeded)
    logger.info(
        "web_searches_complete",
        extra={
            "planned": len(executions),
            "succeeded": succeeded,
            "failed": len(executions) - succeeded,
            "records": sum(e.records_returned for e in executions),
        },
    )
    return executions
