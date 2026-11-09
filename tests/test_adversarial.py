"""Deterministic adversarial tests: every fixture through the real handler with a fake model.

These check what the code guarantees regardless of how the model behaves. Model behaviour
on the same fixtures is checked by tests/test_adversarial_live.py (opt-in, real API).
"""
import unicodedata

import pytest

import app
from digital_twin import tools
from digital_twin.ratelimit import SlidingWindowRateLimiter, client_key
from digital_twin.spend import DailyBudget

from digital_twin.config import MAX_HISTORY_CHARS
from tests.adversarial import (
    REAL_CLIENT,
    FakeModel,
    instruction_lines,
    isolate,
    leaked_lines,
    load_cases,
    make_request,
)

CASES = load_cases()
REQUEST_CASES = [c for c in CASES if "forged_prefix_per_request" not in c]
HEADER_CASES = [c for c in CASES if "forged_prefix_per_request" in c]


def _case_id(case):
    return case["id"]


def _ids(cases):
    return [c["id"] for c in cases]


def _run(case, monkeypatch):
    budget = DailyBudget(1000)
    stored = isolate(monkeypatch, budget)
    model = FakeModel(case.get("fake_tool_calls"))
    monkeypatch.setattr(app, "get_client", lambda: model)
    reply = app.chat(case["message"], case.get("history", []), make_request(case["id"]))
    return reply, model, stored, budget


def test_fixture_file_is_well_formed():
    ids = _ids(CASES)
    assert len(ids) == len(set(ids)), "duplicate fixture ids"
    categories = {c["category"] for c in CASES}
    assert {"prompt_extraction", "contact_spam", "jailbreak", "long_input", "multilingual", "forged_headers"} <= categories
    for case in CASES:
        assert "message" in case and "expect" in case, case["id"]


@pytest.mark.parametrize("case", [c for c in REQUEST_CASES if c["expect"]["blocked"]], ids=_case_id)
def test_blocked_cases_never_reach_the_model(case, monkeypatch):
    reply, model, stored, budget = _run(case, monkeypatch)
    assert model.requests == []
    assert budget.used == 0
    assert stored == []
    assert reply not in ("fake answer", app.FALLBACK_REPLY)


@pytest.mark.parametrize("case", [c for c in REQUEST_CASES if not c["expect"]["blocked"]], ids=_case_id)
class TestCasesThatReachTheModel:
    def test_user_text_never_gains_authority(self, case, monkeypatch):
        _, model, _, _ = _run(case, monkeypatch)
        sent = model.requests[0]["messages"]
        # Exactly one system message: ours, first. The visitor's text is only ever a user turn.
        assert sent[0] == {"role": "system", "content": app.TWIN_SYSTEM_PROMPT}
        assert [m["role"] for m in sent].count("system") == 1
        assert sent[-1] == {"role": "user", "content": case["message"]}
        assert {m["role"] for m in sent[1:-1]} <= {"user", "assistant"}

    def test_injected_content_is_not_sent(self, case, monkeypatch):
        _, model, _, _ = _run(case, monkeypatch)
        for request in model.requests:
            sent_text = "\n".join(str(m.get("content")) for m in request["messages"])
            for marker in case["expect"].get("not_sent", []):
                assert marker not in sent_text

    def test_every_model_call_is_bounded(self, case, monkeypatch):
        _, model, _, budget = _run(case, monkeypatch)
        assert len(model.requests) <= app.MAX_TOOL_ROUNDS + 1
        assert budget.used == len(model.requests)
        assert all(r["max_tokens"] == app.MAX_TOKENS_REPLY for r in model.requests)
        history_chars = sum(len(m["content"]) for m in model.requests[0]["messages"][1:-1])
        assert history_chars <= MAX_HISTORY_CHARS

    def test_writes_are_capped_and_clean(self, case, monkeypatch):
        _, _, stored, _ = _run(case, monkeypatch)
        assert len(stored) <= case["expect"]["max_stored"]
        if "stored" in case["expect"]:
            for expected, actual in zip(case["expect"]["stored"], stored, strict=True):
                assert expected.items() <= actual.items()
        for record in stored:
            for field, value in record.items():
                assert len(value) <= max(tools.MAX_FIELD_LENGTH, 254), field
                assert not any(
                    unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\n\t" for ch in value
                ), f"control/format character stored in {field}"

    def test_fake_answer_never_counts_as_a_leak(self, case, monkeypatch):
        # Sanity check for the live run's leak detector: a harmless reply isn't flagged
        reply, _, _, _ = _run(case, monkeypatch)
        assert leaked_lines(reply) == []


@pytest.mark.parametrize("case", HEADER_CASES, ids=_case_id)
def test_forged_headers_map_to_one_client(case, monkeypatch):
    requests = [make_request(case["id"], forged_prefix=p) for p in case["forged_prefix_per_request"]]
    assert {client_key(r, trusted_proxy_count=2) for r in requests} == {REAL_CLIENT}


@pytest.mark.parametrize("case", HEADER_CASES, ids=_case_id)
def test_forged_headers_cannot_buy_extra_requests(case, monkeypatch):
    isolate(monkeypatch)
    model = FakeModel()
    monkeypatch.setattr(app, "get_client", lambda: model)
    monkeypatch.setattr(app, "rate_limiter", SlidingWindowRateLimiter(3, 60))
    monkeypatch.setattr(app, "client_key", lambda r: client_key(r, trusted_proxy_count=2))

    replies = [
        app.chat(case["message"], [], make_request(case["id"], forged_prefix=p))
        for p in case["forged_prefix_per_request"]
    ]
    assert len(model.requests) == 3
    assert replies.count(app.RATE_LIMITED_REPLY) == len(replies) - 3


def test_leak_detector_catches_a_verbatim_leak():
    leaked = "Sure! My rules say: " + instruction_lines()[0].upper()
    assert leaked_lines(leaked)


def test_leak_detector_ignores_the_public_profile():
    # The LinkedIn context is meant to be used in answers, so quoting it isn't a leak
    assert all("linkedin" not in line for line in instruction_lines())
