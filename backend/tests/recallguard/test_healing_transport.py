"""Transport-level checks of the Bright Data self-healing endpoints."""

import json

import httpx
import pytest

from app.config import Settings
from app.integrations.brightdata.errors import BrightDataInvalidResponseError
from app.integrations.brightdata.schemas import HealingRequest, HealingStatus
from tests.integrations.brightdata.conftest import make_client


def test_self_heal_trigger_uses_the_documented_url_and_body(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/dca/collectors/c_123/refactor_template"
        assert json.loads(request.content) == {
            "prompt": "fix the tam score extraction",
            "custom_input": [],
        }
        return httpx.Response(200, json={"accepted": True})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.request_healing(
            HealingRequest(collector_id="c_123", prompt="fix the tam score extraction")
        )

    assert candidate.collector_id == "c_123"


def test_self_heal_requests_authenticate_without_leaking_the_token(
    brightdata_settings: Settings,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    # The token travels in the header, and nowhere else.
    assert seen["authorization"] == "Bearer test-token-do-not-log"
    assert "test-token-do-not-log" not in repr(candidate)
    assert "test-token-do-not-log" not in str(candidate.provider_metadata)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "in_progress"}, HealingStatus.UNKNOWN),
        ({"status": "pending_answer"}, HealingStatus.AWAITING_APPROVAL),
        ({"status": "done"}, HealingStatus.DONE),
        ({"status": "failed"}, HealingStatus.FAILED),
        ({"status": "error"}, HealingStatus.FAILED),
        ({"status": "cancelled"}, HealingStatus.FAILED),
    ],
)
def test_progress_statuses_map_to_provider_neutral_values(
    brightdata_settings: Settings, payload: dict, expected: HealingStatus
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dca/collectors/c_123/refactor_template/progress"
        return httpx.Response(200, json=payload)

    with make_client(brightdata_settings, handler) as client:
        assert client.get_healing_status("c_123").status is expected


def test_progress_keeps_preview_and_diff_distinguishable(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "pending_answer",
                "preview_result": [{"problem": "a"}],
                "diff": "- old\n+ new",
            },
        )

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    assert candidate.preview_result == [{"problem": "a"}]
    assert candidate.diff == "- old\n+ new"


def test_malformed_progress_payload_is_rejected(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_healing_status("c_123")


def test_approve_sends_message_true_with_auto_save(
    brightdata_settings: Settings,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("resume_automation_job"):
            assert request.method == "POST"
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        client.approve_healing("c_123", auto_save=True)

    assert bodies == [{"message": True, "auto_save": True}]


def test_reject_sends_message_false_with_auto_save_false(
    brightdata_settings: Settings,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("resume_automation_job"):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        client.reject_healing("c_123")

    assert bodies == [{"message": False, "auto_save": False}]
