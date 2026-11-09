import importlib

import pytest

from digital_twin import config
from digital_twin.config import _int_env


def test_int_env_default_when_unset(monkeypatch):
    monkeypatch.delenv("TEST_LIMIT", raising=False)
    assert _int_env("TEST_LIMIT", 7) == 7


def test_int_env_reads_valid_value(monkeypatch):
    monkeypatch.setenv("TEST_LIMIT", "42")
    assert _int_env("TEST_LIMIT", 7) == 42


@pytest.mark.parametrize("raw", ["abc", "", "  ", "1.5"])
def test_int_env_falls_back_on_invalid(monkeypatch, raw):
    monkeypatch.setenv("TEST_LIMIT", raw)
    assert _int_env("TEST_LIMIT", 7) == 7


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_int_env_falls_back_below_minimum(monkeypatch, raw):
    # A zero or negative limit must not silently disable a guard
    monkeypatch.setenv("TEST_LIMIT", raw)
    assert _int_env("TEST_LIMIT", 7) == 7


def test_int_env_allows_zero_when_minimum_is_zero(monkeypatch):
    monkeypatch.setenv("TEST_LIMIT", "0")
    assert _int_env("TEST_LIMIT", 1, minimum=0) == 0


def test_unknown_limiter_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("LIMITER_BACKEND", "redis")
    # Stop the reload from re-reading .env over the patched value
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    try:
        with pytest.raises(ValueError, match="LIMITER_BACKEND"):
            importlib.reload(config)
    finally:
        monkeypatch.setenv("LIMITER_BACKEND", "memory")
        importlib.reload(config)
