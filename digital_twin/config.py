"""Abuse-protection limits, read once from the environment with safe defaults.

Every limit lives here so the guards, tools and app agree on one value. Invalid or
non-positive values fall back to the default instead of disabling a guard.
"""
import logging
import os

from dotenv import load_dotenv

# Load before reading any variable, so import order elsewhere doesn't matter
load_dotenv(override=True)

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("config_invalid_int", extra={"var": name})
        return default
    if value < minimum:
        logger.warning("config_below_minimum", extra={"var": name, "minimum": minimum})
        return default
    return value


# Input and output caps
MAX_MESSAGE_LENGTH = _int_env("GUARD_MAX_MESSAGE_LENGTH", 500)
MAX_HISTORY_TURNS = _int_env("GUARD_MAX_HISTORY_TURNS", 20)
MAX_TOKENS_REPLY = _int_env("GUARD_MAX_TOKENS_REPLY", 1024)
MAX_FIELD_LENGTH = _int_env("GUARD_MAX_FIELD_LENGTH", 2000)
MAX_TOOL_ROUNDS = _int_env("MAX_TOOL_ROUNDS", 5)

# Per-client rate limiting
RATE_LIMIT_WINDOW_SECONDS = _int_env("GUARD_RATE_LIMIT_WINDOW", 60)
RATE_LIMIT_REQUESTS_PER_WINDOW = _int_env("GUARD_RATE_LIMIT_REQUESTS", 30)

# Number of trusted reverse proxies in front of the app (Render's load balancer).
# The client IP is the X-Forwarded-For entry this many hops from the right.
# 0 means no trusted proxy: ignore X-Forwarded-For and use the socket address.
TRUSTED_PROXY_COUNT = _int_env("TRUSTED_PROXY_COUNT", 1, minimum=0)

# Global spend protection
DAILY_BUDGET_REQUESTS = _int_env("GUARD_DAILY_BUDGET", 1000)
CIRCUIT_BREAKER_FAILURE_THRESHOLD = _int_env("GUARD_CIRCUIT_FAILURE_THRESHOLD", 5)
CIRCUIT_BREAKER_OPEN_DURATION = _int_env("GUARD_CIRCUIT_OPEN_TIME", 300)

# Write-tool limits per session
PER_SESSION_WRITE_LIMIT = _int_env("GUARD_PER_SESSION_WRITE_LIMIT", 5)
RECORD_USER_DETAILS_LIMIT = _int_env("GUARD_RECORD_USER_DETAILS_LIMIT", 3)
RECORD_QUESTION_LIMIT = _int_env("GUARD_RECORD_QUESTION_LIMIT", 5)
REPEATED_PATTERN_THRESHOLD = _int_env("GUARD_REPEATED_PATTERN_THRESHOLD", 3)

# Storage backend for limiters and counters. Only the in-process backend exists;
# any other value is rejected so a typo can't silently disable limiting.
LIMITER_BACKEND = os.getenv("LIMITER_BACKEND", "memory").strip().lower()
if LIMITER_BACKEND != "memory":
    raise ValueError(f"Unsupported LIMITER_BACKEND {LIMITER_BACKEND!r}; only 'memory' is available")
