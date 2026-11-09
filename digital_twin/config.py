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
# History comes from the client, so cap its total size too, not just the turn count
MAX_HISTORY_CHARS = _int_env("GUARD_MAX_HISTORY_CHARS", 12000)
MAX_TOKENS_REPLY = _int_env("GUARD_MAX_TOKENS_REPLY", 1024)
MAX_FIELD_LENGTH = _int_env("GUARD_MAX_FIELD_LENGTH", 2000)
MAX_TOOL_ROUNDS = _int_env("MAX_TOOL_ROUNDS", 5)

# Per-client rate limiting
RATE_LIMIT_WINDOW_SECONDS = _int_env("GUARD_RATE_LIMIT_WINDOW", 60)
RATE_LIMIT_REQUESTS_PER_WINDOW = _int_env("GUARD_RATE_LIMIT_REQUESTS", 30)
# Upper bound on clients tracked at once; least recently seen are evicted past it
RATE_LIMIT_MAX_CLIENTS = _int_env("GUARD_RATE_LIMIT_MAX_CLIENTS", 10000)

# Number of trusted proxies that append to X-Forwarded-For in front of the app.
# The client IP is the entry this many hops from the right; anything further left
# was sent by the client and can be forged. 0 = ignore the header, use the socket peer.
# Render: Cloudflare appends the client IP, then Render's proxy appends Cloudflare's,
# so the client is 2 from the right. Verify on a live deploy before relying on it.
TRUSTED_PROXY_COUNT = _int_env("TRUSTED_PROXY_COUNT", 2, minimum=0)

# Gradio queue: requests waiting beyond max size are refused, and at most
# CONCURRENCY_LIMIT chats run model calls at the same time
QUEUE_MAX_SIZE = _int_env("GUARD_QUEUE_MAX_SIZE", 20)
CONCURRENCY_LIMIT = _int_env("GUARD_CONCURRENCY_LIMIT", 4)

# Global spend protection
# Model calls per UTC day across all clients. One chat can make up to MAX_TOOL_ROUNDS + 1
# calls, so this counts calls, not chats. Each call's cost is dominated by the fixed
# system prompt, so calls are a fair proxy for spend.
DAILY_BUDGET_MODEL_CALLS = _int_env("GUARD_DAILY_BUDGET", 1000)
CIRCUIT_BREAKER_FAILURE_THRESHOLD = _int_env("GUARD_CIRCUIT_FAILURE_THRESHOLD", 5)
CIRCUIT_BREAKER_OPEN_DURATION = _int_env("GUARD_CIRCUIT_OPEN_TIME", 300)

# Write tools. Gradio's session_hash is chosen by the client, so per-session caps alone
# can be dodged by opening new sessions; the per-client cap (keyed like the rate limiter)
# bounds writes across all of one client's sessions.
RECORD_USER_DETAILS_LIMIT = _int_env("GUARD_RECORD_USER_DETAILS_LIMIT", 3)  # per session
RECORD_QUESTION_LIMIT = _int_env("GUARD_RECORD_QUESTION_LIMIT", 5)  # per session
SESSION_WRITE_WINDOW_SECONDS = 24 * 60 * 60  # how long a session's write count is remembered
CLIENT_WRITE_LIMIT = _int_env("GUARD_CLIENT_WRITE_LIMIT", 10)  # all write tools, per client
CLIENT_WRITE_WINDOW_SECONDS = _int_env("GUARD_CLIENT_WRITE_WINDOW", 60 * 60)
MAX_NAME_LENGTH = _int_env("GUARD_MAX_NAME_LENGTH", 100)

# Storage backend for limiters and counters. Only the in-process backend exists;
# any other value is rejected so a typo can't silently disable limiting.
LIMITER_BACKEND = os.getenv("LIMITER_BACKEND", "memory").strip().lower()
if LIMITER_BACKEND != "memory":
    raise ValueError(f"Unsupported LIMITER_BACKEND {LIMITER_BACKEND!r}; only 'memory' is available")
