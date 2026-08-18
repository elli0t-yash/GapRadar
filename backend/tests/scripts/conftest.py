"""Reuse RecallGuard's builders rather than inventing a second set.

The reopen script operates on a real ReliabilityIncident, so its tests
need the same source/collector/run scaffolding the RecallGuard tests
already define. Importing the fixtures here keeps one definition of what
a collector row looks like instead of a near-copy that can drift.
"""

from tests.recallguard.conftest import (  # noqa: F401
    collector,
    runs,
    source,
)
