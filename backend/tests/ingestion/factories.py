import uuid
from typing import Any

SOURCE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def make_raw_record(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "external_id": "post-1",
        "canonical_url": "https://reddit.com/r/startups/post-1",
        "title": "I wish there was a tool for X",
        "body": "Full complaint text goes here.",
        "signal_type": "complaint",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "metadata": {"upvotes": 12},
    }
    defaults.update(overrides)
    return defaults
