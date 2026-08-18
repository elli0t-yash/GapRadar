import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.integrations.brightdata.errors import (
    BrightDataAuthenticationError,
    BrightDataInvalidResponseError,
    BrightDataProviderUnavailableError,
    BrightDataTimeoutError,
)
from app.integrations.brightdata.schemas import HealingRequest, HealingStatus
from tests.integrations.brightdata.conftest import make_client


def test_healing_request_rejects_prompt_over_1000_chars() -> None:
    with pytest.raises(ValidationError):
        HealingRequest(collector_id="c_123", prompt="x" * 1001)


def test_healing_request_rejects_blank_prompt() -> None:
    with pytest.raises(ValidationError):
        HealingRequest(collector_id="c_123", prompt="   ")


def test_request_healing_success(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/dca/collectors/c_123/refactor_template"
        assert request.headers["Authorization"] == "Bearer test-token-do-not-log"
        import json as _json

        body = _json.loads(request.content)
        assert body == {"prompt": "fix the price selector", "custom_input": []}
        return httpx.Response(200, json={"id": "abc", "queued": True})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.request_healing(
            HealingRequest(collector_id="c_123", prompt="fix the price selector")
        )

    # The trigger response has no confirmed load-bearing fields, so status
    # is UNKNOWN until get_healing_status is polled explicitly.
    assert candidate.status is HealingStatus.UNKNOWN
    assert candidate.collector_id == "c_123"
    assert candidate.provider_metadata == {"id": "abc", "queued": True}


def test_request_healing_tolerates_empty_response_body(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with make_client(brightdata_settings, handler) as client:
        candidate = client.request_healing(
            HealingRequest(collector_id="c_123", prompt="fix it")
        )

    assert candidate.status is HealingStatus.UNKNOWN
    assert candidate.provider_metadata == {}


def test_get_healing_status_awaiting_approval(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/dca/collectors/c_123/refactor_template/progress"
        return httpx.Response(
            200,
            json={
                "status": "pending_answer",
                "step": "review",
                "preview_result": {"template_b": {"steps": 3}},
            },
        )

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    assert candidate.status is HealingStatus.AWAITING_APPROVAL
    assert candidate.candidate_preview == {"template_b": {"steps": 3}}


def test_get_healing_status_done(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    assert candidate.status is HealingStatus.DONE


@pytest.mark.parametrize("raw_status", ["failed", "error", "cancelled"])
def test_get_healing_status_terminal_failures(
    brightdata_settings: Settings, raw_status: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": raw_status})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    assert candidate.status is HealingStatus.FAILED


def test_get_healing_status_unknown_future_status_is_safe_fallback(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "some_new_status_bright_data_adds"})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.get_healing_status("c_123")

    assert candidate.status is HealingStatus.UNKNOWN
    assert candidate.provider_metadata == {"status": "some_new_status_bright_data_adds"}


def test_get_healing_status_missing_status_field_raises(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"step": "review"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_healing_status("c_123")


def test_get_healing_status_malformed_json_raises(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json{{{")

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_healing_status("c_123")


def test_get_healing_status_authentication_failure(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid token"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataAuthenticationError),
    ):
        client.get_healing_status("c_123")


def test_get_healing_status_provider_5xx(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream error")

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataProviderUnavailableError),
    ):
        client.get_healing_status("c_123")


def test_get_healing_status_timeout(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataTimeoutError),
    ):
        client.get_healing_status("c_123")


def test_approve_healing_sends_message_true_and_polls_status(
    brightdata_settings: Settings,
) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("resume_automation_job"):
            import json as _json

            assert _json.loads(request.content) == {"message": True}
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.approve_healing("c_123")

    assert calls == [
        ("POST", "/dca/collectors/c_123/resume_automation_job"),
        ("GET", "/dca/collectors/c_123/refactor_template/progress"),
    ]
    assert candidate.status is HealingStatus.DONE


def test_approve_healing_with_auto_save_sends_auto_save_flag(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("resume_automation_job"):
            import json as _json

            assert _json.loads(request.content) == {
                "message": True,
                "auto_save": True,
            }
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        client.approve_healing("c_123", auto_save=True)


def test_reject_healing_sends_message_false_and_auto_save_false(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("resume_automation_job"):
            import json as _json

            assert _json.loads(request.content) == {
                "message": False,
                "auto_save": False,
            }
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        candidate = client.reject_healing("c_123")

    # DONE after a reject call means "the reject was processed", not "the
    # fix was applied" -- see the reject_healing docstring. The client
    # returns the same DONE status either way; interpreting it correctly
    # is the caller's responsibility based on which method it called.
    assert candidate.status is HealingStatus.DONE


def test_approve_and_reject_are_separate_explicit_methods(
    brightdata_settings: Settings,
) -> None:
    sent_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("resume_automation_job"):
            import json as _json

            sent_bodies.append(_json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": "done"})

    with make_client(brightdata_settings, handler) as client:
        client.approve_healing("c_123")
        client.reject_healing("c_123")

    assert sent_bodies == [
        {"message": True},
        {"message": False, "auto_save": False},
    ]


def test_request_healing_never_invokes_resume_automation_job(
    brightdata_settings: Settings,
) -> None:
    """Explicit trust-boundary test: request_healing must never call the
    approve/reject endpoint as a side effect. Approval must always be a
    separate, explicit call driven by a RecallGuard/human decision.
    """
    resume_was_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal resume_was_called
        if request.url.path.endswith("resume_automation_job"):
            resume_was_called = True
        return httpx.Response(200, json={"id": "abc", "queued": True})

    with make_client(brightdata_settings, handler) as client:
        client.request_healing(
            HealingRequest(collector_id="c_123", prompt="fix the price selector")
        )

    assert resume_was_called is False
