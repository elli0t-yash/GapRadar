"""Fixtures for the schema-level tests.

Re-exports the signal factory rather than building a second one: a
constraint test should be inserting the same shape of row production
inserts, not a hand-rolled approximation of it.
"""

from tests.research_intelligence.conftest import (  # noqa: F401 - re-exported
    arxiv_records,
    opportunity_signal,
)
