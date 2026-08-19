from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    DATABASE_URL: str = ""
    CORS_ORIGINS: str = "http://localhost:5173"

    BRIGHTDATA_API_KEY: str = ""
    BRIGHTDATA_BASE_URL: str = "https://api.brightdata.com"
    HARNESS_API_KEY: str = ""

    # Semantic research matching. Empty key means the semantic matcher is
    # simply unavailable -- callers fall back to the development matcher
    # rather than the application failing to start.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5-mini"
    # minimal | low | medium | high. Medium is deliberate: judging one
    # abstract against one problem is a bounded call made once per
    # candidate, and every candidate costs money.
    OPENAI_REASONING_EFFORT: str = "medium"

    @property
    def cors_origins_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
