"""Persists and reads independent investigations. Talks to nobody else.

PROVIDER-FREE BY CONSTRUCTION. Nothing in this module reaches Bright
Data, OpenAI, the web, or any other network. Creating an investigation
records a sentence a user typed; it does not research it, score it, or
look anything up. That is the whole point of the split: the write path
that a browser can trigger must be cheap and free, and the expensive
path must be something a user asks for explicitly and separately.

Provenance, restated because it is easy to lose: an Investigation is a
USER HYPOTHESIS. It is never written to `signals`, and nothing here
grants it the trust a collected, validated, RecallGuard-cleared signal
has earned. See app.db.models.investigation.Investigation.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Investigation
from app.domain.enums import InvestigationStatus
from app.investigations.schemas import InvestigationCreate

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def create_investigation(
    session: Session, *, payload: InvestigationCreate
) -> Investigation:
    """Record one user-supplied investigation. Start nothing.

    The new row is DRAFT: it has been written down and nothing has been
    investigated. Returning it as RUNNING, or kicking off work here,
    would make an ordinary POST spend provider budget and would report a
    state the backend cannot prove it is in.

    Commits, as the other write services here do, so a caller holding
    the row can rely on it existing rather than on a transaction it
    cannot see the end of.
    """
    investigation = Investigation(
        query=payload.query,
        industry=payload.industry,
        status=InvestigationStatus.DRAFT,
    )
    session.add(investigation)
    session.commit()
    session.refresh(investigation)
    logger.info(
        "investigation_created",
        extra={
            "investigation_id": str(investigation.id),
            # The query itself is deliberately NOT logged: it is
            # user-authored content, and logs are the wrong place for it.
            "query_length": len(investigation.query),
            "has_industry": investigation.industry is not None,
        },
    )
    return investigation


def get_investigation(
    session: Session, *, investigation_id: uuid.UUID
) -> Investigation | None:
    """One investigation, or None when there is no such row.

    None rather than an exception: "does this exist" is a question the
    caller may legitimately be asking, and only the API layer knows
    whether the answer is a 404.
    """
    return session.get(Investigation, investigation_id)


def list_investigations(
    session: Session, *, limit: int = DEFAULT_LIMIT
) -> list[Investigation]:
    """Recent investigations, newest first.

    Bounded on purpose, and bounded HERE as well as at the route: a
    service that will happily return every row is one background job away
    from loading the whole table into memory.

    Ordered by created_at, then by id. The id tie-break is not
    meaningful ordering -- it exists so two investigations written in the
    same clock tick have a stable relative order, without which a paged
    read could show one twice and the other never.
    """
    bounded = max(1, min(limit, MAX_LIMIT))
    return list(
        session.execute(
            select(Investigation)
            .order_by(Investigation.created_at.desc(), Investigation.id.desc())
            .limit(bounded)
        ).scalars()
    )
