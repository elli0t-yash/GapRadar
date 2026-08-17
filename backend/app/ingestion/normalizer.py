import re
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.domain.enums import SignalType
from app.ingestion.identity import resolve_external_id
from app.ingestion.schemas import NormalizedSignal, RawProviderRecord, RejectionReason

# Column length limits, mirrored from app/db/models/signal.py, checked
# proactively so an oversized value is rejected with a clear reason
# instead of surfacing as an unhandled DB-level DataError (a different
# exception class than IntegrityError, which the ingestion service does
# not otherwise attempt to catch).
_EXTERNAL_ID_MAX_LEN = 255
_CANONICAL_URL_MAX_LEN = 2048
_TITLE_MAX_LEN = 1024

_HORIZONTAL_WHITESPACE_RUN_RE = re.compile(r"[ \t\f\v]+")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


class RecordRejectedError(Exception):
    """Raised internally when a raw provider record fails validation.

    Carries a stable reason code and a human-readable detail; callers
    (app.ingestion.service) catch this and turn it into a RejectedRecord
    rather than letting it propagate.
    """

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


def normalize_text(value: str) -> str:
    """Trim surrounding whitespace and collapse pathological repeated
    whitespace. Never paraphrases, summarizes, truncates, or otherwise
    changes semantic content -- only whitespace shape.
    """
    stripped = value.strip()
    collapsed = _HORIZONTAL_WHITESPACE_RUN_RE.sub(" ", stripped)
    collapsed = _EXCESS_BLANK_LINES_RE.sub("\n\n", collapsed)
    return collapsed


def normalize_url(raw_url: str) -> str:
    """Parse and validate an http(s) URL, normalizing obvious equivalent
    forms:

    - scheme and host lowercased
    - fragment (#...) removed -- fragments are a client-side navigation
      aid, never part of the resource identity the server returns
    - a single trailing slash on a non-root path is removed, so
      "https://x.com/a" and "https://x.com/a/" normalize identically
    - the query string is preserved verbatim, in its original order --
      query parameters can be content-identifying (e.g. "?id=123"), so
      they are never indiscriminately stripped

    Raises ValueError if the URL is not a well-formed http/https URL.
    """
    parts = urlsplit(raw_url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parts.scheme!r}")
    if not parts.netloc:
        raise ValueError("URL is missing a host")

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            parts.query,
            "",
        )
    )


def parse_timestamp(value: Any) -> datetime:
    """Parse a timestamp (already a datetime, or an ISO-8601 string) and
    require it to be timezone-aware; convert to UTC.

    A naive (timezone-less) timestamp is rejected rather than assumed to
    be UTC -- silently assuming a timezone risks misrepresenting when an
    event actually happened, which this pipeline will not guess at.
    """
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp string is blank")
        try:
            candidate = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"could not parse timestamp: {value!r}") from exc
    else:
        raise TypeError(f"unsupported timestamp type: {type(value).__name__}")

    if candidate.tzinfo is None or candidate.tzinfo.utcoffset(candidate) is None:
        raise ValueError("timestamp must be timezone-aware")
    return candidate.astimezone(UTC)


def normalize_record(
    raw: RawProviderRecord, *, source_id: uuid.UUID
) -> NormalizedSignal:
    """Validate, normalize, and resolve identity for one raw provider
    record. Raises RecordRejectedError with a stable reason code on any
    failure; never invents a value for a field that is absent.
    """
    if not isinstance(raw, dict):
        raise RecordRejectedError(
            RejectionReason.INVALID_RECORD, "record is not an object"
        )

    canonical_url_raw = raw.get("canonical_url")
    title_raw = raw.get("title")
    body_raw = raw.get("body")
    signal_type_raw = raw.get("signal_type")
    observed_at_raw = raw.get("observed_at")
    metadata_raw = raw.get("metadata")
    external_id_raw = raw.get("external_id")

    if not isinstance(canonical_url_raw, str) or not canonical_url_raw.strip():
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD, "canonical_url is required"
        )
    if not isinstance(title_raw, str) or not title_raw.strip():
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD, "title is required"
        )
    if not isinstance(body_raw, str) or not body_raw.strip():
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD, "body is required"
        )
    if signal_type_raw is None:
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD, "signal_type is required"
        )
    if observed_at_raw is None:
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD, "observed_at is required"
        )

    try:
        canonical_url = normalize_url(canonical_url_raw)
    except ValueError as exc:
        raise RecordRejectedError(RejectionReason.INVALID_URL, str(exc)) from exc
    if len(canonical_url) > _CANONICAL_URL_MAX_LEN:
        raise RecordRejectedError(
            RejectionReason.INVALID_RECORD,
            f"canonical_url exceeds {_CANONICAL_URL_MAX_LEN} characters",
        )

    title = normalize_text(title_raw)
    body = normalize_text(body_raw)
    if not title:
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD,
            "title is blank after normalization",
        )
    if not body:
        raise RecordRejectedError(
            RejectionReason.MISSING_REQUIRED_FIELD,
            "body is blank after normalization",
        )
    if len(title) > _TITLE_MAX_LEN:
        raise RecordRejectedError(
            RejectionReason.INVALID_RECORD,
            f"title exceeds {_TITLE_MAX_LEN} characters",
        )

    if not isinstance(signal_type_raw, str):
        raise RecordRejectedError(
            RejectionReason.INVALID_SIGNAL_TYPE,
            f"signal_type must be a string, got {type(signal_type_raw).__name__}",
        )
    try:
        signal_type = SignalType(signal_type_raw)
    except ValueError as exc:
        raise RecordRejectedError(
            RejectionReason.INVALID_SIGNAL_TYPE,
            f"unrecognized signal_type: {signal_type_raw!r}",
        ) from exc

    try:
        observed_at = parse_timestamp(observed_at_raw)
    except (ValueError, TypeError) as exc:
        raise RecordRejectedError(RejectionReason.INVALID_TIMESTAMP, str(exc)) from exc

    if metadata_raw is None:
        metadata: dict[str, Any] = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        raise RecordRejectedError(
            RejectionReason.INVALID_RECORD, "metadata must be an object if present"
        )

    external_id_candidate = (
        external_id_raw.strip()
        if isinstance(external_id_raw, str) and external_id_raw.strip()
        else None
    )
    external_id = resolve_external_id(
        provided_external_id=external_id_candidate,
        source_id=source_id,
        canonical_url=canonical_url,
        title=title,
        body=body,
    )
    if len(external_id) > _EXTERNAL_ID_MAX_LEN:
        raise RecordRejectedError(
            RejectionReason.INVALID_RECORD,
            f"external_id exceeds {_EXTERNAL_ID_MAX_LEN} characters",
        )

    return NormalizedSignal(
        external_id=external_id,
        canonical_url=canonical_url,
        title=title,
        body=body,
        signal_type=signal_type,
        observed_at=observed_at,
        metadata=metadata,
    )
