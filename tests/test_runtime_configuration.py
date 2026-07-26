from types import SimpleNamespace

from app.config import Settings
from app.middleware.rate_limit_middleware import RateLimitMiddleware


def test_settings_read_documented_environment_variables(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/test.db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://documents.example.com")
    monkeypatch.setenv("ENVIRONMENT", "production")

    settings = Settings()

    assert settings.database_url == "sqlite:///./data/test.db"
    assert settings.openai_api_key == "test-key"
    assert settings.cors_origins_list == ["https://documents.example.com"]
    assert settings.production_mode is True


def test_forwarded_header_is_ignored_for_untrusted_client():
    middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
    middleware.trusted_proxy_ips = {"127.0.0.1"}
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"X-Forwarded-For": "198.51.100.7"},
    )

    assert middleware.get_client_ip(request) == "203.0.113.10"


def test_forwarded_header_is_used_for_trusted_proxy():
    middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
    middleware.trusted_proxy_ips = {"127.0.0.1"}
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"X-Forwarded-For": "198.51.100.7, 127.0.0.1"},
    )

    assert middleware.get_client_ip(request) == "198.51.100.7"
