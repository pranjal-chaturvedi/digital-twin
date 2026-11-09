import logging

import pytest

from digital_twin import toolguards
from digital_twin.toolguards import (
    ToolCaller,
    ToolInputError,
    ToolWriteGuard,
    clean_name,
    clean_text,
    is_disposable_domain,
    normalise_email,
)

ALICE = ToolCaller(session_id="s1", client_id="203.0.113.7")


class Ticks:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# --- clean_text / clean_name --------------------------------------------------------

@pytest.mark.parametrize("char", [chr(i) for i in range(0x00, 0x20) if chr(i) not in "\n\t\r"] + ["\x7f"])
def test_strips_every_ascii_control_character(char):
    # The old list skipped \x08 and \x0c (off-by-one ranges)
    assert clean_text(f"a{char}b", 100) == "ab"


@pytest.mark.parametrize("char", ["​", "‍", "‮", "⁦", "﻿"])
def test_strips_invisible_format_characters(char):
    # Zero-width and bidi-override characters can hide or reorder stored text
    assert clean_text(f"pay{char}pal", 100) == "paypal"


def test_keeps_newlines_and_tabs_but_collapses_runs():
    assert clean_text("Line1\n\n\n\nLine2\t\t\tLine3\r\nLine4", 100) == "Line1\n\nLine2 Line3\nLine4"


def test_trims_and_caps_length():
    assert clean_text("   " + "x" * 50 + "   ", 10) == "x" * 10


def test_normalises_unicode_compatibility_forms():
    assert clean_text("ｆｕｌｌｗｉｄｔｈ", 100) == "fullwidth"


def test_keeps_ordinary_unicode():
    assert clean_text("José — 東京 ✓", 100) == "José — 東京 ✓"


def test_none_is_empty_and_non_text_is_rejected():
    assert clean_text(None, 10) == ""
    with pytest.raises(ToolInputError):
        clean_text(12345, 10)


def test_name_is_one_line_and_capped(monkeypatch):
    monkeypatch.setattr(toolguards, "MAX_NAME_LENGTH", 10)
    assert clean_name("  Ada\n\nLovelace  ") == "Ada Lovela"


# --- normalise_email -------------------------------------------------------------------

@pytest.mark.parametrize("email, expected", [
    ("user@example.com", "user@example.com"),
    ("user.name@example.com", "user.name@example.com"),
    ("user+tag@example.co.uk", "user+tag@example.co.uk"),
    ("user-name@example-domain.com", "user-name@example-domain.com"),
    ("  User@EXAMPLE.com ", "User@example.com"),
    ("12345678@qq.com", "12345678@qq.com"),  # numeric local parts are real addresses
    ("someone@spamhaus.org", "someone@spamhaus.org"),  # contains "spam", but isn't disposable
    ("someone@junkyard.dog", "someone@junkyard.dog"),
    ("user@bücher.de", "user@xn--bcher-kva.de"),
])
def test_valid_emails(email, expected):
    assert normalise_email(email) == expected


@pytest.mark.parametrize("email", [
    "notanemail",
    "@example.com",
    "user@",
    "user@@example.com",
    "a@b@example.com",
    "user@.com",
    "user@example.",
    "user@example..com",
    "user @example.com",
    ".user@example.com",
    "user.@example.com",
    "user@-example.com",
    "user@example.123",
    "user@localhost",
    "",
    "a" * 65 + "@example.com",
    "a" * 255 + "@example.com",
    "user@example.com\nBcc: victim@example.com",
    "Name <user@example.com>",
    None,
    123,
])
def test_invalid_emails(email):
    with pytest.raises(ToolInputError):
        normalise_email(email)


@pytest.mark.parametrize("email", [
    "user@mailinator.com",
    "user@yopmail.com",
    "user@guerrillamail.com",
    "user@fakeinbox.com",
    "user@throwaway.email",
    "user@sub.mailinator.com",
    "user@MAILINATOR.COM",
])
def test_disposable_emails_rejected(email):
    with pytest.raises(ToolInputError, match="disposable"):
        normalise_email(email)


def test_disposable_check_is_exact_not_substring():
    assert is_disposable_domain("mailinator.com")
    assert is_disposable_domain("eu.mailinator.com")
    assert not is_disposable_domain("notmailinator.com")
    assert not is_disposable_domain("mailinator.com.example.org")


def test_error_message_never_echoes_the_value():
    with pytest.raises(ToolInputError) as exc:
        normalise_email("secret-person@@example.com")
    assert "secret-person" not in str(exc.value)


# --- ToolWriteGuard ----------------------------------------------------------------------

def _guard(session_limit=2, client_limit=10, clock=None, max_keys=1000):
    return ToolWriteGuard(
        session_limits={"record_user_details": session_limit},
        client_limit=client_limit,
        client_window_seconds=3600,
        session_window_seconds=86400,
        max_keys=max_keys,
        clock=clock or Ticks(),
    )


def test_session_cap():
    guard = _guard(session_limit=2)
    assert guard.allow(ALICE, "record_user_details")
    assert guard.allow(ALICE, "record_user_details")
    assert not guard.allow(ALICE, "record_user_details")


def test_new_session_does_not_escape_client_cap():
    guard = _guard(session_limit=2, client_limit=3)
    allowed = [
        guard.allow(ToolCaller(session_id=f"s{i}", client_id="203.0.113.7"), "record_user_details")
        for i in range(10)
    ]
    assert allowed.count(True) == 3


def test_clients_are_isolated():
    guard = _guard(session_limit=1)
    assert guard.allow(ALICE, "record_user_details")
    assert guard.allow(ToolCaller("s2", "198.51.100.9"), "record_user_details")


def test_unknown_tool_is_refused():
    assert not _guard().allow(ALICE, "drop_tables")


def test_client_cap_resets_after_window():
    clock = Ticks()
    guard = _guard(session_limit=100, client_limit=1, clock=clock)
    assert guard.allow(ALICE, "record_user_details")
    assert not guard.allow(ALICE, "record_user_details")
    clock.now += 3600
    assert guard.allow(ALICE, "record_user_details")


def test_dedupe_is_per_session_and_tool():
    guard = _guard()
    guard.mark_written(ALICE, "record_user_details", "a@example.com")
    assert guard.is_duplicate(ALICE, "record_user_details", "a@example.com")
    assert not guard.is_duplicate(ALICE, "record_user_details", "b@example.com")
    assert not guard.is_duplicate(ToolCaller("s2", ALICE.client_id), "record_user_details", "a@example.com")
    assert not guard.is_duplicate(ALICE, "record_unknown_question", "a@example.com")


def test_dedupe_store_is_bounded_and_hashed():
    guard = _guard(max_keys=3)
    for i in range(10):
        guard.mark_written(ALICE, "record_user_details", f"user{i}@example.com")
    assert len(guard._seen) == 3
    assert all("example.com" not in key for key in guard._seen)
    assert guard.is_duplicate(ALICE, "record_user_details", "user9@example.com")
    assert not guard.is_duplicate(ALICE, "record_user_details", "user0@example.com")


def test_limit_logs_hold_no_personal_data(caplog):
    guard = _guard(session_limit=1)
    caller = ToolCaller(session_id="secret-session", client_id="203.0.113.7")
    with caplog.at_level(logging.INFO):
        guard.allow(caller, "record_user_details")
        guard.allow(caller, "record_user_details")
        with pytest.raises(ToolInputError):
            normalise_email("person@mailinator.com")
    assert "guard_tool_session_limit" in caplog.text
    for secret in ["secret-session", "203.0.113.7", "person", "mailinator"]:
        assert secret not in caplog.text
