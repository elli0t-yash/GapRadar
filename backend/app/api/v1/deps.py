"""Shared FastAPI dependencies for the v1 API.

The database session dependency stays app.db.session.get_db -- this
module adds only what the API layer needs on top of it.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.integrations.brightdata.client import BrightDataClient


def get_brightdata_client() -> Iterator[BrightDataClient]:
    """One Bright Data client per request, closed when the request ends.

    Credentials come from the application settings, never from the
    request: the frontend talks to this API and this API talks to Bright
    Data, so no browser ever holds a provider token. Overridden in tests
    with a client bound to a mock transport, which is what keeps the test
    suite from making a real provider call.
    """
    with BrightDataClient() as client:
        yield client


# The existing session dependency, wrapped as an annotation so routes
# declare it without a call in a default argument.
DbSession = Annotated[Session, Depends(get_db)]
BrightData = Annotated[BrightDataClient, Depends(get_brightdata_client)]


# Callable that takes a claimed pipeline run id and arranges for it to be
# executed out of band. Returns immediately; it never does the work.
PipelineScheduler = Callable[[uuid.UUID], None]


def get_pipeline_scheduler() -> PipelineScheduler:
    """How the API hands claimed work to the local executor.

    A dependency rather than a direct import so a test can substitute a
    recorder and prove the route returns without doing the work -- which
    is exactly the property the async change exists to establish. Swapping
    the local executor for a real worker later is a change to this one
    function.

    Imported inside the function on purpose: app.pipeline reaches
    app.collection, which reaches app.schemas, which reaches back into
    app.recallguard. Importing the executor while this module is still
    loading would force that cycle to resolve in the wrong order and
    break app startup. The API layer needs the callable, not the module.
    """
    from app.pipeline.background import execute_pipeline_run

    return execute_pipeline_run


Scheduler = Annotated[PipelineScheduler, Depends(get_pipeline_scheduler)]
