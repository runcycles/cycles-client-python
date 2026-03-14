"""Tests for CyclesConfig."""


import pytest

from runcycles.config import CyclesConfig


class TestCyclesConfig:
    def test_create(self) -> None:
        c = CyclesConfig(base_url="http://localhost:7878", api_key="key-123")
        assert c.base_url == "http://localhost:7878"
        assert c.api_key == "key-123"
        assert c.connect_timeout == 2.0
        assert c.read_timeout == 5.0
        assert c.retry_enabled is True
        assert c.retry_max_attempts == 5

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CYCLES_BASE_URL", "http://cycles:7878")
        monkeypatch.setenv("CYCLES_API_KEY", "test-key")
        monkeypatch.setenv("CYCLES_TENANT", "acme")
        monkeypatch.setenv("CYCLES_CONNECT_TIMEOUT", "5.0")

        c = CyclesConfig.from_env()
        assert c.base_url == "http://cycles:7878"
        assert c.api_key == "test-key"
        assert c.tenant == "acme"
        assert c.connect_timeout == 5.0

    def test_from_env_missing_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CYCLES_BASE_URL", raising=False)
        monkeypatch.setenv("CYCLES_API_KEY", "key")
        with pytest.raises(ValueError, match="BASE_URL"):
            CyclesConfig.from_env()

    def test_from_env_missing_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CYCLES_BASE_URL", "http://localhost:7878")
        monkeypatch.delenv("CYCLES_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API_KEY"):
            CyclesConfig.from_env()
