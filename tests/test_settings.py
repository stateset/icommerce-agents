"""``HostSettings`` reads every host knob once and refuses unsafe combinations."""

import pytest

from host.auth import AuthConfig
from host.settings import HostSettings


def test_defaults_are_the_local_demo(monkeypatch):
    for name in (
        "ICOMMERCE_ENVIRONMENT",
        "ICOMMERCE_STALE_APPLY_SECONDS",
        "ICOMMERCE_SESSION_TTL_SECONDS",
        "ICOMMERCE_CHAT_LEASE_SECONDS",
        "ICOMMERCE_RATE_LIMIT_PER_MINUTE",
        "ICOMMERCE_METRICS_TOKEN",
        "ICOMMERCE_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = HostSettings.from_env()
    assert settings == HostSettings()
    settings.validate(auth_config=AuthConfig())


def test_explicit_stale_apply_override_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ICOMMERCE_STALE_APPLY_SECONDS", "5")
    assert HostSettings.from_env().stale_apply_seconds == 5
    assert HostSettings.from_env(stale_apply_seconds=7).stale_apply_seconds == 7


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stale_apply_seconds", 0, "at least one second"),
        ("session_ttl_seconds", 30, "at least 60 seconds"),
        ("chat_lease_seconds", 10, "between 30 and 3600"),
        ("rate_limit_per_minute", -1, "cannot be negative"),
        ("metrics_token", "short", "at least 32 bytes"),
        ("environment", "staging", "must be development, test, or production"),
    ],
)
def test_invalid_values_are_refused(field, value, message):
    settings = HostSettings(**{field: value})
    with pytest.raises(ValueError, match=message):
        settings.validate(auth_config=AuthConfig())


def test_production_requires_jwt_metrics_rate_limit_and_https_origins():
    settings = HostSettings(environment="production", allowed_origins=("http://shop.example",))
    with pytest.raises(ValueError) as excinfo:
        settings.validate(auth_config=AuthConfig())
    text = str(excinfo.value)
    assert text.startswith("unsafe production configuration")
    for expected in (
        "ICOMMERCE_AUTH_MODE must be jwt",
        "ICOMMERCE_METRICS_TOKEN is required",
        "ICOMMERCE_RATE_LIMIT_PER_MINUTE must be enabled",
        "must be an HTTPS origin",
    ):
        assert expected in text
