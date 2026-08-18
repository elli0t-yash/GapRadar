"""Source adapter for Razorpay's "Fix My Itch" page.

Two responsibilities, deliberately kept apart:

1. Source-native validation. FixMyItchRecord describes exactly what the
   production Bright Data collector emits for this source -- every field
   it sends and no others -- and enforces the source's own invariants
   (score ranges, required text, source identity). It repairs nothing: a
   record that violates the contract is reported as invalid, never
   coerced into a plausible-looking one, and nothing is stripped from a
   record before validation sees it.

2. Deterministic mapping onto the existing generic ingestion contract
   (app.ingestion.normalizer.normalize_record), so no new persistence
   path, identity scheme, or database column is introduced for this
   source.

Historical note -- the "TAM x10" bug: an earlier revision of the scraper
emitted tam_score values on a 0..100 scale (60, 70, 80, ...) instead of
the source's 1..10 scale. The current production scraper emits the
correct scale. The backend deliberately does NOT divide such values by
ten: silently rescaling would hide a broken upstream collector and
fabricate a number the source never published. tam_score = 60 therefore
fails validation here, loudly, and the raw record is preserved verbatim
in the report so the real upstream value stays visible.
"""

import enum
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.enums import SignalType
from app.ingestion.identity import resolve_external_id
from app.ingestion.normalizer import normalize_text, normalize_url
from app.ingestion.schemas import RawProviderRecord
from app.schemas._validators import non_blank

FIX_MY_ITCH_SOURCE = "fix_my_itch"
FIX_MY_ITCH_SOURCE_URL = "https://razorpay.com/m/fix-my-itch/"

# The page publishes a composite itch score on a 0..100 scale, and four
# component scores on a 1..10 scale. Both bounds are inclusive; the
# production fixture contains values sitting exactly on 0/100 and 10.
# allow_inf_nan=False rejects NaN/Infinity, which are finite-score
# violations rather than merely out-of-range ones.
ItchScore = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
ComponentScore = Annotated[float, Field(ge=1, le=10, allow_inf_nan=False)]

_TEXT_FIELDS = frozenset({"problem", "industry", "description"})
_SCORE_FIELDS = frozenset(
    {
        "itch_score",
        "severity_score",
        "tam_score",
        "whitespace_score",
        "frequency_score",
    }
)
# Pydantic error types that mean "the required text is not actually
# there", as opposed to "the value is of the wrong shape entirely".
_ABSENT_TEXT_ERROR_TYPES = frozenset({"missing", "string_too_short", "value_error"})


class FixMyItchInput(BaseModel):
    """The collector input Bright Data echoes back on every result row.

    Provider plumbing rather than source data, but modeled explicitly so
    it can be accepted without also accepting anything else -- and so a
    row scraped from some other URL is caught rather than waved through.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    url: Literal[FIX_MY_ITCH_SOURCE_URL]


class FixMyItchRecord(BaseModel):
    """One problem ("itch") row exactly as the production collector emits it.

    strict=True is intentional: the source emits JSON numbers for every
    score, so a string "7" is upstream shape drift and is surfaced rather
    than coerced. Ints are still accepted for the float fields (Pydantic
    strict mode widens int -> float), which the production payload relies
    on -- most scores arrive as ints, some as floats.

    extra="forbid" is the deliberate choice for a reliability-focused
    pipeline: an unannounced upstream schema change is exactly the signal
    RecallGuard exists to notice, so an unknown field fails the record
    loudly instead of being silently discarded. Every field the verified
    production payload sends -- including Bright Data's `input` echo --
    is therefore modeled here. `input` is optional because it is provider
    plumbing that a raw source payload need not carry, but when present
    it must match FixMyItchInput exactly.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    problem: str
    itch_score: ItchScore
    industry: str
    description: str
    severity_score: ComponentScore
    tam_score: ComponentScore
    whitespace_score: ComponentScore
    frequency_score: ComponentScore
    source: Literal[FIX_MY_ITCH_SOURCE]
    source_url: Literal[FIX_MY_ITCH_SOURCE_URL]
    input: FixMyItchInput | None = None

    @field_validator("problem", "industry", "description")
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        # Conservative normalization only: trim surrounding whitespace,
        # reject whitespace-only. The industry list is dynamic on the
        # source, so its value is never checked against a fixed
        # vocabulary -- an unseen industry is valid data, not an error.
        return non_blank(value)


class FixMyItchRejectionReason(str, enum.Enum):
    """Stable, source-specific reason codes.

    Deliberately separate from app.ingestion.schemas.RejectionReason:
    that enum describes failures of the generic ingestion contract, and
    these describe failures of this source's own contract, which happen
    strictly earlier.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_SCORE = "invalid_score"
    INVALID_SOURCE = "invalid_source"
    INVALID_SOURCE_URL = "invalid_source_url"
    INVALID_RECORD = "invalid_record"


class InvalidFixMyItchRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    reason: FixMyItchRejectionReason
    detail: str
    # The raw record as received, preserved verbatim for debugging.
    # Untrusted: never interpreted, never repaired.
    raw: Any = None


class ValidatedFixMyItchRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    # Identity as the ingestion pipeline will compute it (see
    # compute_external_id).
    external_id: str
    record: FixMyItchRecord


class DuplicateFixMyItchRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    # Index of the earlier record carrying the same identity.
    first_index: int
    external_id: str


class FixMyItchDatasetReport(BaseModel):
    """Outcome of validating one collector payload.

    Reports what the payload contains; enforces nothing about its size or
    shape. Record count, industry count, and rows per industry are all
    dynamic on the source, so no expectation is asserted about them here.

    `valid` keeps every record that passed source validation, in input
    order, including ones flagged in `duplicates` -- deduplication is the
    ingestion layer's job (and is idempotent), so this layer only reports.
    """

    model_config = ConfigDict(frozen=True)

    valid: list[ValidatedFixMyItchRecord] = Field(default_factory=list)
    invalid: list[InvalidFixMyItchRecord] = Field(default_factory=list)
    duplicates: list[DuplicateFixMyItchRecord] = Field(default_factory=list)


def _classify(error: dict[str, Any]) -> FixMyItchRejectionReason:
    field = error["loc"][0] if error["loc"] else None
    error_type = error["type"]
    if error_type == "missing":
        return FixMyItchRejectionReason.MISSING_REQUIRED_FIELD
    if field in _SCORE_FIELDS:
        return FixMyItchRejectionReason.INVALID_SCORE
    if field == "source":
        return FixMyItchRejectionReason.INVALID_SOURCE
    if field == "source_url":
        return FixMyItchRejectionReason.INVALID_SOURCE_URL
    if field in _TEXT_FIELDS and error_type in _ABSENT_TEXT_ERROR_TYPES:
        return FixMyItchRejectionReason.MISSING_REQUIRED_FIELD
    return FixMyItchRejectionReason.INVALID_RECORD


def describe_validation_error(
    exc: ValidationError,
) -> tuple[FixMyItchRejectionReason, str]:
    """Reduce a ValidationError to a stable reason code plus a detail
    string listing every violation.

    Deterministic: Pydantic reports errors in field declaration order, so
    the same bad record always yields the same reason and the same detail.
    """
    errors = exc.errors()
    reason = _classify(errors[0]) if errors else FixMyItchRejectionReason.INVALID_RECORD
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<record>'}: {error['msg']}"
        for error in errors
    )
    return reason, detail


def compute_external_id(record: FixMyItchRecord, *, source_id: uuid.UUID) -> str:
    """Identity this record will carry as Signal.external_id.

    Fix My Itch publishes no per-row identifier, so the existing
    deterministic content fingerprint (app.ingestion.identity) is used --
    no second dedup scheme is introduced. The inputs are normalized here
    exactly as app.ingestion.normalizer would normalize them, so this
    value is identical to the one the ingestion pipeline derives from
    to_raw_provider_record() output.
    """
    return resolve_external_id(
        provided_external_id=None,
        source_id=source_id,
        canonical_url=normalize_url(record.source_url),
        title=normalize_text(record.problem),
        body=normalize_text(record.description),
    )


def to_raw_provider_record(
    record: FixMyItchRecord, *, observed_at: datetime
) -> RawProviderRecord:
    """Map a validated record onto the generic ingestion input contract.

    Pure and deterministic: same record + same observed_at -> same dict.

    - external_id is deliberately absent, so the normalizer falls back to
      the deterministic content fingerprint (see compute_external_id).
    - observed_at must be supplied by the caller (typically the collector
      run's completion time): the source publishes no per-row timestamp,
      and this layer will not invent one. It never affects identity.
    - signal_type is PROBLEM: every row on this page is a stated
      unsolved problem, which is this source's first-class role in
      GapRadar's taxonomy.
    - The scoring inputs the Opportunity Engine will later need are
      preserved in the existing untrusted `metadata` payload rather than
      in new columns. Scores land as floats even where the source sent
      ints (Pydantic widens int -> float); no value is rescaled.
    """
    if observed_at.tzinfo is None or observed_at.tzinfo.utcoffset(observed_at) is None:
        raise ValueError("observed_at must be timezone-aware")

    return {
        "canonical_url": record.source_url,
        "title": record.problem,
        "body": record.description,
        "signal_type": SignalType.PROBLEM.value,
        "observed_at": observed_at,
        "metadata": {
            "source": record.source,
            "source_url": record.source_url,
            "industry": record.industry,
            "itch_score": record.itch_score,
            "severity_score": record.severity_score,
            "tam_score": record.tam_score,
            "whitespace_score": record.whitespace_score,
            "frequency_score": record.frequency_score,
        },
    }


def validate_dataset(
    raw_records: Sequence[Any], *, source_id: uuid.UUID
) -> FixMyItchDatasetReport:
    """Validate a whole collector payload, record by record.

    Every record is validated (a bad one never aborts the batch), invalid
    ones are reported with a deterministic reason, and repeats of an
    already-seen identity are reported as duplicates using the same
    identity mechanism ingestion uses.
    """
    valid: list[ValidatedFixMyItchRecord] = []
    invalid: list[InvalidFixMyItchRecord] = []
    duplicates: list[DuplicateFixMyItchRecord] = []
    first_index_by_external_id: dict[str, int] = {}

    for index, raw in enumerate(raw_records):
        try:
            record = FixMyItchRecord.model_validate(raw)
        except ValidationError as exc:
            reason, detail = describe_validation_error(exc)
            invalid.append(
                InvalidFixMyItchRecord(
                    index=index, reason=reason, detail=detail, raw=raw
                )
            )
            continue

        external_id = compute_external_id(record, source_id=source_id)
        first_index = first_index_by_external_id.get(external_id)
        if first_index is None:
            first_index_by_external_id[external_id] = index
        else:
            duplicates.append(
                DuplicateFixMyItchRecord(
                    index=index, first_index=first_index, external_id=external_id
                )
            )
        valid.append(
            ValidatedFixMyItchRecord(
                index=index, external_id=external_id, record=record
            )
        )

    return FixMyItchDatasetReport(valid=valid, invalid=invalid, duplicates=duplicates)
