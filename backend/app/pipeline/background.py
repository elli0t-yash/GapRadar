"""The LOCAL application executor. Not a durable job queue.

What this is: a way to run a claimed pipeline execution in this process,
after the HTTP response has already been sent, so a refresh request
returns in milliseconds while the scrape takes as long as it takes.

What this is NOT: durable. This runs on FastAPI's BackgroundTasks, which
is an in-process task and nothing more. If the process is killed,
redeployed, or crashes, the in-flight task is gone. There is no retry, no
broker, and no at-least-once delivery, and pretending otherwise would be
worse than the honest limitation.

What makes that survivable is that correctness does not depend on it.
Every step of an execution is committed to PostgreSQL as it happens, and
the provider's collection id is persisted before any waiting begins, so
losing the task loses only the *driving*, never the work:

    app.pipeline.executor.resume_unfinished_pipeline_runs

picks up whatever this process abandoned and rejoins the same Bright Data
collection. That runs from the daily job, so a restart costs a delay
rather than a duplicate scrape or a lost dataset.

Replacing this with a real worker later means replacing this file and
nothing else: the executor's step functions know nothing about how they
are called.
"""

import logging
import uuid

from app.db.session import get_session_factory
from app.integrations.brightdata.client import BrightDataClient
from app.pipeline.executor import drive_pipeline_run

logger = logging.getLogger(__name__)


def execute_pipeline_run(pipeline_run_id: uuid.UUID) -> None:
    """Drive one claimed execution to completion, out of band.

    Opens its own Session and BrightDataClient rather than borrowing the
    request's: by the time this runs the response has been sent and the
    request-scoped dependencies are already closed.

    Never raises. This is the top of a background task, so an escaping
    exception would be swallowed by the event loop with no traceback;
    logging it here is the only way it is ever seen. The execution's own
    state is already persisted by the executor, so a failure here does
    not lose the record of what happened.
    """
    session = get_session_factory()()
    try:
        with BrightDataClient() as client:
            drive_pipeline_run(session, client, pipeline_run_id=pipeline_run_id)
    except Exception:
        logger.exception(
            "pipeline_run_background_execution_failed",
            extra={"pipeline_run_id": str(pipeline_run_id)},
        )
    finally:
        session.close()
