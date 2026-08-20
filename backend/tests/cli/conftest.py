"""Controlled MCP client doubles for command-level CLI tests."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, Self

import pytest
from click.testing import CliRunner

from app.cli import main
from app.cli.client import MCPConnectionConfig

TEST_URL = "https://gapradar.example/mcp"
TEST_TOKEN = "cli-test-token-that-must-never-be-printed"


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.responses: dict[str, dict[str, Any] | list[dict[str, Any]]] = {}
        self.errors: dict[str, Exception] = {}
        self.enter_error: Exception | None = None

    async def __aenter__(self) -> Self:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, arguments or {}, write))
        if tool_name in self.errors:
            raise self.errors[tool_name]
        response = self.responses.get(tool_name, {})
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no response remains for {tool_name}")
            return deepcopy(response.pop(0))
        return deepcopy(response)


class RecordingClientFactory:
    def __init__(self, client: RecordingClient) -> None:
        self.client = client
        self.configs: list[MCPConnectionConfig] = []

    def __call__(self, config: MCPConnectionConfig) -> RecordingClient:
        self.configs.append(config)
        return self.client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def recording_client(
    monkeypatch: pytest.MonkeyPatch,
) -> RecordingClient:
    client = RecordingClient()
    factory = RecordingClientFactory(client)
    monkeypatch.setattr(main, "CLIENT_FACTORY", factory)
    monkeypatch.setenv("GAPRADAR_MCP_URL", TEST_URL)
    monkeypatch.setenv("GAPRADAR_MCP_API_KEY", TEST_TOKEN)
    return client


@pytest.fixture
def make_interactive(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    def apply() -> None:
        monkeypatch.setattr(main, "stdin_is_interactive", lambda: True)

    return apply
