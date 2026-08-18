from fastapi import APIRouter

from app.api.v1.routes import (
    collectors,
    dashboard,
    health,
    opportunities,
    pipeline,
    reliability,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(dashboard.router)
api_router.include_router(opportunities.router)
api_router.include_router(reliability.router)
api_router.include_router(collectors.router)
api_router.include_router(pipeline.router)
