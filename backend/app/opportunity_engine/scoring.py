"""One deterministic opportunity score, computed on read.

Nothing here is persisted and no column exists for it: the score is a
weighted view of numbers the source already published and GapRadar
already stores verbatim in Signal.metadata.

The two scales the source uses are kept straight:

- itch_score is published on 0..100 and is used as-is.
- severity/tam/whitespace/frequency are published on 1..10 and are
  multiplied by ten to put them on the same 0..100 display scale.

That multiplication is presentation only, and it runs in the safe
direction. The backend still never rescales a value *downwards* to make a
broken payload look plausible: a tam_score of 70 is a contract violation
the Fix My Itch adapter rejects outright, so it never reaches a persisted
signal, and if one ever did it would be reported here as unscorable
rather than quietly divided by ten.
"""

import math
from typing import Any

# Sum to 1.0. Weights are a product judgement, not a fact about the
# source, so they live in one named place rather than inline in a query.
ITCH_WEIGHT = 0.30
SEVERITY_WEIGHT = 0.20
TAM_WEIGHT = 0.20
WHITESPACE_WEIGHT = 0.20
FREQUENCY_WEIGHT = 0.10

# The source's own published ranges, mirroring the Fix My Itch source
# contract. A value outside them means the record never passed source
# validation, so it is not scored at all.
_ITCH_RANGE = (0.0, 100.0)
_COMPONENT_RANGE = (1.0, 10.0)
_COMPONENT_TO_PERCENT = 10.0

_COMPONENT_WEIGHTS = (
    ("severity_score", SEVERITY_WEIGHT),
    ("tam_score", TAM_WEIGHT),
    ("whitespace_score", WHITESPACE_WEIGHT),
    ("frequency_score", FREQUENCY_WEIGHT),
)


def _number(value: Any, *, low: float, high: float) -> float | None:
    """A finite number inside the source's published range, or None.

    bool is excluded explicitly: it is a Python int, and treating True as
    1 would invent a score out of a field the source never sent.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if not low <= number <= high:
        return None
    return number


def opportunity_score(metadata: dict[str, Any] | None) -> float | None:
    """Score one signal from its stored source metadata, or None.

    None means "this signal cannot be scored from what the source
    published" -- a missing, non-numeric, or out-of-range component. It
    is never substituted with a zero or a default, because an invented
    number would rank a signal it knows nothing about.

    Rounded to two decimals so the same signal always renders the same
    value.
    """
    if not metadata:
        return None

    itch = _number(metadata.get("itch_score"), low=_ITCH_RANGE[0], high=_ITCH_RANGE[1])
    if itch is None:
        return None

    total = ITCH_WEIGHT * itch
    for field, weight in _COMPONENT_WEIGHTS:
        component = _number(
            metadata.get(field), low=_COMPONENT_RANGE[0], high=_COMPONENT_RANGE[1]
        )
        if component is None:
            return None
        total += weight * component * _COMPONENT_TO_PERCENT

    return round(total, 2)
