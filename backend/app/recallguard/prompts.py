"""Deterministic Bright Data self-healing prompts, built from evidence.

No LLM writes this. The prompt is assembled from what RecallGuard
actually observed -- which checks failed, what the contract requires, and
which values violated it -- so the same incident always produces the same
instruction, and every claim in it is traceable to a recorded fact.

Three rules shape the wording:

- Tell the scraper to extract what the page displays. The historical TAM
  bug produced values like 60 where the page shows a 1-10 score, and the
  correct repair is to read the right element, never to divide by ten.
  A prompt that says "rescale" would teach the scraper to fabricate.
- Keep the output schema. A repair that renames or drops fields breaks
  every downstream contract even if the values are right.
- Describe the failure that is open NOW, not the one that opened the
  incident. An incident outlives its first symptom: a repair can fix the
  values it was asked about and break coverage doing it, and the next
  prompt has to be about the breakage, not about the values that are now
  correct. Which is exactly what happened -- a repair that fixed tam_score
  also shipped `categories.slice(0, 1)`, so the next collection returned
  one category's worth of records and the stale prompt would have asked
  for the tam_score fix a second time.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.fields import FieldInfo

from app.db.models import ReliabilityIncident
from app.integrations.brightdata.fix_my_itch import (
    FIX_MY_ITCH_SOURCE_URL,
    FixMyItchRecord,
)

# Bright Data caps a self-healing prompt at 1000 characters (enforced by
# HealingRequest.prompt). The builder trims to fit rather than risking a
# rejected request.
MAX_PROMPT_CHARS = 1000

# How much evidence is worth spending characters on.
MAX_REPORTED_CHECKS = 3
MAX_REPORTED_VIOLATIONS = 4

_CONTRACT_RULES = (
    "Extract the values displayed on the page. "
    "Never rescale, round, or otherwise transform them. "
    "Keep the existing output schema and field names unchanged."
)

# Said whenever completeness failed, in whatever way. Deliberately
# generic: it names no category, no count, and no site structure, because
# the same mistake -- a repair that leaves a preview-sized sample in the
# production path -- is not specific to the one that caused it.
_COVERAGE_RULES = (
    "Restore full category traversal and collection coverage. "
    "Do not restrict production collection to a preview subset or first "
    "category."
)

# The check whose failure means "too little data", as recorded in
# evidence by app.recallguard.detection.check_completeness.
_COMPLETENESS_CHECK = "completeness"


def build_heal_prompt(
    incident: ReliabilityIncident, *, source_url: str = FIX_MY_ITCH_SOURCE_URL
) -> str:
    """Compose the repair instruction for one incident.

    Sections are added in priority order and the result is trimmed to the
    provider's limit, so the most important context survives truncation.
    Contains no credentials, no environment values, and no raw dataset --
    only bounded, already-public field values that failed validation.
    """
    failure = _latest_failure(incident)
    sections = [
        f"The scraper for {source_url} is returning data that fails its contract.",
        f"Diagnosis: {incident.classification.value}.",
    ]

    if failure.failed_checks:
        sections.append("Failed checks:")
        sections.extend(
            f"- {check['name']}: expected {check['expected']}; observed "
            f"{check['observed']}"
            for check in failure.failed_checks[:MAX_REPORTED_CHECKS]
        )

    violations = _field_violations(failure.sample_violations)
    if violations:
        sections.append("Wrong values returned:")
        sections.extend(f"- {line}" for line in violations[:MAX_REPORTED_VIOLATIONS])

    if _completeness_failed(failure.failed_checks):
        sections.append(f"Coverage: {_COVERAGE_RULES}")
        if not violations:
            # Too few records, but every record that did arrive was
            # valid. Saying so stops the repair from "fixing" extraction
            # that is already correct -- the exact regression that
            # produced this state, in reverse.
            preserved = _preserved_value_rule()
            if preserved:
                sections.append(preserved)

    sections.append(f"Required: {_CONTRACT_RULES}")
    return _fit("\n".join(sections))


class _FailureEvidence(BaseModel):
    """One recorded failure, whatever shape it was recorded in.

    RecallGuard writes failures in two places and two shapes: a detection
    failure becomes an entry in evidence["occurrences"] carrying every
    check with a `passed` flag, while a post-repair verification failure
    becomes a "verification_failed" entry in evidence["events"] carrying
    only the checks that failed. Normalizing both to this makes the
    prompt builder indifferent to which one is newer, which is the whole
    point -- the newest failure is the one worth repairing.
    """

    model_config = ConfigDict(frozen=True)

    at: datetime | None = None
    failed_checks: list[dict[str, Any]] = Field(default_factory=list)
    sample_violations: list[dict[str, Any]] = Field(default_factory=list)


def _latest_failure(incident: ReliabilityIncident) -> _FailureEvidence:
    """The most recent unresolved failure, from either evidence stream.

    Ties and unreadable timestamps fall back to the occurrence, which is
    the historical behavior: a verification failure only wins when it can
    prove it is strictly newer.
    """
    evidence = incident.evidence or {}
    occurrence = _latest_occurrence(evidence)
    verification = _latest_verification_failure(evidence)

    if verification is None:
        return occurrence
    if occurrence.at is None or verification.at is None:
        return verification if occurrence.at is None else occurrence
    return verification if verification.at > occurrence.at else occurrence


def _latest_occurrence(evidence: dict[str, Any]) -> _FailureEvidence:
    occurrences = [
        entry
        for entry in (evidence.get("occurrences") or [])
        if isinstance(entry, dict)
    ]
    if not occurrences:
        return _FailureEvidence()
    latest = occurrences[-1]
    return _FailureEvidence(
        at=_parse_at(latest.get("detected_at")),
        failed_checks=[
            check
            for check in (latest.get("checks") or [])
            if isinstance(check, dict) and check.get("passed") is False
        ],
        sample_violations=[
            violation
            for violation in (latest.get("sample_violations") or [])
            if isinstance(violation, dict)
        ],
    )


def _latest_verification_failure(evidence: dict[str, Any]) -> _FailureEvidence | None:
    """The last time a repair was disproved by a fresh production run.

    Read from the lifecycle event log rather than the occurrence list
    because verification failures are recorded there -- they judge a run
    that was collected to test a repair, not a run that opened an
    incident.
    """
    events = [
        entry
        for entry in (evidence.get("events") or [])
        if isinstance(entry, dict) and entry.get("event") == "verification_failed"
    ]
    if not events:
        return None
    latest = events[-1]
    return _FailureEvidence(
        at=_parse_at(latest.get("at")),
        # Already filtered to failures when written; no `passed` flag to
        # filter on here.
        failed_checks=[
            check
            for check in (latest.get("failed_checks") or [])
            if isinstance(check, dict)
        ],
        sample_violations=[
            violation
            for violation in (latest.get("sample_violations") or [])
            if isinstance(violation, dict)
        ],
    )


def _parse_at(value: Any) -> datetime | None:
    """Read a recorded timestamp, treating a naive one as UTC.

    Everything RecallGuard writes is timezone-aware, but a naive value
    must not make two failures incomparable -- that would silently fall
    back to the stale one.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _completeness_failed(failed_checks: list[dict[str, Any]]) -> bool:
    return any(check.get("name") == _COMPLETENESS_CHECK for check in failed_checks)


def _preserved_value_rule() -> str | None:
    """State the numeric contract the current extraction is already meeting.

    Read off the source model, so the ranges quoted are the contract
    itself. Nothing about the current production volume or the site's
    category list appears here -- those are observations that move, and a
    prompt that hardcoded them would go stale the moment the source grew.
    """
    groups: dict[tuple[Any, Any], list[str]] = {}
    for name, field in FixMyItchRecord.model_fields.items():
        bounds = _numeric_bounds(field)
        if bounds is not None:
            groups.setdefault(bounds, []).append(name)
    if not groups:
        return None
    ranges = "; ".join(
        f"{', '.join(names)} {low}-{high}" for (low, high), names in groups.items()
    )
    return f"Preserve the current valid extraction: {ranges}."


def _numeric_bounds(field: FieldInfo) -> tuple[Any, Any] | None:
    low = high = None
    for constraint in field.metadata:
        low = getattr(constraint, "ge", None) if low is None else low
        high = getattr(constraint, "le", None) if high is None else high
    return (low, high) if low is not None and high is not None else None


def _field_violations(sample_violations: list[dict[str, Any]]) -> list[str]:
    """Name the offending fields and their actual values.

    Derived by re-validating each preserved record against the source
    contract, so the expectations quoted in the prompt are the contract
    itself rather than a second, drifting description of it.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for violation in sample_violations:
        raw = violation.get("raw") if isinstance(violation, dict) else None
        if not isinstance(raw, dict):
            continue
        for field, message, value in _contract_errors(raw):
            key = f"{field}={value!r}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{field} returned {value!r} but {message}")
    return lines


def _contract_errors(raw: dict[str, Any]) -> list[tuple[str, str, Any]]:
    try:
        FixMyItchRecord.model_validate(raw)
    except ValidationError as exc:
        return [
            (
                str(error["loc"][0]) if error["loc"] else "record",
                _expectation(error),
                raw.get(str(error["loc"][0])) if error["loc"] else None,
            )
            for error in exc.errors()
        ]
    return []


def _expectation(error: dict[str, Any]) -> str:
    """Restate one contract violation as what the field must be."""
    error_type = error["type"]
    context = error.get("ctx") or {}
    if error_type == "less_than_equal":
        return f"must be at most {context.get('le')}"
    if error_type == "greater_than_equal":
        return f"must be at least {context.get('ge')}"
    if error_type == "missing":
        return "is required"
    if error_type == "extra_forbidden":
        return "is not part of the output schema"
    if error_type == "literal_error":
        return f"must be {context.get('expected')}"
    return error["msg"]


def _fit(prompt: str) -> str:
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    # Trim on a line boundary so the prompt never ends mid-instruction.
    kept: list[str] = []
    budget = MAX_PROMPT_CHARS
    for line in prompt.split("\n"):
        if len(line) + 1 > budget:
            break
        kept.append(line)
        budget -= len(line) + 1
    return "\n".join(kept)
