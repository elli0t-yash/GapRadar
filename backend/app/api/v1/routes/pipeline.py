"""The single write surface: ask for a refresh, then poll for the answer.

The frontend never talks to Bright Data. It posts here, this process
holds the provider credentials, and RecallGuard decides what the result
means.

What changed, and why: this route used to run the whole cycle inline and
answer with the finished verdict, which meant an HTTP request stayed open
for as long as the scrape took -- up to fifteen minutes, and longer when a
repair was attempted. A browser request has no business living that long,
and a client that gives up mid-flight learned nothing while GapRadar kept
working.

So the request now does the part that is genuinely fast -- validate the
collector, claim one logical execution, persist it -- and answers 202 with
the id of that execution. The scraping, evaluation and repair happen out
of band, and the client watches `GET /pipeline/runs/{id}`.

Two things this route still guarantees, both enforced in the executor
rather than here:

- Asking twice does not scrape twice. A collector with an execution
  already in flight gets that execution back, and no second Bright Data
  job is triggered.
- Nothing is trusted early. The claimed execution reports no trust value
  at all until RecallGuard has reached one, and previously trusted
  opportunities keep being served throughout.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, status
from pydantic import BaseModel, ConfigDict

from app.api.v1.deps import DbSession, Scheduler
from app.db.models import Collector, PipelineRun
from app.exceptions import AppError
from app.pipeline.executor import start_pipeline_run
from app.pipeline.schemas import PipelineRunAccepted, PipelineRunRead

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    collector_id: uuid.UUID


@router.post(
    "/run",
    response_model=PipelineRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def post_pipeline_run(
    payload: PipelineRunRequest,
    session: DbSession,
    background: BackgroundTasks,
    schedule: Scheduler,
) -> PipelineRunAccepted:
    """Claim a refresh for one collector and return immediately.

    202, not 200: nothing has been collected, evaluated or decided when
    this returns. The only work done inside the request is a collector
    lookup and one INSERT, neither of which touches Bright Data.

    An execution already in flight for this collector is returned as-is
    with `already_running: true`, and nothing new is scheduled -- the
    driver that owns it is still driving it. Restarting a genuinely
    abandoned execution is the daily job's resume pass, not a side effect
    of an impatient client retrying.
    """
    collector = session.get(Collector, payload.collector_id)
    if collector is None:
        raise AppError(f"collector {payload.collector_id} not found", status_code=404)

    run, already_running = start_pipeline_run(session, collector=collector)

    if not already_running:
        # Runs after the response is sent, in this process. Local only,
        # and deliberately not treated as durable -- see
        # app.pipeline.background for what happens when it is lost.
        background.add_task(schedule, run.id)

    return PipelineRunAccepted(
        pipeline_run_id=run.id,
        collector_id=run.collector_id,
        status=run.status,
        already_running=already_running,
    )


@router.get("/runs/{pipeline_run_id}", response_model=PipelineRunRead)
def get_pipeline_run(pipeline_run_id: uuid.UUID, session: DbSession) -> PipelineRun:
    """Where one claimed execution has got to.

    Read-only, cheap, and safe to poll. `status` is the execution's own
    lifecycle; `trusted` and `reliability_state` stay null until
    RecallGuard has actually judged the data, so a client polling a
    refresh in flight never sees a verdict that has not been reached.
    """
    run = session.get(PipelineRun, pipeline_run_id)
    if run is None:
        raise AppError(f"pipeline run {pipeline_run_id} not found", status_code=404)
    return run
