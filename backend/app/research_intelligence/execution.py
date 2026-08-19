"""Running the three research searches without letting one of them hang.

The problem this solves, from a real run: three provider searches were
issued strictly one after another, and each waited on the previous. The
observed job durations were 108s, 118s, 195s, 341s, 356s and 366s, so a
single enrichment took 14-16 minutes wall clock and the UI sat on
"Analysing the research frontier..." for all of it. One slow Scraper
Studio job held the entire product hostage.

Two changes fix that, and neither touches the collector:

1. The searches run CONCURRENTLY, so the enrichment costs roughly the
   slowest query rather than the sum of all three.
2. Each search gets a bounded local budget, and the acquisition as a
   whole gets a bounded total budget. A query that outruns either is
   TIMED_OUT and the enrichment continues with what did come back.

Only `collector.search()` runs on a worker thread. It is pure network and
touches no Session -- ingestion stays on the calling thread, in plan
order, because a SQLAlchemy Session is not thread-safe and making it
concurrent would trade a slow demo for a corrupt one.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.enums import ResearchQueryStatus
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
    ResearchCollector,
)

logger = logging.getLogger(__name__)

# How long ONE research query may occupy this process.
#
# Chosen from measured Bright Data arXiv runs, not from the generic
# 900s collection default: 101s, 108s, 110s, 118s, 186s, 195s, 341s,
# 356s, 366s, plus one job still at 1/16 pages after 12 minutes. A 60-90s
# cap -- the first instinct -- would have timed out the MAJORITY of those
# genuinely-successful searches and made partial results the normal case.
# 150s keeps every observed sub-200s job while cutting off the long tail
# that made the UI unusable.
RESEARCH_QUERY_TIMEOUT_SECONDS = 150.0

# How long the whole acquisition phase may take, across all queries.
# Because the queries run concurrently this is a backstop rather than a
# sum: three 150s queries finish in ~150s, not 450s. The extra headroom
# covers stagger in trigger time.
RESEARCH_ACQUISITION_BUDGET_SECONDS = 180.0

# How often each search asks the provider whether it is done. Tighter
# than the 10s generic default so a fast job is noticed promptly and the
# per-query budget is spent waiting, not sleeping.
RESEARCH_POLL_INTERVAL_SECONDS = 5.0

# How often the calling thread publishes progress while the searches run.
# Independent of the provider poll interval: this is GapRadar telling its
# own frontend what it knows, not asking Bright Data anything.
PROGRESS_PUBLISH_INTERVAL_SECONDS = 2.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ResearchQueryExecution:
    """The observable state of ONE research query.

    Mutable and dataclass rather than a frozen pydantic model because it
    is written to as the query progresses and is snapshotted into the
    enrichment row on every transition -- it is the thing the frontend
    polls to render "2/3 complete" honestly.
    """

    query: str
    status: ResearchQueryStatus = ResearchQueryStatus.PENDING
    provider_job_id: str | None = None
    records_received: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Populated only on success; the raw provider result awaiting
    # ingestion on the calling thread.
    result: ResearchCollectionResult | None = field(default=None, repr=False)
    # Set once the ingestion pass has run, so the enrichment can report
    # papers per query rather than only records per query.
    search_run_id: uuid.UUID | None = None
    papers_returned: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_QUERY_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status is ResearchQueryStatus.SUCCEEDED

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or _utcnow()
        return round((end - self.started_at).total_seconds(), 2)

    def to_state(self) -> dict[str, object]:
        """The JSON-safe snapshot persisted on the enrichment row."""
        return {
            "query": self.query,
            "status": self.status.value,
            "provider_job_id": self.provider_job_id,
            "records_received": self.records_received,
            "papers_returned": self.papers_returned,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": self.elapsed_seconds,
        }


_TERMINAL_QUERY_STATUSES = frozenset(
    {
        ResearchQueryStatus.SUCCEEDED,
        ResearchQueryStatus.FAILED,
        ResearchQueryStatus.TIMED_OUT,
    }
)

# Called after every query state transition, with the full list, so the
# caller can persist progress. Never raises into the runner.
ProgressCallback = Callable[[Sequence[ResearchQueryExecution]], None]


def run_searches_concurrently(
    queries: Sequence[str],
    *,
    collector: ResearchCollector,
    on_progress: ProgressCallback | None = None,
    total_budget_seconds: float = RESEARCH_ACQUISITION_BUDGET_SECONDS,
    now: Callable[[], datetime] = _utcnow,
) -> list[ResearchQueryExecution]:
    """Run every query at once and return each one's outcome.

    Returns in the SAME ORDER as `queries`, whatever order they finished
    in, so ingestion and dedupe stay deterministic across runs.

    Never raises for a provider problem. A query that fails, times out,
    or dies unexpectedly comes back as a terminal execution carrying the
    reason; it is the caller's job -- and the partial-result policy's --
    to decide whether the surviving queries are enough.

    The total budget is a backstop over the per-query budgets the
    collector already enforces. If it expires while a worker is still
    polling, that query is recorded TIMED_OUT and the enrichment moves
    on. The worker is NOT killed -- Python cannot interrupt it -- but it
    is abandoned: it holds no Session, writes nothing, and exits on its
    own deadline. The provider job likewise keeps running on Bright
    Data's side and is never cancelled.
    """
    executions = [ResearchQueryExecution(query=query) for query in queries]
    if not executions:
        return executions

    def report() -> None:
        if on_progress is None:
            return
        try:
            on_progress(executions)
        except Exception:  # pragma: no cover - progress must never break a run
            logger.warning("research_progress_callback_failed", exc_info=True)

    def run_one(execution: ResearchQueryExecution) -> None:
        # Runs on a worker thread. It mutates ONLY its own execution and
        # never touches a Session, a callback, or another execution --
        # that is what makes the concurrency safe. Progress is published
        # by the calling thread below, which reads these fields.
        execution.started_at = now()
        execution.status = ResearchQueryStatus.RUNNING
        try:
            execution.result = collector.search(execution.query)
        except ResearchCollectionError as exc:
            execution.error = exc.message
            execution.status = _classify_failure(exc)
        except Exception as exc:  # noqa: BLE001 - a collector may misbehave
            # A collector that raises something outside its contract must
            # not take the enrichment down with it.
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.status = ResearchQueryStatus.FAILED
        else:
            execution.records_received = len(execution.result.records)
            execution.provider_job_id = execution.result.provider_job_id
            execution.status = ResearchQueryStatus.SUCCEEDED
        finally:
            # `status` is assigned AFTER the fields it describes and
            # BEFORE finished_at, so a reader on the calling thread that
            # sees a terminal status also sees the data behind it.
            execution.finished_at = now()

    started = now()
    # NOT a `with` block. ThreadPoolExecutor.__exit__ calls
    # shutdown(wait=True), which joins every worker -- so a query hanging
    # past the budget would still hold this function for its full
    # duration and the budget would enforce nothing. Measured: a 30s hang
    # under a 2s budget still took 30s. The executor is therefore shut
    # down explicitly, without waiting.
    pool = ThreadPoolExecutor(
        max_workers=len(executions), thread_name_prefix="research-search"
    )
    try:
        pending: set[Future[None]] = {
            pool.submit(run_one, execution) for execution in executions
        }
        while pending:
            elapsed = (now() - started).total_seconds()
            remaining = total_budget_seconds - elapsed
            if remaining <= 0:
                break
            # Wake up regularly even while nothing finishes, so progress
            # is published on the calling thread at a useful cadence
            # rather than only when a query completes.
            _done, pending = wait(
                pending, timeout=min(PROGRESS_PUBLISH_INTERVAL_SECONDS, remaining)
            )
            report()
    finally:
        # Abandon, never join. A worker still polling Bright Data is left
        # to end on its own per-query deadline: it holds no Session and
        # writes nothing, so nothing downstream can observe it. Python
        # cannot interrupt a thread, and the provider job is deliberately
        # not cancelled either way.
        pool.shutdown(wait=False, cancel_futures=True)

    elapsed = (now() - started).total_seconds()
    for execution in executions:
        if execution.is_terminal:
            continue
        # The total budget expired before this query reported. Its own
        # per-query budget will end it shortly; from the enrichment's
        # point of view it produced nothing in the time allowed.
        execution.status = ResearchQueryStatus.TIMED_OUT
        execution.finished_at = execution.finished_at or now()
        execution.error = (
            f"acquisition budget of {total_budget_seconds:.0f}s elapsed after "
            f"{elapsed:.0f}s while this search was still running; "
            "the provider job was not cancelled"
        )
        logger.warning(
            "[research-enrichment] query timed_out on total budget query=%r elapsed=%.0fs",
            execution.query,
            elapsed,
        )

    if on_progress is not None:
        on_progress(executions)
    return executions


def _classify_failure(exc: ResearchCollectionError) -> ResearchQueryStatus:
    """Tell a local timeout apart from an outright provider failure.

    The collector reports both through one exception type, but the two
    mean different things to an operator: a timeout is "still running,
    we stopped waiting", a failure is "this search will never return".
    The message is the only signal available without changing the
    collector's contract, which is owned elsewhere.
    """
    message = exc.message.lower()
    if "elapsed" in message or "timeout" in message or "timed out" in message:
        return ResearchQueryStatus.TIMED_OUT
    return ResearchQueryStatus.FAILED


__all__ = [
    "PROGRESS_PUBLISH_INTERVAL_SECONDS",
    "RESEARCH_ACQUISITION_BUDGET_SECONDS",
    "RESEARCH_POLL_INTERVAL_SECONDS",
    "RESEARCH_QUERY_TIMEOUT_SECONDS",
    "ProgressCallback",
    "ResearchQueryExecution",
    "run_searches_concurrently",
]
