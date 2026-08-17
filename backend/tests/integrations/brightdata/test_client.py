import json

import httpx
import pytest

from app.config import Settings
from app.integrations.brightdata.errors import (
    BrightDataAuthenticationError,
    BrightDataInvalidResponseError,
    BrightDataProviderUnavailableError,
    BrightDataTimeoutError,
)
from app.integrations.brightdata.schemas import CollectorRunStatus
from tests.integrations.brightdata.conftest import make_client


def test_trigger_collector_run_maps_valid_response(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dca/trigger"
        assert request.url.params["collector"] == "c_123"
        assert request.url.params["queue_next"] == "1"
        assert request.headers["Authorization"] == "Bearer test-token-do-not-log"
        assert json.loads(request.content) == [{"url": "https://example.com"}]
        return httpx.Response(200, json={"collection_id": "j_abc"})

    with make_client(brightdata_settings, handler) as client:
        execution = client.trigger_collector_run(
            "c_123", [{"url": "https://example.com"}]
        )

    assert execution.external_run_id == "j_abc"
    assert execution.status is CollectorRunStatus.RUNNING
    assert execution.provider_metadata == {"collection_id": "j_abc"}


def test_get_collector_run_status_building(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dca/dataset"
        assert request.url.params["id"] == "j_abc"
        return httpx.Response(200, json={"status": "building"})

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.RUNNING
    assert execution.record_count is None


def test_get_collector_run_status_succeeded(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"row": 1}, {"row": 2}])

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.SUCCEEDED
    assert execution.record_count == 2


def test_get_collector_run_status_unexpected_shape_raises(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_collector_run_status("j_abc")


def test_get_collector_output_maps_records(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"title": "post 1"}, {"title": "post 2"}])

    with make_client(brightdata_settings, handler) as client:
        output = client.get_collector_output("j_abc")

    assert output.external_run_id == "j_abc"
    assert output.records == [{"title": "post 1"}, {"title": "post 2"}]


def test_get_collector_output_raises_when_not_ready(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "building"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_collector_output("j_abc")


def test_authentication_failure_raises(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid token"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataAuthenticationError),
    ):
        client.get_collector_run_status("j_abc")


def test_provider_5xx_raises_unavailable(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream error")

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataProviderUnavailableError),
    ):
        client.get_collector_run_status("j_abc")


def test_timeout_raises(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataTimeoutError),
    ):
        client.get_collector_run_status("j_abc")


def test_connection_failure_raises_unavailable(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataProviderUnavailableError),
    ):
        client.get_collector_run_status("j_abc")


def test_malformed_json_raises_invalid_response(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json{{{")

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_collector_run_status("j_abc")


def test_missing_required_field_raises_invalid_response(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "field"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.trigger_collector_run("c_123", [{"url": "https://example.com"}])


def test_token_does_not_leak_through_exception_messages(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid token"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataAuthenticationError) as exc_info,
    ):
        client.get_collector_run_status("j_abc")

    assert "test-token-do-not-log" not in str(exc_info.value)
    assert "test-token-do-not-log" not in repr(exc_info.value)


def test_provider_metadata_is_plain_data_not_used_for_control_flow(
    brightdata_settings: Settings,
) -> None:
    # provider_metadata carries whatever Bright Data returned, but nothing
    # in the client branches on its contents beyond the documented
    # "status": "building" marker checked explicitly in the client method
    # itself -- the returned model must not expose behavior, only data.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "building", "malicious": "__class__"}
        )

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.provider_metadata == {
        "status": "building",
        "malicious": "__class__",
    }
    assert isinstance(execution.provider_metadata, dict)
