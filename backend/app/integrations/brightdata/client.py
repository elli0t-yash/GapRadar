import json
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import Settings, get_settings
from app.integrations.brightdata.errors import (
    BrightDataAuthenticationError,
    BrightDataError,
    BrightDataInvalidResponseError,
    BrightDataMalformedDatasetError,
    BrightDataProviderUnavailableError,
    BrightDataTimeoutError,
)
from app.integrations.brightdata.schemas import (
    CollectorExecution,
    CollectorOutput,
    CollectorRunStatus,
    HealingCandidate,
    HealingRequest,
    HealingStatus,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0

# Path fragments confirmed via the official brightdata/cli source
# (src/commands/scraper.ts). Collector-scoped; no separate healing/job ID
# exists in the verified contract.
_REFACTOR_TRIGGER_PATH = "refactor_template"
_REFACTOR_PROGRESS_PATH = "refactor_template/progress"
_RESUME_JOB_PATH = "resume_automation_job"

# Raw wire status values for the refactor_template/progress response,
# verified via the same source (DONE_STATUS, TERMINAL_FAIL_STATUSES,
# AWAITING_STATUS constants).
_RAW_STATUS_DONE = "done"
_RAW_STATUS_TERMINAL_FAILURES = frozenset({"failed", "error", "cancelled"})
_RAW_STATUS_AWAITING_APPROVAL = "pending_answer"


class BrightDataClient:
    """External adapter over Bright Data's Scraper Studio collector API.

    This client is a pure transport boundary. It has no knowledge of
    RecallGuard, ingestion, or the database, and makes no writes of its
    own. Critically: it never decides whether a repair is trustworthy.
    `approve_healing`/`reject_healing` only ever tell Bright Data to
    commit or discard a proposed diff on Bright Data's side -- they do
    not and must not mark anything RECOVERED or HEALTHY in GapRadar. That
    decision belongs entirely to RecallGuard (a future phase), which
    requires a fresh, independently validated collector run before
    RECOVERED can ever be set. APPROVED != RECOVERED.

    A future orchestration layer decides *when* these methods are called;
    this client never calls itself.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        settings = settings or get_settings()
        self._api_token = settings.BRIGHTDATA_API_KEY
        self._client = httpx.Client(
            base_url=settings.BRIGHTDATA_BASE_URL,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- verified capabilities ------------------------------------------------
    # Endpoints below are confirmed against Bright Data's official
    # documentation (docs.brightdata.com/api-reference/scraper-studio-api,
    # docs.brightdata.com/datasets/scraper-studio/quickstart) as of this
    # writing.

    def trigger_collector_run(
        self, collector_id: str, inputs: list[dict[str, Any]]
    ) -> CollectorExecution:
        """Trigger an existing, published Scraper Studio collector.

        Verified: POST /dca/trigger?collector={collector_id}&queue_next=1
        Body: JSON array of input objects matching the collector's schema.
        Response: {"collection_id": "..."}
        """
        response = self._request(
            "POST",
            "/dca/trigger",
            params={"collector": collector_id, "queue_next": "1"},
            json=inputs,
        )
        data = self._parse_json(response)
        external_run_id = self._require_field(data, "collection_id", str)
        return CollectorExecution(
            external_run_id=external_run_id,
            status=CollectorRunStatus.RUNNING,
            record_count=None,
            provider_metadata=data,
        )

    def get_collector_run_status(self, external_run_id: str) -> CollectorExecution:
        """Poll a triggered run for status.

        Verified: GET /dca/dataset?id={external_run_id}
        In-progress response: {"status": "building"}
        Completed response: a JSON array of result rows -- there is no
        documented explicit "done"/"succeeded" status string; completion
        is inferred from the response being an array rather than a status
        object. Any other shape raises BrightDataInvalidResponseError
        rather than being guessed into a status.
        """
        response = self._request("GET", "/dca/dataset", params={"id": external_run_id})
        data = self._parse_json(response)

        if isinstance(data, list):
            return CollectorExecution(
                external_run_id=external_run_id,
                status=CollectorRunStatus.SUCCEEDED,
                record_count=len(data),
                provider_metadata={},
            )
        if isinstance(data, dict) and data.get("status") == "building":
            return CollectorExecution(
                external_run_id=external_run_id,
                status=CollectorRunStatus.RUNNING,
                record_count=None,
                provider_metadata=data,
            )
        raise BrightDataInvalidResponseError(
            "Unexpected /dca/dataset response shape while polling run "
            f"status for {external_run_id!r}; expected a JSON array or "
            '{"status": "building"}.'
        )

    def get_collector_output(self, external_run_id: str) -> CollectorOutput:
        """Retrieve structured output for a completed run.

        Verified: GET /dca/dataset?id={external_run_id}
        Completed response: JSON array of result rows.

        Raises BrightDataInvalidResponseError if the response is not a
        JSON array -- callers should poll get_collector_run_status until
        SUCCEEDED before calling this.

        Every row must be a JSON object. A row of any other shape raises
        BrightDataMalformedDatasetError naming the offending index: a
        malformed row is never dropped, because silently returning the
        surviving rows would present a corrupted dataset as a merely
        smaller one.
        """
        response = self._request("GET", "/dca/dataset", params={"id": external_run_id})
        data = self._parse_json(response)

        if not isinstance(data, list):
            raise BrightDataInvalidResponseError(
                f"Collector output for {external_run_id!r} is not ready, "
                "or the response was not a JSON array. Poll "
                "get_collector_run_status until status is SUCCEEDED before "
                "calling get_collector_output."
            )
        for index, record in enumerate(data):
            if not isinstance(record, dict):
                raise BrightDataMalformedDatasetError(
                    f"Dataset row {index} of collection {external_run_id!r} "
                    f"is a {type(record).__name__}, expected a JSON object",
                    index=index,
                    value_type=type(record).__name__,
                )
        return CollectorOutput(
            external_run_id=external_run_id,
            records=data,
            provider_metadata={},
        )

    # -- self-healing capabilities ----------------------------------------------
    # Verified via the official brightdata/cli source (src/commands/
    # scraper.ts, src/types/scraper.ts), a first-party Bright Data
    # repository -- not a third-party/unofficial source. Bright Data's
    # docs.brightdata.com site itself only shows the self-healing feature
    # as a UI/IDE walkthrough with no HTTP-level reference, so the CLI
    # source is the authoritative confirmation for this transport.

    def request_healing(self, healing_request: HealingRequest) -> HealingCandidate:
        """Trigger Bright Data's self-healing for a collector.

        Verified: POST /dca/collectors/{collector_id}/refactor_template
        Body: {"prompt": str, "custom_input": list}

        The reference CLI implementation posts this exact body and then
        immediately begins polling get_healing_status instead of reading
        the trigger response for control flow -- no field in that
        response is confirmed to be load-bearing beyond a 2xx meaning
        "accepted". We therefore do not require any particular field
        here: on success this returns status UNKNOWN (not yet polled) and
        stores whatever JSON body Bright Data returned, if any, in
        provider_metadata. Call get_healing_status next to learn the real
        status.
        """
        response = self._request(
            "POST",
            f"/dca/collectors/{healing_request.collector_id}/{_REFACTOR_TRIGGER_PATH}",
            json={
                "prompt": healing_request.prompt,
                "custom_input": healing_request.custom_input,
            },
        )
        provider_metadata: dict[str, Any] = {}
        if response.content:
            try:
                parsed = self._parse_json(response)
            except BrightDataInvalidResponseError:
                parsed = None
            if isinstance(parsed, dict):
                provider_metadata = parsed
        return HealingCandidate(
            collector_id=healing_request.collector_id,
            status=HealingStatus.UNKNOWN,
            candidate_preview=None,
            provider_metadata=provider_metadata,
        )

    def get_healing_status(self, collector_id: str) -> HealingCandidate:
        """Poll self-healing progress for a collector.

        Verified: GET /dca/collectors/{collector_id}/refactor_template/progress
        Response (fields genuinely read by the reference implementation,
        not merely declared): {"status": str (required), "step": str?,
        "completed_steps": [str]?, "diff": <untyped>?, "preview_result":
        <untyped>?}
        """
        response = self._request(
            "GET",
            f"/dca/collectors/{collector_id}/{_REFACTOR_PROGRESS_PATH}",
        )
        data = self._parse_json(response)
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise BrightDataInvalidResponseError(
                "Unexpected /refactor_template/progress response shape "
                f"for collector {collector_id!r}; expected an object with "
                "a string 'status' field."
            )
        return self._build_healing_candidate(collector_id, data)

    def approve_healing(
        self, collector_id: str, auto_save: bool = False
    ) -> HealingCandidate:
        """Approve a self-healing fix that is awaiting approval.

        Verified: POST /dca/collectors/{collector_id}/resume_automation_job
        Body: {"message": true}, or {"message": true, "auto_save": true}
        when auto_save=True.

        This only tells Bright Data to commit the proposed diff on its
        own side. It must be called only in direct response to an
        explicit RecallGuard/human approval decision -- never
        automatically, and never as a side effect of request_healing.
        Approving here is not equivalent to GapRadar's RECOVERED state:
        RECOVERED requires a fresh, independently validated collector run
        after approval. APPROVED != RECOVERED.

        Per the reference implementation, a resumed job can land on
        another approval gate rather than finishing immediately
        (multi-step approval), so this returns whatever
        get_healing_status reports next -- it does not assume the job is
        necessarily done.
        """
        self._resume_automation_job(collector_id, approve=True, auto_save=auto_save)
        return self.get_healing_status(collector_id)

    def reject_healing(self, collector_id: str) -> HealingCandidate:
        """Reject a self-healing fix that is awaiting approval.

        Verified: POST /dca/collectors/{collector_id}/resume_automation_job
        Body: {"message": false}. auto_save is never sent on reject (the
        reference implementation omits it there; Bright Data's API
        ignores it on reject).

        Note on interpreting the result: Bright Data's progress endpoint
        has no dedicated "rejected" wire status. After a successful
        reject, the reference implementation observes raw status "done"
        and treats that as "the reject was processed" -- purely because
        it knows it just requested a reject, not because the wire value
        says so. This client mirrors that: a DONE status returned here
        means the reject was processed, not that a fix was applied. A
        HealingCandidate from an unrelated get_healing_status() call
        cannot make that distinction on its own.
        """
        self._resume_automation_job(collector_id, approve=False, auto_save=False)
        return self.get_healing_status(collector_id)

    def _resume_automation_job(
        self, collector_id: str, *, approve: bool, auto_save: bool
    ) -> None:
        body: dict[str, Any] = {"message": approve}
        if approve and auto_save:
            body["auto_save"] = True
        self._request(
            "POST",
            f"/dca/collectors/{collector_id}/{_RESUME_JOB_PATH}",
            json=body,
        )

    def _build_healing_candidate(
        self, collector_id: str, data: dict[str, Any]
    ) -> HealingCandidate:
        raw_status = data["status"]
        if raw_status == _RAW_STATUS_AWAITING_APPROVAL:
            status = HealingStatus.AWAITING_APPROVAL
        elif raw_status == _RAW_STATUS_DONE:
            status = HealingStatus.DONE
        elif raw_status in _RAW_STATUS_TERMINAL_FAILURES:
            status = HealingStatus.FAILED
        else:
            status = HealingStatus.UNKNOWN

        candidate_preview = data.get("preview_result", data.get("diff"))
        return HealingCandidate(
            collector_id=collector_id,
            status=status,
            candidate_preview=candidate_preview,
            provider_metadata=data,
        )

    # -- transport plumbing -----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_token}"}
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BrightDataTimeoutError(
                f"Bright Data request timed out: {method} {path}"
            ) from exc
        except httpx.ConnectError as exc:
            raise BrightDataProviderUnavailableError(
                f"Could not connect to Bright Data: {method} {path}"
            ) from exc
        except httpx.TransportError as exc:
            raise BrightDataProviderUnavailableError(
                f"Bright Data transport error: {method} {path}"
            ) from exc

        if response.status_code in (401, 403):
            raise BrightDataAuthenticationError(
                f"Bright Data authentication failed ({response.status_code}) "
                f"for {method} {path}"
            )
        if response.status_code >= 500:
            raise BrightDataProviderUnavailableError(
                f"Bright Data returned {response.status_code} for {method} {path}"
            )
        if response.status_code >= 400:
            raise BrightDataError(
                f"Bright Data returned {response.status_code} for {method} {path}"
            )
        return response

    def _parse_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise BrightDataInvalidResponseError(
                "Bright Data returned a response that was not valid JSON"
            ) from exc

    def _require_field(self, data: Any, field: str, expected_type: type) -> Any:
        if not isinstance(data, dict) or field not in data:
            raise BrightDataInvalidResponseError(
                f"Bright Data response missing required field {field!r}"
            )
        value = data[field]
        if not isinstance(value, expected_type):
            raise BrightDataInvalidResponseError(
                f"Bright Data response field {field!r} had unexpected type "
                f"{type(value).__name__}"
            )
        return value
