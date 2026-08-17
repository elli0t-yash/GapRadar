import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CollectorRunStatus(str, enum.Enum):
    """Provider-neutral run status, mapped from Bright Data's raw response.

    Bright Data's documented /dca/dataset response only distinguishes two
    shapes: {"status": "building"} while in progress, and a JSON array of
    result rows once complete. There is no documented explicit "failed"
    status string, so that outcome is intentionally not represented here
    -- an unrecognized response raises BrightDataInvalidResponseError
    instead of being guessed into a status value.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"


class CollectorExecution(BaseModel):
    """Result of triggering or polling a Scraper Studio collector run."""

    model_config = ConfigDict(frozen=True)

    external_run_id: str
    status: CollectorRunStatus
    record_count: int | None = None
    # Raw provider payload, preserved for audit/debugging only. Untrusted:
    # must never be interpreted or used to drive application control flow.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class CollectorOutput(BaseModel):
    """Structured output of a completed Scraper Studio collector run."""

    model_config = ConfigDict(frozen=True)

    external_run_id: str
    records: list[dict[str, Any]]
    # Untrusted, as above.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class HealingStatus(str, enum.Enum):
    """Provider-neutral healing lifecycle status, mapped from Bright
    Data's GET /dca/collectors/{id}/refactor_template/progress response.

    Verified via the official brightdata/cli source
    (src/commands/scraper.ts): the raw `status` field returned by that
    endpoint is compared directly against the literal strings "done",
    "failed", "error", "cancelled", and "pending_answer" -- these are not
    CLI-invented labels, they are the values the CLI's own polling logic
    branches on against the real API response.

    - AWAITING_APPROVAL maps from raw "pending_answer" (the CLI's
      "awaiting_approval" is its own display label for this wire value,
      not the wire value itself).
    - DONE maps from raw "done". Note this is contextually ambiguous: it
      means "fix committed" after a heal trigger or an approve call, but
      "reject was processed" after a reject call -- the wire value alone
      does not distinguish these, only the caller's own knowledge of
      which request it made does (see BrightDataClient.reject_healing).
    - FAILED maps from raw "failed", "error", or "cancelled" (grouped
      together, matching the CLI's own TERMINAL_FAIL_STATUSES grouping).
    - UNKNOWN covers any other status string. There is no confirmed
      "still running" wire value in the documentation or CLI source --
      the CLI infers "keep polling" for anything unrecognized, but that
      inference is specific to its own polling loop, so this client does
      not repeat it as fact. UNKNOWN is a safe, forward-compatible
      fallback rather than a rejection of an unrecognized value.
    """

    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HealingRequest(BaseModel):
    """Request to trigger Bright Data's self-healing on a collector.

    Verified: POST /dca/collectors/{collector_id}/refactor_template
    Body (confirmed via official brightdata/cli source,
    src/types/scraper.ts `Refactor_request`): {"prompt": str, "custom_input":
    list}. `custom_input` is always sent, defaulting to an empty list --
    the reference implementation never populates it with anything else,
    but the wire type allows arbitrary array contents.

    prompt max length 1000, matching the CLI's own validate_heal_prompt.
    """

    model_config = ConfigDict(frozen=True)

    collector_id: str
    prompt: str = Field(min_length=1, max_length=1000)
    custom_input: list[Any] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be blank")
        return stripped


class HealingCandidate(BaseModel):
    """A proposed (or in-progress) scraper repair.

    Fields mirror what GapRadar's RecallGuard will conceptually need to
    inspect before ever deciding APPROVED vs. RECOVERED -- this client
    never makes that decision itself.

    Verified via the official brightdata/cli source
    (src/types/scraper.ts `Ai_progress_response`): the progress endpoint
    response has a required `status` field, and optional `step`,
    `completed_steps`, `diff`, and `preview_result` fields -- all of
    which are genuinely read by the reference implementation, not merely
    declared in a type with no runtime use.

    `healing_operation_id` is always None: Bright Data's self-healing
    endpoints (trigger, progress, resume) are scoped entirely by
    `collector_id` -- there is no separate healing/job identifier in the
    verified contract. The field is kept for forward compatibility only.
    """

    model_config = ConfigDict(frozen=True)

    collector_id: str
    healing_operation_id: str | None = None
    status: HealingStatus
    # Populated from the response's `preview_result` field if present,
    # else `diff` if present, else None. Untyped on the wire (Bright
    # Data's own type declares both as `unknown`), so kept as Any rather
    # than assumed to be a dict. Untrusted, as above.
    candidate_preview: Any | None = None
    # Untrusted, as above.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
