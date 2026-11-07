import unittest
from digital_twin.guards import (
    validate_message,
    get_client_ip,
    SlidingWindowRateLimiter,
    DailyBudgetGuard,
    CircuitBreaker,
    SessionToolLimiter,
    GuardError,
    MAX_MESSAGE_LENGTH,
    MAX_HISTORY_TURNS,
)


class TestGuards(unittest.TestCase):
    def test_validate_message_valid(self):
        # Should not raise exception
        validate_message("Hello world!", [])

    def test_validate_message_too_long(self):
        long_msg = "a" * (MAX_MESSAGE_LENGTH + 1)
        with self.assertRaises(GuardError) as cm:
            validate_message(long_msg, [])
        self.assertIn("too long", cm.exception.user_friendly_message.lower())

    def test_validate_message_invalid_type(self):
        with self.assertRaises(GuardError):
            validate_message(12345, [])  # type: ignore

    def test_validate_history_too_long(self):
        long_history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}] * (MAX_HISTORY_TURNS + 5)
        with self.assertRaises(GuardError) as cm:
            validate_message("short msg", long_history)
        self.assertIn("history", cm.exception.user_friendly_message.lower())

    def test_get_client_ip_forwarded(self):
        headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18"}
        self.assertEqual(get_client_ip(headers), "203.0.113.195")

    def test_get_client_ip_fallback(self):
        headers = {"RemoteAddr": "192.168.1.1"}
        self.assertEqual(get_client_ip(headers), "192.168.1.1")

    def test_get_client_ip_unknown(self):
        self.assertEqual(get_client_ip({}), "unknown")
        self.assertEqual(get_client_ip(None), "unknown")

    def test_rate_limiter(self):
        limiter = SlidingWindowRateLimiter(requests_per_window=2, window_seconds=10)
        
        allowed, msg = limiter.check_rate_limit("1.2.3.4")
        self.assertTrue(allowed)
        self.assertIsNone(msg)

        allowed, msg = limiter.check_rate_limit("1.2.3.4")
        self.assertTrue(allowed)
        self.assertIsNone(msg)

        allowed, msg = limiter.check_rate_limit("1.2.3.4")
        self.assertFalse(allowed)
        self.assertIn("Too many requests", msg)

    def test_daily_budget_guard(self):
        budget = DailyBudgetGuard(max_requests=2)

        allowed, msg = budget.check_budget()
        self.assertTrue(allowed)

        allowed, msg = budget.check_budget()
        self.assertTrue(allowed)

        allowed, msg = budget.check_budget()
        self.assertFalse(allowed)
        self.assertIn("budget", msg.lower())

    def test_circuit_breaker(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        self.assertTrue(cb.can_proceed())

        cb.record_failure()
        self.assertTrue(cb.can_proceed())

        cb.record_failure()
        self.assertFalse(cb.can_proceed())

        cb.record_success()
        self.assertTrue(cb.can_proceed())

    def test_per_session_write_limit(self):
        limiter = SessionToolLimiter(write_tool_limit=2, tool_limits={"record_user_details": 2})
        session = "session_1"

        allowed, msg = limiter.check_and_record_write_call(session, "record_user_details")
        self.assertTrue(allowed)
        self.assertIsNone(msg)

        allowed, msg = limiter.check_and_record_write_call(session, "record_user_details")
        self.assertTrue(allowed)
        self.assertIsNone(msg)

        allowed, msg = limiter.check_and_record_write_call(session, "record_user_details")
        self.assertFalse(allowed)
        self.assertIsNotNone(msg)
        self.assertIn("limit", msg.lower())

        status = limiter.get_session_status(session)
        self.assertEqual(status.get("record_user_details"), 2)


if __name__ == "__main__":
    unittest.main()
