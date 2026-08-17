from app.config import Settings


def test_default_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.APP_ENV == "development"
    assert settings.DATABASE_URL == ""
    assert settings.CORS_ORIGINS == "http://localhost:5173"


def test_cors_origins_list_parses_comma_separated_values() -> None:
    settings = Settings(_env_file=None, CORS_ORIGINS="http://a.com, http://b.com")

    assert settings.cors_origins_list == ["http://a.com", "http://b.com"]


def test_cors_origins_list_empty_string_yields_empty_list() -> None:
    settings = Settings(_env_file=None, CORS_ORIGINS="")

    assert settings.cors_origins_list == []
