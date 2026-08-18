"""Deterministic Bright Data self-healing prompts, built from evidence.

No LLM writes this. The prompt is assembled from what RecallGuard
actually observed -- which checks failed, what the contract requires, and
which values violated it -- so the same incident always produces the same
instruction, and every claim in it is traceable to a recorded fact.

Two rules shape the wording:

- Tell the scraper to extract what the page displays. The historical TAM
  bug produced values like 60 where the page shows a 1-10 score, and the
  correct repair is to read the right element, never to divide by ten.
  A prompt that says "rescale" would teach the scraper to fabricate.
- Keep the output schema. A repair that renames or drops fields breaks
  every downstream contract even if the values are right.
"""

from typing import Any

from pydantic import ValidationError

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


def build_heal_prompt(
    incident: ReliabilityIncident, *, source_url: str = FIX_MY_ITCH_SOURCE_URL
) -> str:
    """Compose the repair instruction for one incident.

    Sections are added in priority order and the result is trimmed to the
    provider's limit, so the most important context survives truncation.
    Contains no credentials, no environment values, and no raw dataset --
    only bounded, already-public field values that failed validation.
    """
    occurrence = _latest_occurrence(incident)
    sections = [
        f"The scraper for {source_url} is returning data that fails its contract.",
        f"Diagnosis: {incident.classification.value}.",
    ]

    failures = _failed_checks(occurrence)
    if failures:
        sections.append("Failed checks:")
        sections.extend(
            f"- {check['name']}: expected {check['expected']}; observed "
            f"{check['observed']}"
            for check in failures[:MAX_REPORTED_CHECKS]
        )

    violations = _field_violations(occurrence)
    if violations:
        sections.append("Wrong values returned:")
        sections.extend(f"- {line}" for line in violations[:MAX_REPORTED_VIOLATIONS])

    sections.append(f"Required: {_CONTRACT_RULES}")
    return _fit("\n".join(sections))


def _latest_occurrence(incident: ReliabilityIncident) -> dict[str, Any]:
    occurrences = (incident.evidence or {}).get("occurrences") or []
    return occurrences[-1] if isinstance(occurrences[-1:], list) and occurrences else {}


def _failed_checks(occurrence: dict[str, Any]) -> list[dict[str, Any]]:
    checks = occurrence.get("checks") or []
    return [
        check
        for check in checks
        if isinstance(check, dict) and check.get("passed") is False
    ]


def _field_violations(occurrence: dict[str, Any]) -> list[str]:
    """Name the offending fields and their actual values.

    Derived by re-validating each preserved record against the source
    contract, so the expectations quoted in the prompt are the contract
    itself rather than a second, drifting description of it.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for violation in occurrence.get("sample_violations") or []:
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
