import unittest
from digital_twin.guards import (
    validate_message,
    GuardError,
    MAX_MESSAGE_LENGTH,
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
