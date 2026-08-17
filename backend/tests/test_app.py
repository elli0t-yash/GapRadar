from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app


def test_create_app_returns_fastapi_instance(settings: Settings) -> None:
    app = create_app(settings=settings)

    assert isinstance(app, FastAPI)
    assert app.title == "GapRadar API"


def test_app_includes_v1_health_route(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200


def test_unhandled_exception_returns_500(
    client: TestClient, settings: Settings
) -> None:
    app = create_app(settings=settings)

    @app.get("/__boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as boom_client:
        response = boom_client.get("/__boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
