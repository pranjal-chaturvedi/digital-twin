"""Write-tool tests: storage is replaced with an in-memory list, so nothing reaches the Hub."""
import json
from types import SimpleNamespace

import pytest

import app
from digital_twin import tools
from digital_twin.ratelimit import SlidingWindowRateLimiter
from digital_twin.spend import CircuitBreaker, DailyBudget
from digital_twin.toolguards import ToolCaller, ToolWriteGuard

CALLER = ToolCaller(session_id="sess-1", client_id="203.0.113.7")


@pytest.fixture
def stored(monkeypatch):
    """Capture writes and give each test fresh caps (3 contacts / 5 questions per session)."""
    records = []
    monkeypatch.setattr(tools, "_append_record", lambda table, record: records.append((table, record)))
    monkeypatch.setattr(tools, "write_guard", ToolWriteGuard(
        session_limits={"record_user_details": 3, "record_unknown_question": 5},
        client_limit=10,
        client_window_seconds=3600,
    ))
    return records


def _call(name, arguments, call_id="call_1"):
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))


def _run(name, arguments, caller=CALLER):
    [result] = tools.handle_tool_calls([_call(name, arguments)], caller)
    return json.loads(result["content"])


# --- Single calls ----------------------------------------------------------------------

def test_valid_contact_is_cleaned_and_stored(stored):
    result = _run("record_user_details", {
        "email": " Ada@Example.COM ", "name": "Ada​\nLovelace", "notes": "Wants\x00 a chat",
    })
    assert result == "OK"
    assert stored == [("contacts", {"name": "Ada Lovelace", "email": "Ada@example.com", "notes": "Wants a chat"})]


def test_contact_defaults(stored):
    _run("record_user_details", {"email": "ada@example.com"})
    assert stored[0][1]["name"] == "Name not provided"
    assert stored[0][1]["notes"] == "not provided"


def test_invalid_email_returns_tool_error_and_stores_nothing(stored):
    result = _run("record_user_details", {"email": "not-an-email"})
    assert "error" in result
    assert stored == []


def test_question_is_cleaned_and_stored(stored):
    assert _run("record_unknown_question", {"question": "  What's your\x07 favourite stack?  "}) == "OK"
    assert stored == [("questions", {"question": "What's your favourite stack?"})]


def test_empty_question_is_rejected(stored):
    assert "error" in _run("record_unknown_question", {"question": " ​ \n "})
    assert stored == []


def test_question_is_capped(stored, monkeypatch):
    monkeypatch.setattr(tools, "MAX_FIELD_LENGTH", 20)
    _run("record_unknown_question", {"question": "q" * 500})
    assert len(stored[0][1]["question"]) == 20


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null", '{"email": "a@example.com", "evil": 1}', "{}"])
def test_malformed_arguments_return_errors(stored, raw):
    assert "error" in _run("record_user_details", raw)
    assert stored == []


def test_unknown_tool_name_is_not_echoed_or_logged(stored, capsys):
    result = _run("exfiltrate_<secret>", {})
    assert "error" in result
    assert "secret" not in json.dumps(result)
    assert "secret" not in capsys.readouterr().out


# --- Dedupe and caps -----------------------------------------------------------------------

def test_duplicate_contact_is_stored_once_and_costs_no_cap(stored):
    for _ in range(5):
        _run("record_user_details", {"email": "ada@example.com", "notes": "again"})
    assert len(stored) == 1
    # The cap of 3 is still fully available for different addresses
    for i in range(2):
        assert _run("record_user_details", {"email": f"other{i}@example.com"}) == "OK"


def test_duplicate_email_matches_case_insensitively(stored):
    _run("record_user_details", {"email": "ada@example.com"})
    assert _run("record_user_details", {"email": "ADA@EXAMPLE.COM"}) == tools.ALREADY_RECORDED
    assert len(stored) == 1


def test_duplicate_question_ignores_case_and_spacing(stored):
    _run("record_unknown_question", {"question": "Do you like Rust?"})
    assert _run("record_unknown_question", {"question": "  do you   LIKE rust? "}) == tools.ALREADY_RECORDED
    assert len(stored) == 1


def test_session_cap_returns_tool_error(stored):
    results = [_run("record_user_details", {"email": f"user{i}@example.com"}) for i in range(6)]
    assert results[:3] == ["OK"] * 3
    assert all(r == {"error": tools.LIMIT_REACHED_ERROR} for r in results[3:])
    assert len(stored) == 3


def test_invalid_input_does_not_use_up_the_cap(stored):
    for _ in range(10):
        _run("record_user_details", {"email": "bad"})
    assert _run("record_user_details", {"email": "ada@example.com"}) == "OK"


def test_failed_write_is_not_marked_as_recorded(stored, monkeypatch):
    def boom(table, record):
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_append_record", boom)
    assert "error" in _run("record_user_details", {"email": "ada@example.com"})
    monkeypatch.setattr(tools, "_append_record", lambda t, r: stored.append((t, r)))
    assert _run("record_user_details", {"email": "ada@example.com"}) == "OK"


def test_every_tool_call_gets_one_reply(stored):
    calls = [_call("record_user_details", {"email": f"u{i}@example.com"}, f"call_{i}") for i in range(5)]
    results = tools.handle_tool_calls(calls, CALLER)
    assert [r["tool_call_id"] for r in results] == [f"call_{i}" for i in range(5)]


# --- End to end: a model spamming the contact tool ---------------------------------------------

class SpammingClient:
    """Every reply asks to record 4 new contacts, until tools are switched off."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if kwargs.get("tool_choice") == "none":
            message = SimpleNamespace(content="done", tool_calls=None)
        else:
            batch = [
                _call("record_user_details", {"email": f"spam{self.calls}-{i}@example.com"}, f"c{self.calls}-{i}")
                for i in range(4)
            ]
            message = SimpleNamespace(
                content=None,
                tool_calls=batch,
                model_dump=lambda exclude_none=False: {"role": "assistant", "tool_calls": []},
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_spamming_model_is_stopped_by_the_cap(stored, monkeypatch):
    fake = SpammingClient()
    monkeypatch.setattr(app, "get_client", lambda: fake)
    monkeypatch.setattr(app, "rate_limiter", SlidingWindowRateLimiter(100, 60))
    monkeypatch.setattr(app, "daily_budget", DailyBudget(100))
    monkeypatch.setattr(app, "circuit_breaker", CircuitBreaker(5, 300))

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.7"), session_hash="spam-session")
    assert app.chat("record lots of contacts", [], request) == "done"

    # 5 tool rounds x 4 calls = 20 attempts, but only the session cap (3) is written
    assert len(stored) == 3


def test_rotating_sessions_is_stopped_by_the_client_cap(stored, monkeypatch):
    fake = SpammingClient()
    monkeypatch.setattr(app, "get_client", lambda: fake)
    monkeypatch.setattr(app, "rate_limiter", SlidingWindowRateLimiter(100, 60))
    monkeypatch.setattr(app, "daily_budget", DailyBudget(1000))
    monkeypatch.setattr(app, "circuit_breaker", CircuitBreaker(5, 300))

    for i in range(10):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.7"), session_hash=f"s{i}")
        app.chat("record lots of contacts", [], request)

    # Session caps alone would allow 10 x 3 = 30; the client cap holds it at 10
    assert len(stored) == 10
