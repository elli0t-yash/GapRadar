import uuid

from app.ingestion.identity import compute_fallback_identity, resolve_external_id


def test_resolve_external_id_prefers_provided_id() -> None:
    source_id = uuid.uuid4()
    result = resolve_external_id(
        provided_external_id="post-1",
        source_id=source_id,
        canonical_url="https://example.com/a",
        title="T",
        body="B",
    )
    assert result == "post-1"


def test_resolve_external_id_falls_back_to_fingerprint_when_missing() -> None:
    source_id = uuid.uuid4()
    result = resolve_external_id(
        provided_external_id=None,
        source_id=source_id,
        canonical_url="https://example.com/a",
        title="T",
        body="B",
    )
    assert result.startswith("fp:")
    assert result == compute_fallback_identity(
        source_id=source_id, canonical_url="https://example.com/a", title="T", body="B"
    )


def test_fallback_identity_is_deterministic_same_content_same_id() -> None:
    source_id = uuid.uuid4()
    first = compute_fallback_identity(
        source_id=source_id,
        canonical_url="https://example.com/a",
        title="Same title",
        body="Same body",
    )
    second = compute_fallback_identity(
        source_id=source_id,
        canonical_url="https://example.com/a",
        title="Same title",
        body="Same body",
    )
    assert first == second


def test_fallback_identity_differs_for_different_content() -> None:
    source_id = uuid.uuid4()
    a = compute_fallback_identity(
        source_id=source_id, canonical_url="https://example.com/a", title="T1", body="B"
    )
    b = compute_fallback_identity(
        source_id=source_id, canonical_url="https://example.com/a", title="T2", body="B"
    )
    assert a != b


def test_fallback_identity_differs_across_sources_for_same_content() -> None:
    a = compute_fallback_identity(
        source_id=uuid.uuid4(),
        canonical_url="https://example.com/a",
        title="T",
        body="B",
    )
    b = compute_fallback_identity(
        source_id=uuid.uuid4(),
        canonical_url="https://example.com/a",
        title="T",
        body="B",
    )
    assert a != b


def test_fallback_identity_is_a_sha256_hex_digest() -> None:
    source_id = uuid.uuid4()
    result = compute_fallback_identity(
        source_id=source_id, canonical_url="https://example.com/a", title="T", body="B"
    )
    digest = result.removeprefix("fp:")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


def test_fallback_identity_stable_across_separate_process_style_calls() -> None:
    # Simulates "separate executions" by recomputing from scratch with no
    # shared state (no caching, no seeded randomness) -- this is the
    # property that Python's built-in hash() would violate due to
    # PYTHONHASHSEED randomization, which is exactly why it is not used.
    source_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    results = {
        compute_fallback_identity(
            source_id=source_id,
            canonical_url="https://example.com/stable",
            title="Stable title",
            body="Stable body",
        )
        for _ in range(5)
    }
    assert len(results) == 1


def test_blank_string_external_id_treated_as_missing() -> None:
    # An empty/whitespace-only external_id from the provider is not
    # "trustworthy" and should fall back to the fingerprint, not be
    # persisted as a blank identity.
    source_id = uuid.uuid4()
    result = resolve_external_id(
        provided_external_id="",
        source_id=source_id,
        canonical_url="https://example.com/a",
        title="T",
        body="B",
    )
    assert result.startswith("fp:")
