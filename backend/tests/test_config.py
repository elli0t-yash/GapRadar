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


def test_mcp_defaults_are_local_only_and_disabled_without_a_secret() -> None:
    settings = Settings(_env_file=None)

    assert settings.mcp_api_key_is_configured is False
    assert settings.mcp_allowed_hosts_list == [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    assert settings.mcp_allowed_origins_list == [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]


def test_mcp_remote_security_settings_are_explicitly_parsed() -> None:
    settings = Settings(
        _env_file=None,
        GAPRADAR_MCP_API_KEY="x" * 32,
        GAPRADAR_MCP_ALLOWED_HOSTS="api.example.com, internal.example.com:8443",
        GAPRADAR_MCP_ALLOWED_ORIGINS="https://inspector.example.com",
    )

    assert settings.mcp_api_key_is_configured is True
    assert settings.mcp_allowed_hosts_list == [
        "api.example.com",
        "internal.example.com:8443",
    ]
    assert settings.mcp_allowed_origins_list == [
        "https://inspector.example.com"
    ]
