"""Fixtures for the investigation tests.

Re-exports the research-side fixtures rather than building parallel ones,
which is the point of the whole phase: an investigation is researched by
the SAME engine over the SAME records, so it should be exercised against
the same committed collector output a signal is.

NOTHING HERE MAKES A NETWORK CALL. Acquisition is replayed from the
committed fixture through SequenceResearchCollector, and every provider
client a test could reach is bound to a transport that raises.
"""

from tests.research_intelligence.conftest import (  # noqa: F401 - re-exported
    arxiv_records,
    investigation,
    opportunity_signal,
    records,
    second_opportunity_signal,
)

