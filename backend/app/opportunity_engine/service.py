"""Reads trusted problem signals. Ranks them. Writes nothing.

The one rule that matters here: a signal is exposed only when the
collector that produced it is currently HEALTHY by RecallGuard's
reckoning. "Currently" is deliberate -- a collector with an open incident
of any status (degraded, healing, validating, verifying, manual review)
is excluded, even for signals it collected long before the incident
opened, because until the incident is proven closed nobody can say which
of its runs were affected.

The healthy/unhealthy split is asked of RecallGuard
(collectors_with_active_incidents) rather than re-derived from incident
rows, so there is exactly one definition of an active incident.
"""

import logging
import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import CollectorRun, Signal
from app.domain.enums import SignalType
from app.opportunity_engine.schemas import Opportunity
from app.recallguard.service import collectors_with_active_incidents

logger = logging.getLogger(__name__)

# The signal role Fix My Itch produces: a stated unsolved problem.
_OPPORTUNITY_SIGNAL_TYPE = SignalType.PROBLEM

DEFAULT_LIMIT = 20


def list_opportunities(
    session: Session, *, limit: int = DEFAULT_LIMIT
) -> list[Opportunity]:
    """The highest-scoring trusted problems, best first.

    Ranking happens in Python rather than in SQL because the score is
    computed on read from the untrusted metadata payload and is
    deliberately not a column -- adding one would mean a migration for a
    number that changes whenever the weights do. The candidate set is one
    source's problem signals, so the whole set is small enough to sort in
    memory.

    Signals whose components are incomplete score None and sort last;
    they are never given a substitute score to make them rankable.
    """
    ranked = sorted(
        (Opportunity.from_signal(signal) for signal in _trusted_signals(session)),
        key=_rank,
        reverse=True,
    )
    return ranked[:limit] if limit >= 0 else ranked


def get_opportunity(session: Session, *, signal_id: uuid.UUID) -> Opportunity | None:
    """One trusted problem, or None.

    None covers both "no such signal" and "that signal belongs to a
    collector that is not currently healthy" -- an untrusted signal is
    simply not available through this surface.
    """
    signal = session.execute(
        _trusted_query(session).where(Signal.id == signal_id)
    ).scalar_one_or_none()
    return None if signal is None else Opportunity.from_signal(signal)


def count_trusted_signals(session: Session) -> int:
    """How many signals currently come from a healthy collector."""
    unhealthy = collectors_with_active_incidents(session)
    query = (
        select(func.count())
        .select_from(Signal)
        .join(CollectorRun, Signal.collector_run_id == CollectorRun.id)
    )
    if unhealthy:
        query = query.where(CollectorRun.collector_id.notin_(unhealthy))
    return session.execute(query).scalar_one()


def count_signals(session: Session) -> int:
    """How many signals exist at all, trusted or not."""
    return session.execute(select(func.count()).select_from(Signal)).scalar_one()


def _trusted_signals(session: Session) -> list[Signal]:
    return list(session.execute(_trusted_query(session)).scalars())


def _trusted_query(session: Session) -> Select[tuple[Signal]]:
    unhealthy = collectors_with_active_incidents(session)
    if unhealthy:
        logger.info(
            "opportunities_excluding_untrusted_collectors",
            extra={"untrusted_collector_count": len(unhealthy)},
        )
    query = (
        select(Signal)
        .join(CollectorRun, Signal.collector_run_id == CollectorRun.id)
        .where(Signal.signal_type == _OPPORTUNITY_SIGNAL_TYPE)
    )
    if unhealthy:
        query = query.where(CollectorRun.collector_id.notin_(unhealthy))
    return query


def _rank(opportunity: Opportunity) -> tuple[int, float, str]:
    """Scored signals first, then by score, then by title for stability."""
    score = opportunity.opportunity_score
    return (0 if score is None else 1, score or 0.0, opportunity.title)
