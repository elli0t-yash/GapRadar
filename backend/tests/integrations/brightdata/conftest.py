from collections.abc import Callable

import httpx
import pytest

from app.config import Settings
from app.integrations.brightdata.client import BrightDataClient


@pytest.fixture
def brightdata_settings() -> Settings:
    return Settings(
        _env_file=None,
        BRIGHTDATA_API_KEY="test-token-do-not-log",
        BRIGHTDATA_BASE_URL="https://api.brightdata.test",
    )


def make_client(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> BrightDataClient:
    return BrightDataClient(settings=settings, transport=httpx.MockTransport(handler))
