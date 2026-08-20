"""One authenticated Streamable HTTP client boundary for all CLI commands."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


class GapRadarCLIClientError(Exception):
    """Base class for sanitized, user-facing client failures."""


class ConfigurationError(GapRadarCLIClientError):
    """Required local connection configuration is invalid or absent."""


class AuthenticationError(GapRadarCLIClientError):
    """The remote MCP bearer boundary rejected this client."""


class ConnectionError(GapRadarCLIClientError):
    """The remote MCP service could not complete a protocol operation."""

    def __init__(self, message: str, *, action_may_have_completed: bool = False):
        super().__init__(message)
        self.action_may_have_completed = action_may_have_completed


class ToolUnavailableError(GapRadarCLIClientError):
    """The connected server does not expose the command's required tool."""


class ToolCallError(GapRadarCLIClientError):
    """GapRadar rejected or could not complete one tool operation."""


class ToolContractError(GapRadarCLIClientError):
    """The server returned no structured result for a structured tool."""


class _AuthenticationRejected(Exception):
    pass


class _ServiceUnavailable(Exception):
    pass


class _HostPolicyRejected(Exception):
    pass


@dataclass(frozen=True)
class MCPConnectionConfig:
    """Validated endpoint and secret read from local process configuration."""

    url: str
    api_key: str

    @classmethod
    def from_environment(
        cls,
        *,
        url_override: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> MCPConnectionConfig:
        values = os.environ if environ is None else environ
        raw_url = url_override if url_override is not None else values.get(
            "GAPRADAR_MCP_URL"
        )
        if raw_url is None or not raw_url.strip():
            raise ConfigurationError(
                "GapRadar MCP URL is not configured. Set GAPRADAR_MCP_URL."
            )

        url = raw_url.strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "GapRadar MCP URL is malformed. Use an http:// or https:// MCP endpoint."
            )
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise ConfigurationError(
                "GapRadar MCP URL must use HTTPS unless it targets the local machine."
            )

        api_key = values.get("GAPRADAR_MCP_API_KEY")
        if api_key is None or not api_key:
            raise ConfigurationError(
                "GapRadar MCP API key is not configured. Set GAPRADAR_MCP_API_KEY."
            )
        return cls(url=url, api_key=api_key)


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class _MCPClient(Protocol):
    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


async def _inspect_response(response: httpx2.Response) -> None:
    """Classify the HTTP boundary without reading or logging response bodies."""

    if response.status_code == 401:
        raise _AuthenticationRejected
    if response.status_code == 503:
        raise _ServiceUnavailable
    if response.status_code in {403, 421}:
        raise _HostPolicyRejected


@asynccontextmanager
async def authenticated_mcp_connection(
    config: MCPConnectionConfig,
) -> AsyncIterator[_MCPClient]:
    """Open one official MCP v2 Streamable HTTP client session."""

    headers = {"Authorization": f"Bearer {config.api_key}"}
    async with httpx2.AsyncClient(
        headers=headers,
        follow_redirects=True,
        event_hooks={"response": [_inspect_response]},
    ) as http_client:
        transport = streamable_http_client(
            config.url,
            http_client=http_client,
        )
        async with Client(
            transport,
            mode="legacy",
            read_timeout_seconds=30,
        ) as client:
            yield client


class GapRadarClient:
    """Discover and invoke structured GapRadar tools over one MCP session."""

    def __init__(
        self,
        config: MCPConnectionConfig,
        *,
        connection: AbstractAsyncContextManager[_MCPClient] | None = None,
    ) -> None:
        self.config = config
        self._connection = connection or authenticated_mcp_connection(config)
        self._client: _MCPClient | None = None
        self._tool_names: set[str] | None = None
        self._write_sent = False

    async def __aenter__(self) -> Self:
        try:
            self._client = await self._connection.__aenter__()
        except Exception as exc:  # noqa: BLE001 - sanitize the remote boundary
            raise _connection_failure(exc, api_key=self.config.api_key) from None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return await self._connection.__aexit__(
                exc_type,
                exc_value,
                traceback,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize cleanup failures
            if exc_value is not None:
                return False
            failure = _connection_failure(exc, api_key=self.config.api_key)
            if isinstance(failure, ConnectionError):
                failure.action_may_have_completed = self._write_sent
            raise failure from None
        finally:
            self._client = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> dict[str, Any]:
        client = self._require_connected()
        await self._require_tool(client, tool_name)

        if write:
            self._write_sent = True
        try:
            result = await client.call_tool(tool_name, arguments or {})
        except Exception as exc:  # noqa: BLE001 - sanitize the remote boundary
            failure = _connection_failure(
                exc,
                api_key=self.config.api_key,
                action_may_have_completed=write,
            )
            raise failure from None

        if result.is_error:
            message = _tool_error_text(result, api_key=self.config.api_key)
            raise ToolCallError(message)
        if not isinstance(result.structured_content, dict):
            raise ToolContractError(
                f"GapRadar server returned no structured result for tool: {tool_name}"
            )
        return result.structured_content

    async def _require_tool(self, client: _MCPClient, tool_name: str) -> None:
        if self._tool_names is None:
            try:
                tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 - sanitize discovery failures
                raise _connection_failure(exc, api_key=self.config.api_key) from None
            self._tool_names = {tool.name for tool in tools.tools}
        if tool_name not in self._tool_names:
            raise ToolUnavailableError(
                f"GapRadar server does not expose required tool: {tool_name}"
            )

    def _require_connected(self) -> _MCPClient:
        if self._client is None:
            raise RuntimeError("GapRadarClient must be used as an async context manager")
        return self._client


def _tool_error_text(result: Any, *, api_key: str) -> str:
    messages = [
        item.text.strip()
        for item in result.content
        if getattr(item, "type", None) == "text"
        and isinstance(getattr(item, "text", None), str)
        and item.text.strip()
    ]
    message = messages[0] if messages else "GapRadar could not complete the request."
    return _redact(message, api_key)


def _connection_failure(
    exc: BaseException,
    *,
    api_key: str,
    action_may_have_completed: bool = False,
) -> AuthenticationError | ConnectionError:
    exceptions = tuple(_exception_tree(exc))
    if any(isinstance(item, _AuthenticationRejected) for item in exceptions):
        return AuthenticationError(
            "GapRadar MCP authentication failed. Check GAPRADAR_MCP_API_KEY."
        )
    if any(isinstance(item, _ServiceUnavailable) for item in exceptions):
        return ConnectionError("GapRadar MCP service is unavailable.")
    if any(isinstance(item, _HostPolicyRejected) for item in exceptions):
        return ConnectionError(
            "GapRadar MCP rejected this host or origin. Check the server allowlist."
        )
    if any(
        isinstance(
            item,
            (
                httpx2.HTTPError,
                OSError,
                TimeoutError,
                MCPError,
            ),
        )
        for item in exceptions
    ):
        return ConnectionError(
            "Could not connect to the GapRadar MCP server.",
            action_may_have_completed=action_may_have_completed,
        )
    return ConnectionError(
        _redact("GapRadar MCP connection failed unexpectedly.", api_key),
        action_may_have_completed=action_may_have_completed,
    )


def _exception_tree(exc: BaseException) -> list[BaseException]:
    items = [exc]
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            items.extend(_exception_tree(nested))
    return items


def _redact(message: str, api_key: str) -> str:
    return message.replace(api_key, "[REDACTED]") if api_key else message
