"""The single write surface: run the pipeline for one collector.

The frontend never talks to Bright Data. It posts here, this process
holds the provider credentials, and RecallGuard decides what the result
means. The response carries the whole verdict -- collection, evaluation,
and any repair attempt -- so a demo can POST once and then read
/dashboard and /reliability/incidents to see the same story from the
persisted side.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.api.v1.deps import BrightData, DbSession
from app.db.models import Collector
from app.exceptions import AppError
from app.pipeline.schemas import PipelineRunResult
from app.pipeline.service import baseline_from_history, run_pipeline

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    collector_id: uuid.UUID


@router.post("/run", response_model=PipelineRunResult)
def post_pipeline_run(
    payload: PipelineRunRequest,
    session: DbSession,
    client: BrightData,
) -> PipelineRunResult:
    """Collect, evaluate, and repair if RecallGuard asks for a repair.

    Synchronous on purpose: one collection, one verdict, one response.
    The route is a plain `def`, so FastAPI runs this blocking work in a
    worker thread rather than on the event loop.

    The completeness baseline is the collector's own observed history --
    the largest dataset it has previously delivered. A collector with no
    successful history gets no baseline, and completeness is simply not
    evaluated rather than measured against a number nobody observed.
    """
    collector = session.get(Collector, payload.collector_id)
    if collector is None:
        raise AppError(f"collector {payload.collector_id} not found", status_code=404)

    return run_pipeline(
        session,
        client,
        collector=collector,
        baseline=baseline_from_history(session, collector_id=collector.id),
    )
