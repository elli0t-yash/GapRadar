"""Fixtures for the pipeline orchestration tests.

The collector/source/run builders are the ones the RecallGuard tests
already use, so these tests exercise the same evidence contract rather
than a second invented one. No test here makes a real Bright Data call:
the provider is always an httpx.MockTransport handler.
"""

import json
from typing import Any

import httpx
import pytest

from tests.integrations.brightdata.conftest import HEALTHY_FIX_MY_ITCH_FIXTURE
from tests.recallguard.conftest import (  # noqa: F401 - re-exported fixtures
    brightdata_settings,
    collector,
    runs,
    source,
)
from tests.recallguard.healing_fakes import ScriptedProvider


class RepairableProvider(ScriptedProvider):
    """A collector that returns broken data until its repair is approved.

    Two behaviours the base fake does not have, both needed to drive a
    whole pipeline cycle:

    - every trigger gets its own collection id, because a real
      verification run is a genuinely new run and the schema enforces
      that (collector_id, external_run_id) is unique;
    - the dataset changes only after the provider is told to resume the
      approved repair, so recovery can only come from a collection that
      happened after the fix -- never from a replay of the broken one.
    """

    def __init__(
        self,
        *,
        broken: list[dict[str, Any]],
        healed: list[dict[str, Any]],
        progress: list[dict[str, Any]],
    ) -> None:
        super().__init__(progress=progress, dataset=broken)
        self.healed = healed
        self.trigger_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dca/trigger":
            self.requests.append(request)
            self.trigger_count += 1
            return httpx.Response(
                200, json={"collection_id": f"j_pipeline_{self.trigger_count}"}
            )
        response = super().__call__(request)
        if request.url.path.endswith("resume_automation_job"):
            self.dataset = self.healed
        return response


@pytest.fixture(scope="session")
def production_records() -> list[dict[str, Any]]:
    """The verified healthy production payload, committed as a fixture."""
    return json.loads(HEALTHY_FIX_MY_ITCH_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def good_records(production_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in production_records[:5]]


@pytest.fixture
def drifted_records(good_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The deliberate scraper fault: a TAM score on the wrong scale.

    Preserved verbatim, never rescaled -- 60 stays 60, which is exactly
    why the source contract rejects it.
    """
    records = [dict(record) for record in good_records]
    records[0] = {**records[0], "tam_score": 60}
    return records
