"""Authenticated Streamable HTTP adapter for GapRadar's in-process MCP server."""

from __future__ import annotations

import secrets

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.authentication import AuthCredentials
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PATH = "/mcp"


def is_mcp_path(path: str) -> bool:
    """Match the mount itself and descendants without catching `/mcpfoo`."""

    return path == MCP_PATH or path.startswith(f"{MCP_PATH}/")


class MCPBearerAuthMiddleware:
    """Fail-closed bearer authentication scoped strictly to the MCP mount.

    This is intentionally a small ASGI boundary rather than browser CORS or a
    frontend-visible credential. It runs before Starlette's mount redirect, so
    even a request to `/mcp` without its trailing slash is authenticated.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: str,
        transport_security: TransportSecuritySettings,
    ) -> None:
        self.app = app
        self._api_key = api_key
        self._enabled = len(api_key) >= 32
        self._allowed_hosts = tuple(transport_security.allowed_hosts)
        self._allowed_origins = tuple(transport_security.allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_mcp_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        if not self._enabled:
            await self._respond(
                scope,
                receive,
                send,
                status_code=503,
                detail="MCP service unavailable",
            )
            return

        candidate = self._bearer_token(scope)
        if candidate is None or not secrets.compare_digest(
            candidate.encode("utf-8"), self._api_key.encode("utf-8")
        ):
            await self._respond(
                scope,
                receive,
                send,
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
            return

        security_error = self._transport_error(scope)
        if security_error is not None:
            status_code, detail = security_error
            await self._respond(
                scope,
                receive,
                send,
                status_code=status_code,
                detail=detail,
            )
            return

        # Give the SDK a stable, secret-free identity so stateful sessions are
        # bound to the authenticated principal. The raw bearer token is never
        # placed in request state or logs.
        authenticated_scope = dict(scope)
        authenticated_scope["auth"] = AuthCredentials([])
        authenticated_scope["user"] = AuthenticatedUser(
            AccessToken(
                token="",
                client_id="gapradar-mcp-shared-key",
                scopes=[],
                subject="trusted-agent",
            )
        )
        await self.app(authenticated_scope, receive, send)

    @staticmethod
    def _bearer_token(scope: Scope) -> str | None:
        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return None

        scheme, separator, credential = values[0].partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not credential
            or any(character.isspace() for character in credential)
        ):
            return None
        return credential

    def _transport_error(self, scope: Scope) -> tuple[int, str] | None:
        # Validate before Starlette can construct a redirect from an untrusted
        # Host. Unlike the SDK's inner defense, this boundary deliberately does
        # not log attacker-controlled header values (which could contain the
        # bearer secret).
        if scope.get("method") == "POST":
            content_type = self._single_header(scope, b"content-type")
            if content_type is None or not content_type.lower().startswith(
                "application/json"
            ):
                return 400, "Invalid Content-Type header"

        host = self._single_header(scope, b"host")
        if host is None or not self._is_allowed(host, self._allowed_hosts):
            return 421, "Invalid Host header"

        origin = self._single_header(scope, b"origin")
        if origin is not None and not self._is_allowed(origin, self._allowed_origins):
            return 403, "Invalid Origin header"
        return None

    @staticmethod
    def _single_header(scope: Scope, name: bytes) -> str | None:
        values = [
            value.decode("latin-1")
            for header_name, value in scope.get("headers", [])
            if header_name.lower() == name
        ]
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _is_allowed(value: str, allowed_values: tuple[str, ...]) -> bool:
        if value in allowed_values:
            return True
        return any(
            allowed.endswith(":*") and value.startswith(f"{allowed[:-2]}:")
            for allowed in allowed_values
        )

    @staticmethod
    async def _respond(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = JSONResponse(
            {"detail": detail},
            status_code=status_code,
            headers=headers,
        )
        await response(scope, receive, send)
