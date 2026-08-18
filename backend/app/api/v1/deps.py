"""Shared FastAPI dependencies for the v1 API.

The database session dependency stays app.db.session.get_db -- this
module adds only what the API layer needs on top of it.
"""

from collections.abc import Iterator
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
