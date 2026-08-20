#!/usr/bin/env python3
"""Normalize one Bright Data Google SERP response into GapRadar records.

This module is deliberately semantic-free. It does not score, classify, or
filter a result by meaning; it only validates shape, normalizes stable fields,
deduplicates canonical URLs, and applies the caller's bound.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_SINGLE_PAGE_RESULTS = 10
_TRACKING_KEYS = frozenset({"fbclid", "gclid", "msclkid"})
_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _canonicalize_url(value: Any) -> tuple[str, str] | None:
    """Return (normalized URL, display domain), or None for unusable URLs."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None

        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None

    scheme = parsed.scheme.lower()
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"

    query_items = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
            continue
        query_items.append((key, item_value))

    normalized = urlunsplit(
        (scheme, netloc, path, urlencode(query_items, doseq=True), "")
    )
    domain = host.removeprefix("www.")
    return normalized, domain


def _normalize_position(result: dict[str, Any]) -> int | None:
    for field in ("rank", "global_rank"):
        value = result.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _normalize_date(result: dict[str, Any]) -> str | None:
    value = result.get("published_at") or result.get("date")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_results(
    payload: dict[str, Any], *, query: str, limit: int
) -> list[dict[str, Any]]:
    """Map Bright Data organic results onto the stable acquisition contract."""

    clean_query = _clean_text(query)
    if not clean_query:
        raise ValueError("query must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_SINGLE_PAGE_RESULTS:
        raise ValueError(
            f"limit must be between 1 and {MAX_SINGLE_PAGE_RESULTS} "
            "for the one-request contract"
        )

    organic = payload.get("organic", [])
    if not isinstance(organic, list):
        return []

    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for value in organic:
        if not isinstance(value, dict):
            continue

        title = _clean_text(value.get("title"))
        canonical = _canonicalize_url(value.get("link") or value.get("url"))
        if not title or canonical is None:
            continue

        url, domain = canonical
        if url in seen_urls:
            continue
        seen_urls.add(url)

        records.append(
            {
                "query": clean_query,
                "title": title,
                "url": url,
                "domain": domain,
                "snippet": _clean_text(
                    value.get("description") or value.get("snippet")
                ),
                "position": _normalize_position(value),
                "published_at": _normalize_date(value),
            }
        )
        if len(records) == limit:
            break

    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    records = normalize_results(payload, query=args.query, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
