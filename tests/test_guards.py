import unittest
from digital_twin.guards import (
    validate_message,
    SessionToolLimiter,
    GuardError,
    MAX_MESSAGE_LENGTH,
    InputValidator,
    MAX_FIELD_LENGTH,
    REPEATED_PATTERN_THRESHOLD,
)


class TestGuards(unittest.TestCase):
    def test_validate_message_valid(self):
        # Should not raise exception
        validate_message("Hello world!")

    def test_validate_message_too_long(self):
        long_msg = "a" * (MAX_MESSAGE_LENGTH + 1)
        with self.assertRaises(GuardError) as cm:
            validate_message(long_msg)
        self.assertIn("too long", cm.exception.user_friendly_message.lower())

    def test_validate_message_at_limit(self):
        validate_message("a" * MAX_MESSAGE_LENGTH)

    def test_validate_message_invalid_type(self):
        with self.assertRaises(GuardError):
            validate_message(12345)  # type: ignore

    def test_validate_message_empty(self):
        for msg in ["", "   ", "\n\t"]:
            with self.assertRaises(GuardError):
                validate_message(msg)

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

    # InputValidator tests
    def test_email_validation_valid(self):
        valid_emails = [
            "user@example.com",
            "user.name@example.com",
            "user+tag@example.co.uk",
            "user-name@example-domain.com",
            "user123@example.org",
            "\"quoted@local\"@example.com",
        ]
        for email in valid_emails:
            with self.subTest(email=email):
                is_valid, msg = InputValidator.validate_email(email)
                self.assertTrue(is_valid, f"Email '{email}' should be valid: {msg}")
                self.assertIsNone(msg)

    def test_email_validation_invalid_format(self):
        invalid_emails = [
            "notanemail",
            "@example.com",
            "user@",
            "user@@example.com",
            "user@.com",
            "user@example.",
            "user @example.com",  # space in local part
            "",
            "a" * 255 + "@example.com",  # too long
        ]
        for email in invalid_emails:
            with self.subTest(email=email):
                is_valid, msg = InputValidator.validate_email(email)
                self.assertFalse(is_valid, f"Email '{email}' should be invalid")
                self.assertIsNotNone(msg)

    def test_email_validation_suspicious_domain(self):
        suspicious_emails = [
            "user@tempmail.com",
            "user@mailinator.com",
            "user@throwaway.email",
            "user@guerrillamail.com",
            "user@yopmail.com",
            "user@fakeinbox.com",
        ]
        for email in suspicious_emails:
            with self.subTest(email=email):
                is_valid, msg = InputValidator.validate_email(email)
                self.assertFalse(is_valid, f"Email '{email}' should be rejected as suspicious")
                self.assertIsNotNone(msg)

    def test_email_validation_all_numeric_local(self):
        # All-numeric local parts longer than 5 chars are suspicious
        suspicious_emails = [
            "123456@example.com",
            "9876543210@example.com",
        ]
        for email in suspicious_emails:
            with self.subTest(email=email):
                is_valid, msg = InputValidator.validate_email(email)
                self.assertFalse(is_valid, f"Email '{email}' should be rejected as spam pattern")

    def test_strip_control_characters(self):
        # Test various control characters are stripped
        text_with_controls = "Hello\x00World\x01\x02\x03Test\x07"
        cleaned = InputValidator.strip_control_characters(text_with_controls)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x01", cleaned)
        self.assertNotIn("\x02", cleaned)
        self.assertNotIn("\x03", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertEqual(cleaned, "HelloWorldTest")

        # Newlines, tabs, carriage returns should be preserved (but not duplicated)
        text_with_newlines = "Line1\n\n\nLine2\t\t\tLine3\r\r\rLine4"
        cleaned = InputValidator.strip_control_characters(text_with_newlines)
        self.assertEqual(cleaned, "Line1\nLine2\tLine3\rLine4")

    def test_strip_control_characters_invalid_type(self):
        with self.assertRaises(GuardError):
            InputValidator.strip_control_characters(12345)  # type: ignore

    def test_validate_field_length_valid(self):
        field = "a" * MAX_FIELD_LENGTH
        is_valid, msg = InputValidator.validate_field_length(field)
        self.assertTrue(is_valid)
        self.assertIsNone(msg)

    def test_validate_field_length_exceeded(self):
        field = "a" * (MAX_FIELD_LENGTH + 1)
        is_valid, msg = InputValidator.validate_field_length(field)
        self.assertFalse(is_valid)
        self.assertIsNotNone(msg)
        self.assertIn(str(MAX_FIELD_LENGTH), msg)

    def test_validate_field_length_custom_max(self):
        is_valid, msg = InputValidator.validate_field_length("hello", max_length=3)
        self.assertFalse(is_valid)
        self.assertIn("3", msg)

    def test_deduplicate_input_no_repetition(self):
        text = "This is a normal sentence without repetition."
        result = InputValidator.deduplicate_input(text)
        self.assertEqual(result, text)

    def test_deduplicate_input_repeated_pattern(self):
        # Pattern repeated more than threshold
        pattern = "spam "
        text = pattern * (REPEATED_PATTERN_THRESHOLD + 2)  # 5 repetitions
        result = InputValidator.deduplicate_input(text)
        # Should be deduplicated to single instance
        self.assertEqual(result, pattern)

    def test_deduplicate_input_short_text(self):
        # Text shorter than 6 chars should not be processed
        text = "abc"
        result = InputValidator.deduplicate_input(text)
        self.assertEqual(result, text)

    def test_deduplicate_input_multiple_patterns(self):
        # Multiple different repeated patterns
        text = "ababababab" + "cdcdcdcdcd"
        result = InputValidator.deduplicate_input(text)
        # Should handle the most repeated pattern
        self.assertLess(len(result), len(text))


if __name__ == "__main__":
    unittest.main()
