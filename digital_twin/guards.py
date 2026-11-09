import re
import time
import logging
from datetime import datetime
from collections import defaultdict
from enum import Enum

from digital_twin.config import (
    MAX_MESSAGE_LENGTH,
    MAX_HISTORY_TURNS,
    MAX_HISTORY_CHARS,
    MAX_TOKENS_REPLY,
    MAX_FIELD_LENGTH,
    RATE_LIMIT_WINDOW_SECONDS,
    RATE_LIMIT_REQUESTS_PER_WINDOW,
    DAILY_BUDGET_REQUESTS,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_OPEN_DURATION,
    PER_SESSION_WRITE_LIMIT,
    RECORD_USER_DETAILS_LIMIT,
    RECORD_QUESTION_LIMIT,
    REPEATED_PATTERN_THRESHOLD,
)

logger = logging.getLogger(__name__)


class GuardError(Exception):
    def __init__(self, message, user_friendly_message=None):
        super().__init__(message)
        self.user_friendly_message = user_friendly_message or "Sorry, I couldn't process your request. Please try again."


def validate_message(message: str):
    """Reject a new user message that shouldn't reach the model. Raises GuardError."""
    if not isinstance(message, str):
        logger.warning("guard_invalid_message_type")
        raise GuardError("Message must be a string", "Sorry, your message format wasn't recognized.")

    if not message.strip():
        logger.warning("guard_empty_message")
        raise GuardError("Message is empty", "Please type a message first.")

    if len(message) > MAX_MESSAGE_LENGTH:
        logger.warning("guard_max_message_length_exceeded", extra={"limit": MAX_MESSAGE_LENGTH})
        raise GuardError(
            f"Message exceeds maximum allowed length of {MAX_MESSAGE_LENGTH}",
            f"Your message is too long. Please keep messages under {MAX_MESSAGE_LENGTH} characters."
        )


_HISTORY_ROLES = {"user", "assistant"}


def _history_text(content) -> str:
    """Text of one history entry; Gradio 6 gives a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Keep only text parts (drops files and components)
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def prepare_history(history) -> list[dict]:
    """Turn client-supplied chat history into model messages that are safe to send.

    History arrives from the client, so it's untrusted: only user/assistant roles are
    kept (no injected system or tool messages), malformed entries are skipped, and the
    result is truncated to the most recent MAX_HISTORY_TURNS turns and MAX_HISTORY_CHARS
    characters. Older context is dropped rather than rejecting the request.
    """
    if not isinstance(history, list):
        return []

    cleaned = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") not in _HISTORY_ROLES:
            continue
        text = _history_text(msg.get("content"))
        if text:
            cleaned.append({"role": msg["role"], "content": text})

    # Walk back from the newest message until either cap is reached
    kept = []
    total_chars = 0
    for msg in reversed(cleaned[-(MAX_HISTORY_TURNS * 2):]):
        total_chars += len(msg["content"])
        if total_chars > MAX_HISTORY_CHARS:
            break
        kept.append(msg)
    kept.reverse()

    if len(kept) < len(cleaned):
        logger.info("guard_history_truncated", extra={"kept": len(kept), "dropped": len(cleaned) - len(kept)})
    return kept


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
circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,recovery_timeout=CIRCUIT_BREAKER_OPEN_DURATION)


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


class InputValidator:
    """Validate and sanitize input data before storage."""
    
    # Email validation pattern (simplified RFC 5322 compliant)
    EMAIL_PATTERN = re.compile(
        r'^(?:[a-zA-Z0-9!#$%&\'*+/=^_`{|}~-]+(?:\.[a-zA-Z0-9!#$%&\'*+/=^_`{|}~-]+)*|'
        r'"(?:[\x01-\x08\x0b\x0c\x0e-\x1f\x21\x23-\x5b\x5d-\x7f]|\\[\x01-\x09\x0b\x0c\x0e-\x7f])*")'
        r'@(?:(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?|'
        r'\[(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\])$'
    )
    
    # Characters to strip (control characters except newline, tab, carriage return)
    CONTROL_CHARS = ''.join(chr(i) for i in range(0x00, 0x08)) + \
                    ''.join(chr(i) for i in range(0x0B, 0x0C)) + \
                    ''.join(chr(i) for i in range(0x0E, 0x20)) + \
                    '\x7F'  # DEL character
    
    @classmethod
    def validate_email(cls, email: str) -> tuple[bool, str | None]:
        """Validate email format and basic domain checks."""
        if not isinstance(email, str):
            logger.warning("guard_invalid_email", extra={"error": "not_string"})
            return False, "Email must be a string."
        
        if len(email) > 254:
            logger.warning("guard_invalid_email", extra={"error": "too_long"})
            return False, "Email must be no more than 254 characters."
        
        email = email.strip()
        if not cls.EMAIL_PATTERN.fullmatch(email):
            logger.warning("guard_email_format_mismatch")
            return False, "Invalid email format."
        
        # Check for obvious spam patterns
        local_part = email.split('@')[0].lower()
        domain = email.split('@')[1].lower()
        
        # Check for all-numeric local part (common in spam)
        if local_part.isdigit() and len(local_part) > 5:
            logger.warning("guard_invalid_email_spam_pattern", extra={"pattern": "all_numeric_local"})
            return False, "Please provide a valid email address."
        
        # Check for suspicious domains
        if cls._is_suspicious_domain(domain):
            logger.warning("guard_invalid_email_suspicious_domain", extra={"domain": domain})
            return False, "Please provide a valid email address."
        
        return True, None
    
    @classmethod
    def _is_suspicious_domain(cls, domain: str) -> bool:
        """Check for obviously fake/spam domains."""
        # List of suspicious TLDs or patterns
        suspicious_patterns = [
            'tempmail', 'throwaway', 'guerrilla', 'mailinator', 'trashmail',
            'fakeinbox', 'spam', 'junk', 'disposable', 'getairmail',
            'mailnesia', 'mohmal', 'yopmail', 'dispostable', 'spamgourmet'
        ]
        domain_lower = domain.lower()
        for pattern in suspicious_patterns:
            if pattern in domain_lower:
                return True
        return False
    
    @classmethod
    def strip_control_characters(cls, text: str) -> str:
        """Remove control characters and excessive whitespace."""
        if not isinstance(text, str):
            logger.warning("guard_invalid_input_type")
            raise GuardError("Input must be a string", "Your input format wasn't recognized.")
        
        # Remove control characters except newline, tab, carriage return
        cleaned = ''.join(c for c in text if c not in cls.CONTROL_CHARS)
        
        # Strip multiple newlines to a single one
        cleaned = re.sub(r'\n{2,}', '\n', cleaned)
        # Strip multiple tabs to a single one
        cleaned = re.sub(r'\t{2,}', '\t', cleaned)
        # Strip multiple carriage returns to a single one
        cleaned = re.sub(r'\r{2,}', '\r', cleaned)
        
        return cleaned
    
    @classmethod
    def validate_field_length(cls, field: str, max_length: int | None = None) -> tuple[bool, str | None]:
        """Ensure fields don't exceed maximum allowed length."""
        max_len = max_length or MAX_FIELD_LENGTH
        if len(field) > max_len:
            logger.warning("guard_field_length_exceeded", extra={"length": len(field), "max": max_len})
            return False, f"Field must be no more than {max_len} characters."
        return True, None
    
    @classmethod
    def deduplicate_input(cls, text: str) -> str:
        """Remove repeated sequences (e.g., spam with 'a\\nb\\na\\nb' pattern)."""
        if len(text) < 6:
            return text
        
        # Check for consecutive repeated patterns (e.g., "spam spam spam" -> "spam")
        # Look for patterns up to 64 chars that repeat consecutively
        max_pattern_len = min(len(text), 64)
        
        for pattern_len in range(1, max_pattern_len + 1):
            # Check if the text consists of consecutive repetitions of a pattern
            pattern = text[:pattern_len]
            if len(pattern) < 1:
                continue
            
            # Count consecutive repetitions at the start
            repetitions = 1
            pos = pattern_len
            while pos + pattern_len <= len(text) and text[pos:pos + pattern_len] == pattern:
                repetitions += 1
                pos += pattern_len
            
            # If we found enough consecutive repetitions, collapse them
            if repetitions > REPEATED_PATTERN_THRESHOLD:
                logger.warning("guard_repeated_pattern", extra={"pattern": pattern[:20], "count": repetitions})
                # Replace the consecutive repetitions with a single instance
                # But only if the entire text is just this repetition (or mostly)
                # For safety, only collapse if repetitions cover most of the text
                if pos >= len(text) * 0.8:  # Pattern covers 80%+ of text
                    return pattern
        
        # Also check for repeated substring patterns (non-consecutive but frequent)
        # Simple check: if a short substring appears too many times
        if len(text) >= 20:
            for check_len in [2, 3, 4, 5, 8, 16, 32]:
                if check_len > len(text) // 2:
                    continue
                for start in range(0, len(text) - check_len + 1, check_len):
                    pattern = text[start:start + check_len]
                    count = text.count(pattern)
                    if count > REPEATED_PATTERN_THRESHOLD and count * check_len >= len(text) * 0.5:
                        logger.warning("guard_repeated_pattern", extra={"pattern": pattern[:20], "count": count})
                        # This is likely spam/gibberish
                        return pattern
        
        return text


input_validator = InputValidator()

