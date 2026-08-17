import hashlib
import uuid

# Fallback identities are prefixed so they are visually distinguishable in
# the database from a genuine provider-supplied external_id.
_FALLBACK_PREFIX = "fp:"


def compute_fallback_identity(
    *, source_id: uuid.UUID, canonical_url: str, title: str, body: str
) -> str:
    """Deterministic fallback identity for a Signal when the provider does
    not supply a trustworthy external_id.

    Formula: SHA-256 hex digest of the UTF-8 encoded, newline-joined tuple

        (source_id, canonical_url, title, body)

    using values that have already been normalized (whitespace-collapsed
    text, URL-normalized) by app.ingestion.normalizer -- callers MUST
    normalize before calling this, so that cosmetically different but
    logically identical content (e.g. a URL differing only by a trailing
    slash, or incidental whitespace differences) still produces the same
    fingerprint.

    Deliberately excludes any value that can legitimately differ between
    repeat observations of the same logical item across collector runs:
    collector_run_id, ingestion time, and any provider-supplied
    timestamp (including observed_at) are never part of this formula.

    Python's built-in hash() is never used here, since it is salted with
    PYTHONHASHSEED and is not stable across processes -- the same input
    would produce a different value in a later run, defeating
    deduplication entirely.
    """
    payload = "\n".join((str(source_id), canonical_url, title, body))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_FALLBACK_PREFIX}{digest}"


def resolve_external_id(
    *,
    provided_external_id: str | None,
    source_id: uuid.UUID,
    canonical_url: str,
    title: str,
    body: str,
) -> str:
    """Resolve the identity to persist as Signal.external_id.

    Priority:
    1. The provider's own external_id, if present and non-blank -- it is
       the more specific, provider-native identity.
    2. Otherwise, a deterministic content fingerprint (see
       compute_fallback_identity).
    """
    if provided_external_id:
        return provided_external_id
    return compute_fallback_identity(
        source_id=source_id, canonical_url=canonical_url, title=title, body=body
    )
