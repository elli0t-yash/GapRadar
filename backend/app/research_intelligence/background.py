"""The LOCAL executor for on-demand research enrichment. Not a job queue.

What this is: a way to run a claimed enrichment in this process after the
HTTP response has been sent, so `POST /research/enrich` returns in
milliseconds while the Bright Data searches and the semantic judging take
as long as they take.

What this is NOT: durable. This runs on FastAPI's BackgroundTasks, which
is an in-process task and nothing more. If the process is killed,
redeployed, or crashes mid-run, the task is gone and the row is left
QUEUED or RUNNING forever. There is no retry and no broker, and pretending
otherwise would be worse than the honest limitation.

What makes that survivable is that the job's state is PERSISTED and the
work is idempotent: papers upsert by arxiv_id and matches upsert by
(signal_id, research_paper_id), so a stranded job can be superseded by a
fresh one without duplicating anything. The user-visible cost of a crash
is a stuck status, not corrupted intelligence.

Replacing this with a real worker later means replacing this file and
nothing else: app.research_intelligence.enrichment knows nothing about how
it is called.
"""

import logging
import uuid

from app.collection.schemas import PollingPolicy
from app.config import Settings, get_settings
from app.db.session import get_session_factory
from app.research_intelligence.execution import RESEARCH_POLL_INTERVAL_SECONDS

logger = logging.getLogger(__name__)

# What ONE research search is allowed to cost this process.
#
# app.collection's DEFAULT_POLLING_POLICY is 10s/900s, sized for a market
# collection nobody is watching. A user IS watching this one, so the
# budget is the research-specific pair: a query that outruns it is
# TIMED_OUT, the enrichment keeps the searches that did return, and the
# provider job is left running rather than cancelled.


def research_polling_policy(settings: Settings | None = None) -> PollingPolicy:
    """The per-query provider budget, from settings.

    Read per call rather than at import so the value can be changed
    without restarting anything that has already imported this module.
    """
    settings = settings or get_settings()
    return PollingPolicy(
        interval_seconds=RESEARCH_POLL_INTERVAL_SECONDS,
        timeout_seconds=settings.RESEARCH_QUERY_TIMEOUT_SECONDS,
    )


def execute_research_enrichment(enrichment_id: uuid.UUID) -> None:
    """Run one claimed enrichment to completion, out of band.

    Opens its own Session and its own provider clients rather than
    borrowing the request's: by the time this runs the response has been
    sent and the request-scoped dependencies are already closed.

    Never raises. This is the top of a background task, so an escaping
    exception would be swallowed by the event loop with no traceback.
    The enrichment's own terminal state is written by execute_enrichment,
    so a failure here does not lose the record of what happened -- and if
    even that fails, the row is left active and the log is the only trace,
    which is why this logs rather than passes.

    The provider clients are constructed HERE, at the outermost edge, so
    that nothing in app.research_intelligence has to import one.
    """
    # Imported inside the function so importing this module never drags a
    # provider SDK into a process that will not enrich anything.
    from app.integrations.brightdata.arxiv import BrightDataArxivCollector
    from app.integrations.brightdata.client import BrightDataClient
    from app.integrations.openai.errors import SemanticJudgeUnavailableError
    from app.integrations.openai.semantic_matcher import OpenAISemanticMatcher
    from app.research_intelligence.enrichment import execute_enrichment

    session = get_session_factory()()
    try:
        try:
            matcher = OpenAISemanticMatcher()
        except SemanticJudgeUnavailableError:
            # No key configured. The orchestration falls back to its
            # deterministic development matcher rather than failing the
            # job, and the log says which one ran.
            logger.warning(
                "research_enrichment_semantic_matcher_unavailable",
                extra={"enrichment_id": str(enrichment_id)},
            )
            matcher = None

        with BrightDataClient() as client:
            execute_enrichment(
                session,
                enrichment_id=enrichment_id,
                # The research-specific budget, NOT the generic 900s
                # collection default. Inheriting that default is what let
                # a single stuck arXiv job hold an enrichment for 14
                # minutes; the collector is unchanged, only the policy it
                # is handed.
                collector=BrightDataArxivCollector(
                    client, polling=research_polling_policy()
                ),
                matcher=matcher,
                acquisition_budget_seconds=(
                    get_settings().RESEARCH_ACQUISITION_BUDGET_SECONDS
                ),
            )
    except Exception:
        logger.exception(
            "research_enrichment_background_execution_failed",
            extra={"enrichment_id": str(enrichment_id)},
        )
    finally:
        session.close()
