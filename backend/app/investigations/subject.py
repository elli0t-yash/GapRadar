"""Investigation -> ResearchSubject. Provider-free, database-free, pure.

The whole point of the ResearchSubject abstraction: a user-supplied
hypothesis becomes something the research engine can work on WITHOUT
being written into `signals` first. No fake Signal is created here or
anywhere else -- see app.db.models.investigation.Investigation for why
that would be unrecoverable.
"""

from app.db.models import Investigation
from app.domain.enums import ResearchSubjectOrigin
from app.research_intelligence.schemas import ResearchSubject


def research_subject_from_investigation(
    investigation: Investigation,
) -> ResearchSubject:
    """One user-supplied hypothesis, as the research engine's subject.

    The mapping, and why each half is what it is:

    - `problem` is `investigation.query`, verbatim. There is no derived
      canonical problem statement at this phase, and inventing one would
      mean the engine researches a sentence the user never wrote.
    - `description` falls back to the query when none was derived. It is
      NOT persisted onto the row and it is NOT presented as a
      description the user gave: it exists because the research contract
      wants elaboration text and the honest answer to "what elaboration"
      is "the same words, we have nothing more". Repeating the problem
      costs nothing downstream -- candidate ranking deliberately ignores
      the description, and the matcher sees the same sentence twice
      rather than a fabricated one.
    - `origin` is INVESTIGATION, permanently. This is what stops a user
      hypothesis' verdicts landing in the opportunity tables, and what
      lets any consumer say which kind of thing it is looking at.
    - `industry` passes through, including its absence. An investigation
      whose author named no industry is not given an invented one.

    THE SUBJECT CARRIES NO TRUST. Nothing has corroborated that the
    problem is real, widespread or unsolved; the only validation this
    text has ever had is shape validation at the API boundary.
    """
    return ResearchSubject(
        subject_id=investigation.id,
        origin=ResearchSubjectOrigin.INVESTIGATION,
        problem=investigation.query,
        description=investigation.description or investigation.query,
        industry=investigation.industry,
    )
