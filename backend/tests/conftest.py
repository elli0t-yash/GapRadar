import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_ENV="test", DATABASE_URL="", CORS_ORIGINS="http://localhost:5173"
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    return TestClient(app)
