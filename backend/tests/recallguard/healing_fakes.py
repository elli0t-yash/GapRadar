"""A scripted Bright Data endpoint covering self-healing AND collection.

One handler serves both, exactly as the real API does, so the healing
tests drive the genuine BrightDataClient and the genuine collection
orchestrator rather than stand-ins.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

HEAL_COLLECTION_ID = "j_verification_run"


class ScriptedProvider:
    """Serves the self-healing and collection endpoints from a script."""

    def __init__(
        self,
        *,
        progress: list[dict[str, Any]] | None = None,
        dataset: list[dict[str, Any]] | None = None,
        trigger_response: httpx.Response | None = None,
        heal_response: httpx.Response | None = None,
        progress_response: httpx.Response | None = None,
        resume_response: httpx.Response | None = None,
    ) -> None:
        self.progress = progress or []
        self.dataset = dataset if dataset is not None else []
        self.trigger_response = trigger_response
        self.heal_response = heal_response
        self.progress_response = progress_response
        self.resume_response = resume_response
        self.requests: list[httpx.Request] = []
        self.progress_index = 0

    # -- recorded traffic -------------------------------------------------
    def paths(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in r.url.path]

    @property
    def heal_requests(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.path.endswith("/refactor_template") and r.method == "POST"
        ]

    @property
    def resume_requests(self) -> list[httpx.Request]:
        return self.paths("resume_automation_job")

    @property
    def resume_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.resume_requests]

    @property
    def collection_triggers(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/dca/trigger"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path.endswith("/refactor_template") and request.method == "POST":
            return self.heal_response or httpx.Response(200, json={"ok": True})
        if path.endswith("/refactor_template/progress"):
            if self.progress_response is not None:
                return self.progress_response
            index = min(self.progress_index, len(self.progress) - 1)
            self.progress_index += 1
            return httpx.Response(200, json=self.progress[index])
        if path.endswith("/resume_automation_job"):
            return self.resume_response or httpx.Response(200, json={"ok": True})
        if path == "/dca/trigger":
            return self.trigger_response or httpx.Response(
                200, json={"collection_id": HEAL_COLLECTION_ID}
            )
        if path == "/dca/dataset":
            return httpx.Response(200, json=self.dataset)
        raise AssertionError(f"unexpected request path: {path}")


class HealClock:
    """Advances only when slept on, keeping ordering deterministic."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current += timedelta(seconds=seconds)


def awaiting_approval(preview: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "pending_answer", "step": "review_diff"}
    if preview is not None:
        payload["preview_result"] = preview
    return payload


def running() -> dict[str, Any]:
    return {"status": "in_progress", "step": "analysing"}


def done() -> dict[str, Any]:
    return {"status": "done", "completed_steps": ["analyse", "patch", "save"]}


def failed() -> dict[str, Any]:
    return {"status": "failed", "step": "patch"}
