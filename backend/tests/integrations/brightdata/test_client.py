import json

import httpx
import pytest

from app.config import Settings
from app.integrations.brightdata.errors import (
    BrightDataAuthenticationError,
    BrightDataInvalidResponseError,
    BrightDataMalformedDatasetError,
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
        # A production trigger carries nothing else: version=dev would run
        # an unpublished draft collector, and a Bright Data `deadline`
        # once terminated a real production run mid-collection.
        assert "version" not in request.url.params
        assert "deadline" not in request.url.params
        assert set(request.url.params) == {"collector", "queue_next"}
        assert b"deadline" not in request.content
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


@pytest.mark.parametrize(
    "payload",
    [
        # The documented in-progress example...
        {"status": "building"},
        # ...and what a real production job actually returned.
        {"status": "collecting", "message": "Job is not finished"},
    ],
)
def test_get_collector_run_status_in_progress(
    brightdata_settings: Settings, payload: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dca/dataset"
        assert request.url.params["id"] == "j_abc"
        return httpx.Response(200, json=payload)

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.RUNNING
    assert execution.record_count is None
    # The provider's own account of the wait is kept for debugging.
    assert execution.provider_metadata == payload


def test_get_collector_run_status_succeeded(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"row": 1}, {"row": 2}])

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.SUCCEEDED
    assert execution.record_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "shape"},
        # An unrecognized status is NOT assumed to mean "still running":
        # polling forever on a state we do not understand would hide a
        # real provider change.
        {"status": "weird"},
        {"status": "failed"},
        {"status": None},
        {},
    ],
)
def test_get_collector_run_status_unexpected_shape_raises(
    brightdata_settings: Settings, payload: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_collector_run_status("j_abc")


JSONL_HEADERS = {"Content-Type": "application/jsonl; charset=utf-8"}


def jsonl_body(*values: object) -> str:
    return "\n".join(json.dumps(value) for value in values) + "\n"


def jsonl_response(*values: object) -> httpx.Response:
    """A completed dataset served the way a real production job served it."""
    return httpx.Response(200, content=jsonl_body(*values), headers=JSONL_HEADERS)


def test_get_collector_run_status_jsonl_dataset_is_succeeded(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return jsonl_response({"problem": "a"}, {"problem": "b"})

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.SUCCEEDED
    assert execution.record_count == 2


def test_single_row_jsonl_is_a_dataset_not_a_status_object(
    brightdata_settings: Settings,
) -> None:
    # One JSON object on one line is a one-record dataset. It must not be
    # mistaken for an in-progress status object merely because it is a
    # dict -- nor rejected for not being a recognized status.
    def handler(request: httpx.Request) -> httpx.Response:
        return jsonl_response({"problem": "only one"})

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")
        output = client.get_collector_output("j_abc")

    assert execution.status is CollectorRunStatus.SUCCEEDED
    assert execution.record_count == 1
    assert output.records == [{"problem": "only one"}]


def test_get_collector_output_reads_jsonl_rows_in_order(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return jsonl_response(
            {"problem": "first", "tam_score": 70},
            {"problem": "second", "tam_score": 50},
            {"problem": "third", "tam_score": 100},
        )

    with make_client(brightdata_settings, handler) as client:
        output = client.get_collector_output("j_abc")

    assert [record["problem"] for record in output.records] == [
        "first",
        "second",
        "third",
    ]
    # Values arrive exactly as the provider sent them. Deciding that a
    # tam_score of 70 is wrong belongs to the source validator; the
    # transport must not rescale, clamp, or drop it.
    assert [record["tam_score"] for record in output.records] == [70, 50, 100]


def test_blank_lines_in_jsonl_are_separators_not_rows(
    brightdata_settings: Settings,
) -> None:
    body = '{"problem": "a"}\n\n{"problem": "b"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers=JSONL_HEADERS)

    with make_client(brightdata_settings, handler) as client:
        output = client.get_collector_output("j_abc")

    assert output.records == [{"problem": "a"}, {"problem": "b"}]


def test_empty_jsonl_body_is_an_empty_dataset(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="", headers=JSONL_HEADERS)

    with make_client(brightdata_settings, handler) as client:
        execution = client.get_collector_run_status("j_abc")

    assert execution.status is CollectorRunStatus.SUCCEEDED
    assert execution.record_count == 0


@pytest.mark.parametrize("method", ["get_collector_run_status", "get_collector_output"])
def test_a_malformed_jsonl_line_rejects_the_whole_response(
    brightdata_settings: Settings, method: str
) -> None:
    # Never skip the bad line and return the survivors: that would turn a
    # corrupted dataset into a merely shorter one.
    body = '{"problem": "a"}\n{"problem": broken\n{"problem": "c"}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers=JSONL_HEADERS)

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError) as excinfo,
    ):
        getattr(client, method)("j_abc")

    assert "line 2" in str(excinfo.value)
    assert "j_abc" in str(excinfo.value)


@pytest.mark.parametrize(
    ("bad_row", "value_type"),
    [("unexpected string", "str"), (None, "NoneType"), (42, "int"), ([1], "list")],
)
def test_a_non_object_jsonl_row_is_a_malformed_dataset(
    brightdata_settings: Settings, bad_row: object, value_type: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return jsonl_response({"problem": "a"}, bad_row, {"problem": "c"})

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataMalformedDatasetError) as excinfo,
    ):
        client.get_collector_output("j_abc")

    assert excinfo.value.index == 1
    assert excinfo.value.value_type == value_type


def test_jsonl_parsing_is_chosen_by_content_type(
    brightdata_settings: Settings,
) -> None:
    # A JSON array is still read as a JSON array, whatever the row count.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps([{"problem": "a"}, {"problem": "b"}]),
            headers={"Content-Type": "application/json"},
        )

    with make_client(brightdata_settings, handler) as client:
        assert client.get_collector_output("j_abc").records == [
            {"problem": "a"},
            {"problem": "b"},
        ]


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


@pytest.mark.parametrize(
    ("bad_row", "value_type"),
    [
        ("unexpected string", "str"),
        (None, "NoneType"),
        (42, "int"),
        (3.5, "float"),
        (["nested", "list"], "list"),
        (True, "bool"),
    ],
)
def test_get_collector_output_rejects_a_non_object_row(
    brightdata_settings: Settings, bad_row: object, value_type: str
) -> None:
    # A malformed row must never be silently dropped: two good rows and
    # one garbage row is a broken dataset, not a two-record dataset.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"problem": "a"}, bad_row, {"problem": "b"}])

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataMalformedDatasetError) as excinfo,
    ):
        client.get_collector_output("j_abc")

    assert excinfo.value.index == 1
    assert excinfo.value.value_type == value_type
    assert "j_abc" in str(excinfo.value)


def test_malformed_dataset_error_is_an_invalid_response_error(
    brightdata_settings: Settings,
) -> None:
    # Callers already handling BrightDataInvalidResponseError keep working.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["nope"])

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataInvalidResponseError),
    ):
        client.get_collector_output("j_abc")


def test_get_collector_output_reports_the_first_malformed_row(
    brightdata_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"problem": "a"}, None, "also bad"])

    with (
        make_client(brightdata_settings, handler) as client,
        pytest.raises(BrightDataMalformedDatasetError) as excinfo,
    ):
        client.get_collector_output("j_abc")

    assert excinfo.value.index == 1


def test_empty_dataset_is_not_malformed(brightdata_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with make_client(brightdata_settings, handler) as client:
        output = client.get_collector_output("j_abc")

    assert output.records == []
