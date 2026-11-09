"""Global spend protection: a daily model-call budget and a circuit breaker for the model API.

Both are per process and reset on restart. Each has an injectable clock for tests and a
lock, since Gradio runs chats on several worker threads.
"""
import logging
import threading
import time
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Protocol

from digital_twin.config import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_OPEN_DURATION,
    DAILY_BUDGET_MODEL_CALLS,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Budget(Protocol):
    def has_remaining(self) -> bool:
        """True if at least one unit is left today; spends nothing."""
        ...

    def try_spend(self, units: int = 1) -> bool:
        """Spend `units` if they fit in today's budget; return False (spending nothing) if not."""
        ...


class DailyBudget:
    """Counts model calls per UTC day. A request can make several calls, so each one is charged."""

    def __init__(self, limit: int, clock: Callable[[], datetime] = _utc_now):
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._day = self._today()
        self._used = 0
        self._trip_logged = False

    def _today(self) -> date:
        return self._clock().astimezone(timezone.utc).date()

    def _roll_over(self):
        # Caller holds the lock. Compare with != so a clock that moves backwards also resets
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = 0
            self._trip_logged = False
            logger.info("guard_daily_budget_reset")

    def _trip(self):
        # One warning per day, not one per rejected request
        if not self._trip_logged:
            self._trip_logged = True
            logger.warning("guard_daily_budget_exhausted")

    def has_remaining(self) -> bool:
        with self._lock:
            self._roll_over()
            if self._used < self.limit:
                return True
            self._trip()
            return False

    def try_spend(self, units: int = 1) -> bool:
        with self._lock:
            self._roll_over()
            if self._used + units > self.limit:
                self._trip()
                return False
            self._used += units
            return True

    @property
    def used(self) -> int:
        with self._lock:
            self._roll_over()
            return self._used


class CircuitBreaker:
    """Stops calling the model API after repeated failures, then tests recovery with one request.

    CLOSED: calls flow; `failure_threshold` consecutive failures open the circuit.
    OPEN: calls are refused for `recovery_timeout` seconds.
    HALF_OPEN: exactly one probe is let through. Success closes the circuit, failure reopens
    it. If the probe never reports back, another is allowed after `recovery_timeout`.
    """

    class State(Enum):
        CLOSED = "closed"
        OPEN = "open"
        HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int,
        recovery_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_started_at = 0.0

    @property
    def state(self) -> "CircuitBreaker.State":
        with self._lock:
            return self._state

    def can_proceed(self) -> bool:
        with self._lock:
            if self._state is self.State.CLOSED:
                return True
            now = self._clock()
            if self._state is self.State.OPEN:
                if now - self._opened_at < self.recovery_timeout:
                    return False
                self._state = self.State.HALF_OPEN
                self._probe_started_at = now
                logger.info("guard_circuit_breaker_half_open")
                return True
            # HALF_OPEN: a probe is already in flight
            if now - self._probe_started_at >= self.recovery_timeout:
                self._probe_started_at = now
                return True
            return False

    def record_success(self):
        with self._lock:
            if self._state is not self.State.CLOSED:
                logger.info("guard_circuit_breaker_closed")
            self._state = self.State.CLOSED
            self._failures = 0

    def record_failure(self):
        with self._lock:
            now = self._clock()
            if self._state is self.State.HALF_OPEN:
                self._state = self.State.OPEN
                self._opened_at = now
                logger.error("guard_circuit_breaker_reopened")
                return
            self._failures += 1
            if self._state is self.State.CLOSED and self._failures >= self.failure_threshold:
                self._state = self.State.OPEN
                self._opened_at = now
                logger.error("guard_circuit_breaker_opened")


daily_budget: Budget = DailyBudget(DAILY_BUDGET_MODEL_CALLS)
circuit_breaker = CircuitBreaker(CIRCUIT_BREAKER_FAILURE_THRESHOLD, CIRCUIT_BREAKER_OPEN_DURATION)
