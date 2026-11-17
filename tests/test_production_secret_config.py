import pytest

from app import create_app
from config import Config, TestConfig


def test_valid_production_secret_is_accepted(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    Config.validate_runtime({"APP_ENV": "production", "SECRET_KEY": "N3vEr-Reu5e_Prod!Key_2026-Example-84#SAFE"})


@pytest.mark.parametrize("secret", [None, "", "dev-only-change-me", "short", "s" * 48, "ab" * 24])
def test_production_rejects_missing_or_weak_secret(monkeypatch, secret):
    monkeypatch.delenv("RENDER", raising=False)
    with pytest.raises(RuntimeError, match="Production requires SECRET_KEY"):
        Config.validate_runtime({"APP_ENV": "production", "SECRET_KEY": secret})


def test_render_environment_requires_explicit_secret(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="Production requires SECRET_KEY"):
        Config.validate_runtime({"SECRET_KEY": "dev-only-change-me"})


def test_development_and_test_configuration_remain_usable(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    Config.validate_runtime({"APP_ENV": "development", "SECRET_KEY": "dev-only-change-me"})
    assert len(TestConfig.SECRET_KEY) >= 32
    assert create_app(TestConfig).testing


def test_production_application_startup_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)

    class InvalidProductionConfig(TestConfig):
        APP_ENV = "production"
        SECRET_KEY = "dev-only-change-me"

    with pytest.raises(RuntimeError, match="Production requires SECRET_KEY"):
        create_app(InvalidProductionConfig)
