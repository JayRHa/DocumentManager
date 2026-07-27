from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.middleware.rate_limit_middleware import RateLimitMiddleware


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def test_env_example_only_documents_supported_settings():
    env_keys = {
        line.split("=", 1)[0]
        for line in (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    supported_keys = {name.upper() for name in Settings.model_fields}

    assert env_keys <= supported_keys
    assert {
        "SECRET_KEY",
        "DATABASE_URL",
        "AI_PROVIDER",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "TRUSTED_PROXY_IPS",
    } <= env_keys


def test_setup_uses_env_file_without_evaluating_its_contents():
    setup_script = (REPOSITORY_ROOT / "setup.sh").read_text(encoding="utf-8")

    assert "--env-file .env" in setup_script
    assert "export $(" not in setup_script


def test_container_runtime_does_not_start_a_duplicate_chroma_server():
    production_dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    development_dockerfile = (REPOSITORY_ROOT / "Dockerfile.dev").read_text(encoding="utf-8")
    entrypoint = (REPOSITORY_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "supervisor" not in production_dockerfile
    assert "supervisor" not in development_dockerfile
    assert "chromadb.cli" not in entrypoint
    assert "python -m uvicorn" in entrypoint


def test_runtime_dependencies_do_not_pull_unused_local_embedding_stack():
    requirements = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "sentence-transformers" not in requirements


def test_docker_context_excludes_credentials_and_runtime_data():
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in dockerignore
    assert "data" in dockerignore
    assert "backups" in dockerignore
