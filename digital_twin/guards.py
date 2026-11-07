import os
import time
import logging
from datetime import datetime
from collections import defaultdict
from enum import Enum

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = int(os.getenv("GUARD_MAX_MESSAGE_LENGTH", "500"))
MAX_HISTORY_TURNS = int(os.getenv("GUARD_MAX_HISTORY_TURNS", "20"))
MAX_TOKENS_REPLY = int(os.getenv("GUARD_MAX_TOKENS_REPLY", "1024"))

RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("GUARD_RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_REQUESTS_PER_WINDOW = int(os.getenv("GUARD_RATE_LIMIT_REQUESTS", "30"))

DAILY_BUDGET_REQUESTS = int(os.getenv("GUARD_DAILY_BUDGET", "1000"))
CIRCUIT_BREAKER_OPEN_DURATION = int(os.getenv("GUARD_CIRCUIT_OPEN_TIME", "300"))

PER_SESSION_WRITE_LIMIT = int(os.getenv("GUARD_PER_SESSION_WRITE_LIMIT", "5"))
RECORD_USER_DETAILS_LIMIT = int(os.getenv("GUARD_RECORD_USER_DETAILS_LIMIT", "3"))
RECORD_QUESTION_LIMIT = int(os.getenv("GUARD_RECORD_QUESTION_LIMIT", "5"))


class GuardError(Exception):
    def __init__(self, message, user_friendly_message=None):
        super().__init__(message)
        self.user_friendly_message = user_friendly_message or "Sorry, I couldn't process your request. Please try again."


def validate_message(message: str, history: list):
    if not isinstance(message, str):
        logger.warning("guard_invalid_message_type")
        raise GuardError("Message must be a string", "Sorry, your message format wasn't recognized.")
    
    if len(message) > MAX_MESSAGE_LENGTH:
        logger.warning("guard_max_message_length_exceeded", extra={"limit": MAX_MESSAGE_LENGTH})
        raise GuardError(
            f"Message exceeds maximum allowed length of {MAX_MESSAGE_LENGTH}",
            f"Your message is too long. Please keep messages under {MAX_MESSAGE_LENGTH} characters."
        )
    
    if history and len(history) > (MAX_HISTORY_TURNS * 2):
        logger.warning("guard_max_history_exceeded", extra={"turns": len(history)})
        raise GuardError(
            f"History exceeds maximum allowed turns of {MAX_HISTORY_TURNS}",
            "Our conversation history is getting long. Please start a new session."
        )


def get_client_ip(request_headers) -> str:
    if not request_headers:
        return "unknown"
    
    xff = None
    if hasattr(request_headers, "get"):
        xff = request_headers.get("X-Forwarded-For") or request_headers.get("x-forwarded-for")
    elif isinstance(request_headers, dict):
        xff = request_headers.get("X-Forwarded-For") or request_headers.get("x-forwarded-for")
    
    if xff:
        return xff.split(",")[0].strip()
    
    remote = None
    if hasattr(request_headers, "get"):
        remote = request_headers.get("RemoteAddr") or request_headers.get("remote_addr")
    elif isinstance(request_headers, dict):
        remote = request_headers.get("RemoteAddr") or request_headers.get("remote_addr")
        
    return remote or "unknown"


class SlidingWindowRateLimiter:
    def __init__(self, requests_per_window: int, window_seconds: int):
        self.requests = defaultdict(list)
        self.max_requests = requests_per_window
        self.window_seconds = window_seconds

    def check_rate_limit(self, client_ip: str) -> tuple[bool, str | None]:
        current_time = time.time()
        self.requests[client_ip] = [
            t for t in self.requests[client_ip] 
            if current_time - t < self.window_seconds
        ]
        
        if len(self.requests[client_ip]) >= self.max_requests:
            logger.warning("guard_rate_limit_triggered")
            return False, "Too many requests. Please wait a moment before trying again."
        
        self.requests[client_ip].append(current_time)
        return True, None


class DailyBudgetGuard:
    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self.request_count = 0
        self.reset_date = datetime.now().date()

    def check_budget(self) -> tuple[bool, str | None]:
        today = datetime.now().date()
        if today > self.reset_date:
            self.request_count = 0
            self.reset_date = today
            logger.info("guard_daily_budget_reset")

        if self.request_count >= self.max_requests:
            logger.warning("guard_daily_budget_exceeded")
            return False, "Daily request budget reached. Please check back tomorrow!"

        self.request_count += 1
        return True, None


class CircuitBreaker:
    class State(Enum):
        CLOSED = "closed"
        OPEN = "open"
        HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = self.State.CLOSED
        self.failure_count = 0
        self.last_failure_time = None

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = self.State.OPEN
            logger.error("guard_circuit_breaker_opened")

    def record_success(self):
        self.failure_count = 0
        self.state = self.State.CLOSED

    def can_proceed(self) -> bool:
        if self.state == self.State.CLOSED:
            return True
        if self.state == self.State.OPEN:
            if self.last_failure_time and (time.time() - self.last_failure_time) > self.recovery_timeout:
                self.state = self.State.HALF_OPEN
                logger.info("guard_circuit_breaker_half_open")
                return True
            return False
        return True


rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_REQUESTS_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS)
daily_budget = DailyBudgetGuard(DAILY_BUDGET_REQUESTS)
circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=CIRCUIT_BREAKER_OPEN_DURATION)


class SessionToolLimiter:
    def __init__(self, write_tool_limit: int = PER_SESSION_WRITE_LIMIT, tool_limits: dict[str, int] | None = None):
        self.default_limit = write_tool_limit
        self.tool_limits = tool_limits or {
            "record_user_details": RECORD_USER_DETAILS_LIMIT,
            "record_unknown_question": RECORD_QUESTION_LIMIT,
        }
        self.write_tool_calls = defaultdict(lambda: defaultdict(int))

    def check_and_record_write_call(self, session_id: str, tool_name: str, arguments: dict | None = None) -> tuple[bool, str | None]:
        """Check if write tool call is allowed for session_id and increment count if allowed."""
        if not tool_name:
            return True, None

        limit = self.tool_limits.get(tool_name, self.default_limit)
        current_count = self.write_tool_calls[session_id][tool_name]

        if current_count >= limit:
            logger.warning("guard_write_tool_limit_exceeded", extra={"session": session_id, "tool": tool_name})
            return False, f"You've reached your limit of recording interactions for tool '{tool_name}'."

        self.write_tool_calls[session_id][tool_name] += 1
        return True, None

    def get_session_status(self, session_id: str) -> dict[str, int]:
        """Returns counts for each write tool per session."""
        return dict(self.write_tool_calls[session_id])


session_tool_limiter = SessionToolLimiter()

