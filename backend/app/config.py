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

    # How long ONE research search may occupy the process before it is
    # recorded TIMED_OUT and the enrichment continues without it.
    #
    # Configurable because the arXiv collector's speed is not stable, and
    # the right trade differs by situation. Measured on 2026-08-19: the
    # same collector returned 15 records in 101-118s in the morning and
    # took 497-576s for identical work in the afternoon. A cap that suits
    # the fast regime turns the slow regime into a guaranteed failure, and
    # a cap that suits the slow regime makes a live demo wait ten minutes.
    #
    # 300s is the default compromise: it captures every fast and moderate
    # run observed, and bounds the pathological case at five minutes.
    # Raise it (e.g. 600) when correctness matters more than latency, such
    # as a backfill; lower it when a bounded failure is preferable to a
    # long wait.
    RESEARCH_QUERY_TIMEOUT_SECONDS: float = 300.0
    # Ceiling for the acquisition phase as a whole. Because the searches
    # run concurrently this is close to the slowest query, not their sum;
    # the margin only covers stagger in trigger time.
    RESEARCH_ACQUISITION_BUDGET_SECONDS: float = 330.0

    # The production market collector the daily refresh drives.
    #
    # Named by id rather than discovered by scanning active collectors:
    # the daily refresh is about ONE dataset (Fix My Itch), and a job that
    # picked up whatever happened to be active could start scraping a
    # collector someone added for an unrelated experiment. The generic
    # every-collector job (app.jobs.daily_pipeline) still exists for the
    # cases where that IS the intent.
    #
    # Defaulted to production so the cron service needs one fewer variable,
    # and overridable so staging can point somewhere else.
    MARKET_COLLECTOR_ID: str = "48cbf27f-8b29-4106-ba39-b812a0002694"
    # What the collector named above MUST look like. Checked, never
    # written: a daily job that repaired its own configuration would turn
    # a misconfiguration into silent scraping of the wrong source.
    MARKET_COLLECTOR_PROVIDER: str = "brightdata"
    MARKET_COLLECTOR_EXTERNAL_ID: str = "c_mswvtpby29tybc04dr"

    # How long the daily refresh keeps driving one execution before
    # leaving it resumable, and how often it steps. Defaults come from the
    # pipeline executor rather than being restated, so the scheduled path
    # and the API path wait the same way unless deliberately told not to.
    DAILY_REFRESH_TIMEOUT_SECONDS: float | None = None
    DAILY_REFRESH_INTERVAL_SECONDS: float | None = None

    @property
    def cors_origins_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
