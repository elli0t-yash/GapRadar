"""Configuration, discovery, and sanitization at the MCP client boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.cli.client import (
    ConfigurationError,
    ConnectionError,
    GapRadarClient,
    MCPConnectionConfig,
    ToolCallError,
    ToolContractError,
    ToolUnavailableError,
)

URL = "https://gapradar.example/mcp"
TOKEN = "boundary-secret-token"


class FakeSession:
    def __init__(
        self,
        *,
        tools: tuple[str, ...] = ("wanted",),
        result: Any = None,
    ) -> None:
        self.tools = tools
        self.result = result or SimpleNamespace(
            is_error=False,
            structured_content={"ok": True},
            content=[],
        )
        self.list_calls = 0
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        self.list_calls += 1
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in self.tools]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.tool_calls.append((name, arguments))
        return self.result


class FakeConnection:
    def __init__(
        self,
        session: FakeSession | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.session = session or FakeSession()
        self.error = error

    async def __aenter__(self) -> FakeSession:
        if self.error is not None:
            raise self.error
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


def config() -> MCPConnectionConfig:
    return MCPConnectionConfig(url=URL, api_key=TOKEN)


@pytest.mark.parametrize(
    ("environ", "url_override", "expected"),
    [
        ({"GAPRADAR_MCP_API_KEY": "key"}, None, "MCP URL"),
        ({"GAPRADAR_MCP_URL": URL}, None, "API key"),
        (
            {"GAPRADAR_MCP_URL": "not-a-url", "GAPRADAR_MCP_API_KEY": "key"},
            None,
            "malformed",
        ),
        (
            {
                "GAPRADAR_MCP_URL": "http://remote.example/mcp",
                "GAPRADAR_MCP_API_KEY": "key",
            },
            None,
            "HTTPS",
        ),
    ],
)
def test_invalid_connection_configuration_is_clear(
    environ: dict[str, str],
    url_override: str | None,
    expected: str,
) -> None:
    with pytest.raises(ConfigurationError, match=expected):
        MCPConnectionConfig.from_environment(
            environ=environ,
            url_override=url_override,
        )


def test_url_override_and_local_plain_http_are_supported() -> None:
    configured = MCPConnectionConfig.from_environment(
        environ={"GAPRADAR_MCP_API_KEY": "key"},
        url_override="http://127.0.0.1:8000/mcp",
    )

    assert configured.url == "http://127.0.0.1:8000/mcp"
    assert configured.api_key == "key"


@pytest.mark.asyncio
async def test_client_discovers_once_and_returns_structured_output() -> None:
    session = FakeSession()
    client = GapRadarClient(config(), connection=FakeConnection(session))

    async with client:
        first = await client.call_tool("wanted", {"limit": 2})
        second = await client.call_tool("wanted", {})

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert session.list_calls == 1
    assert session.tool_calls == [("wanted", {"limit": 2}), ("wanted", {})]


@pytest.mark.asyncio
async def test_missing_tool_reports_server_contract_drift() -> None:
    client = GapRadarClient(
        config(),
        connection=FakeConnection(FakeSession(tools=("different",))),
    )

    async with client:
        with pytest.raises(ToolUnavailableError, match="required tool: wanted"):
            await client.call_tool("wanted")


@pytest.mark.asyncio
async def test_tool_error_redacts_the_api_key() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(type="text", text=f"failure {TOKEN}")],
    )
    client = GapRadarClient(
        config(),
        connection=FakeConnection(FakeSession(result=result)),
    )

    async with client:
        with pytest.raises(ToolCallError) as raised:
            await client.call_tool("wanted")

    assert TOKEN not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


@pytest.mark.asyncio
async def test_missing_structured_output_is_a_contract_error() -> None:
    result = SimpleNamespace(is_error=False, structured_content=None, content=[])
    client = GapRadarClient(
        config(),
        connection=FakeConnection(FakeSession(result=result)),
    )

    async with client:
        with pytest.raises(ToolContractError, match="no structured result"):
            await client.call_tool("wanted")


@pytest.mark.asyncio
async def test_connection_failure_is_sanitized() -> None:
    client = GapRadarClient(
        config(),
        connection=FakeConnection(error=RuntimeError(f"wire {TOKEN}")),
    )

    with pytest.raises(ConnectionError) as raised:
        async with client:
            pass

    assert str(raised.value) == "GapRadar MCP connection failed unexpectedly."
    assert TOKEN not in str(raised.value)
