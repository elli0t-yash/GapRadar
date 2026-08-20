"""Where research found for ONE subject gets attributed and written.

The research engine is deliberately one engine, not two. Query
generation, acquisition, ingestion, ranking and judging are identical
whether the subject is a market Signal or a user-supplied Investigation
-- and any duplication of those would be a second place for the pipeline
to drift.

What genuinely differs is the last inch: which table a verdict lands in,
and which foreign key a search run is attributed to. That difference is
real and is not worth abstracting away, because both sides carry proper
foreign keys and a polymorphic subject id would carry none.

So this module is the seam. It maps a ResearchSubject onto its
persistence, and the orchestration asks it once instead of branching on
origin in four places. THE SUBJECT SELECTS ITS OWN STORE: a caller cannot
hand the engine a Signal subject and an investigation store, because it
never chooses one.
"""

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InvestigationResearchMatch, OpportunityResearchMatch
from app.domain.enums import ResearchSubjectOrigin
from app.research_intelligence.matching import ResearchMatchVerdict
from app.research_intelligence.schemas import ResearchSubject


class SubjectAttribution(Protocol):
    """The foreign keys a search run carries for one kind of subject."""

    signal_id: uuid.UUID | None
    investigation_id: uuid.UUID | None


class ResearchMatchStore(Protocol):
    """Writes and reads relevance verdicts for ONE subject.

    Small on purpose. It owns exactly the operations whose SQL differs by
    subject kind; everything upstream of a verdict is subject-agnostic
    and lives in the engine.
    """

    def upsert(
        self, session: Session, *, paper_id: uuid.UUID, verdict: ResearchMatchVerdict
    ) -> bool:
        """Write one verdict. Returns True if a row was created."""
        ...


def search_run_attribution(subject: ResearchSubject) -> dict[str, uuid.UUID | None]:
    """The ResearchSearchRun foreign keys for this subject.

    Returns both columns explicitly, including the None, so a caller
    cannot accidentally leave the other one carrying a stale value. At
    most one is ever set -- the table's CHECK constraint enforces the
    same rule independently.
    """
    if subject.origin is ResearchSubjectOrigin.SIGNAL:
        return {"signal_id": subject.subject_id, "investigation_id": None}
    return {"signal_id": None, "investigation_id": subject.subject_id}


class _MatchStore:
    """Upsert-by-(subject, paper) over one of the two match tables.

    One implementation, parameterised by model and column, rather than
    two near-identical classes: the SQL shape is genuinely the same and
    only the names differ. The tables stay separate for the reasons in
    app.db.models.investigation_research_match; that is a schema
    decision, and it does not require duplicated Python.
    """

    def __init__(
        self,
        *,
        model: type[OpportunityResearchMatch] | type[InvestigationResearchMatch],
        column: str,
        subject_id: uuid.UUID,
    ) -> None:
        self._model = model
        self._column = column
        self._subject_id = subject_id

    def upsert(
        self, session: Session, *, paper_id: uuid.UUID, verdict: ResearchMatchVerdict
    ) -> bool:
        """Write one verdict. Returns True if a row was created.

        One verdict per (subject, paper): re-running replaces the previous
        judgement rather than stacking a second, near-identical claim, so
        "how relevant is this paper to this subject" always has exactly
        one answer.

        Crucially this is scoped to ONE subject, so a verdict written for
        an Investigation can never touch the verdict the same paper
        earned against a Signal. They are different claims about
        different problem statements and both remain readable.
        """
        subject_column = getattr(self._model, self._column)
        existing = session.execute(
            select(self._model).where(
                subject_column == self._subject_id,
                self._model.research_paper_id == paper_id,
            )
        ).scalar_one_or_none()

        values = {
            "relevance_score": verdict.relevance_score,
            "matched_concepts": list(verdict.matched_concepts),
            "match_reason": verdict.match_reason,
            "technical_readiness_score": verdict.technical_readiness_score,
        }

        if existing is not None:
            for field, value in values.items():
                setattr(existing, field, value)
            session.flush()
            return False

        session.add(
            self._model(
                **{self._column: self._subject_id},
                research_paper_id=paper_id,
                **values,
            )
        )
        session.flush()
        return True


def match_store_for(subject: ResearchSubject) -> ResearchMatchStore:
    """The match table this subject's verdicts belong in.

    Derived from the subject rather than injected, so the engine cannot
    be handed a subject and a store that disagree about what is being
    researched -- a mismatch that would silently write a user hypothesis'
    verdicts into the opportunity table.
    """
    if subject.origin is ResearchSubjectOrigin.SIGNAL:
        return _MatchStore(
            model=OpportunityResearchMatch,
            column="signal_id",
            subject_id=subject.subject_id,
        )
    return _MatchStore(
        model=InvestigationResearchMatch,
        column="investigation_id",
        subject_id=subject.subject_id,
    )


__all__ = [
    "ResearchMatchStore",
    "match_store_for",
    "search_run_attribution",
]
