import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.exceptions import register_exception_handlers
from app.logging_config import configure_logging
from app.mcp_server.http import MCPBearerAuthMiddleware
from app.mcp_server.server import create_mcp_server

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("app_startup")
    # Future resources (DB engine, integration clients) will be initialized here.
    try:
        yield
    finally:
        logger.info("app_shutdown")


def _composed_lifespan(mcp_server: MCPServer | None):
    @asynccontextmanager
    async def composed(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan(app):
            if mcp_server is None:
                yield
            else:
                async with mcp_server.session_manager.run():
                    yield

    return composed


async def _mcp_unavailable(_request: object) -> JSONResponse:
    return JSONResponse({"detail": "MCP service unavailable"}, status_code=503)


def create_app(
    settings: Settings | None = None,
    *,
    mcp_server: MCPServer | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.APP_ENV)

    mcp_transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts_list,
        allowed_origins=settings.mcp_allowed_origins_list,
    )
    configured_mcp_server: MCPServer | None = None
    if settings.mcp_api_key_is_configured:
        configured_mcp_server = mcp_server or create_mcp_server()
        mcp_http_app = configured_mcp_server.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            transport_security=mcp_transport_security,
        )
    else:
        mcp_http_app = Starlette(routes=[Route("/", _mcp_unavailable)])

    app = FastAPI(
        title="GapRadar API",
        version="0.1.0",
        lifespan=_composed_lifespan(configured_mcp_server),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router)
    app.mount("/mcp", mcp_http_app, name="mcp")
    app.add_middleware(
        MCPBearerAuthMiddleware,
        api_key=settings.GAPRADAR_MCP_API_KEY,
        transport_security=mcp_transport_security,
    )

    app.state.mcp_server = configured_mcp_server

    return app
