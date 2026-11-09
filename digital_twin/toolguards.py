"""Guards for the write tools: input cleaning, write caps and dedupe.

Tool arguments come from the model, which a user can steer, so they're treated as
untrusted. Problems are raised as ToolInputError, whose message is safe to hand back
to the model. Nothing here logs argument values.
"""
import hashlib
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from digital_twin.config import (
    CLIENT_WRITE_LIMIT,
    CLIENT_WRITE_WINDOW_SECONDS,
    MAX_NAME_LENGTH,
    RATE_LIMIT_MAX_CLIENTS,
    RECORD_QUESTION_LIMIT,
    RECORD_USER_DETAILS_LIMIT,
    SESSION_WRITE_WINDOW_SECONDS,
)
from digital_twin.ratelimit import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)


class ToolInputError(ValueError):
    """Invalid tool input. The message goes back to the model, so it never echoes the value."""


# --- Text cleaning ---------------------------------------------------------------

# Control (Cc), format (Cf: zero-width, bidi overrides) and surrogate (Cs) characters
_STRIPPED_CATEGORIES = {"Cc", "Cf", "Cs"}


def clean_text(value, max_length: int) -> str:
    """Normalise free text for storage: NFKC, no invisible/control characters except
    newline and tab, collapsed whitespace, trimmed and capped at `max_length`."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolInputError("Expected text.")
    text = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    )
    text = re.sub(r"[^\S\n]+", " ", text)  # runs of spaces/tabs -> one space
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_length].strip()


def clean_name(value) -> str:
    """A person's name on one line, capped at MAX_NAME_LENGTH."""
    return " ".join(clean_text(value, MAX_NAME_LENGTH * 2).split())[:MAX_NAME_LENGTH].strip()


# --- Email ------------------------------------------------------------------------

_LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

# Exact disposable-mail domains (and their subdomains). Substring matching is avoided:
# it rejected real domains that merely contain words like "spam" or "junk".
DISPOSABLE_DOMAINS = frozenset({
    "10minutemail.com", "burnermail.io", "dispostable.com", "emailondeck.com",
    "fakeinbox.com", "getairmail.com", "getnada.com", "grr.la", "guerrillamail.com",
    "guerrillamail.net", "guerrillamail.org", "mailinator.com", "maildrop.cc",
    "mailnesia.com", "mintemail.com", "mohmal.com", "sharklasers.com", "spamgourmet.com",
    "temp-mail.org", "tempmail.com", "tempmailo.com", "throwaway.email",
    "throwawaymail.com", "trashmail.com", "yopmail.com",
})

_INVALID_EMAIL = "That isn't a valid email address. Ask the user to check it; don't guess or fix it yourself."


def is_disposable_domain(domain: str) -> bool:
    labels = domain.lower().split(".")
    return any(".".join(labels[i:]) in DISPOSABLE_DOMAINS for i in range(len(labels) - 1))


def normalise_email(value) -> str:
    """Validate an email address and return it with a lower-cased (ASCII) domain."""
    if not isinstance(value, str):
        raise ToolInputError(_INVALID_EMAIL)
    address = value.strip()
    if len(address) > 254 or address.count("@") != 1:
        raise ToolInputError(_INVALID_EMAIL)

    local, domain = address.split("@")
    if len(local) > 64 or not _LOCAL_PART.fullmatch(local):
        raise ToolInputError(_INVALID_EMAIL)
    try:
        # Internationalised domains become their ASCII (punycode) form
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ToolInputError(_INVALID_EMAIL) from None

    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ToolInputError(_INVALID_EMAIL)
    if is_disposable_domain(domain):
        logger.info("guard_tool_disposable_email")
        raise ToolInputError(
            "That looks like a disposable email address. Ask the user for one they check regularly."
        )
    return f"{local}@{domain}"


# --- Write caps and dedupe --------------------------------------------------------------

@dataclass(frozen=True)
class ToolCaller:
    """Who a tool call is made on behalf of; set by the server, never by the model."""
    session_id: str
    client_id: str


class ToolWriteGuard:
    """Caps writes per session (per tool) and per client (all tools), and drops repeats.

    Counts live in sliding-window limiters, so they're bounded in size and forgotten
    after their window. Dedupe keys are stored as hashes, never the values themselves.
    """

    def __init__(
        self,
        session_limits: dict[str, int],
        client_limit: int,
        client_window_seconds: float,
        session_window_seconds: float = SESSION_WRITE_WINDOW_SECONDS,
        max_keys: int = RATE_LIMIT_MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._session = {
            tool: SlidingWindowRateLimiter(limit, session_window_seconds, max_keys, clock)
            for tool, limit in session_limits.items()
        }
        self._client = SlidingWindowRateLimiter(client_limit, client_window_seconds, max_keys, clock)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_keys = max_keys
        self._lock = threading.Lock()

    @staticmethod
    def _fingerprint(caller: ToolCaller, tool: str, dedupe_key: str) -> str:
        raw = f"{caller.session_id}\0{tool}\0{dedupe_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def is_duplicate(self, caller: ToolCaller, tool: str, dedupe_key: str) -> bool:
        with self._lock:
            return self._fingerprint(caller, tool, dedupe_key) in self._seen

    def allow(self, caller: ToolCaller, tool: str) -> bool:
        """Count one write; False if a session or client cap is reached."""
        session_limiter = self._session.get(tool)
        if session_limiter is None:
            logger.warning("guard_tool_unknown_write_tool")
            return False
        if not session_limiter.allow(caller.session_id):
            logger.warning("guard_tool_session_limit", extra={"tool": tool})
            return False
        if not self._client.allow(caller.client_id):
            logger.warning("guard_tool_client_limit", extra={"tool": tool})
            return False
        return True

    def mark_written(self, caller: ToolCaller, tool: str, dedupe_key: str):
        with self._lock:
            self._seen[self._fingerprint(caller, tool, dedupe_key)] = None
            while len(self._seen) > self._max_keys:
                self._seen.popitem(last=False)


write_guard = ToolWriteGuard(
    session_limits={
        "record_user_details": RECORD_USER_DETAILS_LIMIT,
        "record_unknown_question": RECORD_QUESTION_LIMIT,
    },
    client_limit=CLIENT_WRITE_LIMIT,
    client_window_seconds=CLIENT_WRITE_WINDOW_SECONDS,
)
