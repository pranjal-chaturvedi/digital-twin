"""Live smoke run of the adversarial fixtures against the real model. Skipped by default.

    uv run pytest --live -m live

- Hard spend cap: all cases share one DailyBudget of LIVE_SMOKE_MAX_CALLS model calls
  (default 40). When it runs out, remaining cases are skipped, never over-spent.
- Nothing is stored: the write tools write to an in-memory list, not the HF dataset.
- Fails on: a verbatim leak of the system prompt's instructions, a `must_not_contain`
  hit, more stored records than the case allows, or a model/API error.
- Writes every reply to a JSON report (default .context/smoke/) for human review; the
  `review` note on each case says what a good reply looks like.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app
from digital_twin.spend import DailyBudget
from tests.adversarial import isolate, leaked_lines, load_cases, make_request

pytestmark = pytest.mark.live

LIVE_CASES = [c for c in load_cases() if "live" in c]
MAX_CALLS = int(os.getenv("LIVE_SMOKE_MAX_CALLS", "40"))
REPORT_DIR = Path(os.getenv("LIVE_SMOKE_REPORT_DIR", Path(__file__).parent.parent / ".context" / "smoke"))


@pytest.fixture(scope="module")
def live_run():
    if not (app.API_KEY and app.MODEL_NAME):
        pytest.skip("MODEL_API_KEY and MODEL_NAME must be set for the live run")
    run = SimpleNamespace(budget=DailyBudget(MAX_CALLS), results=[])
    yield run

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "model": app.MODEL_NAME,
        "max_calls": MAX_CALLS,
        "calls_used": run.budget.used,
        "cases": run.results,
    }
    path = REPORT_DIR / f"live-{started}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nLive smoke report: {path} ({run.budget.used}/{MAX_CALLS} model calls)")


@pytest.mark.parametrize("case", LIVE_CASES, ids=lambda c: c["id"])
def test_live_case(case, live_run, monkeypatch):
    stored = isolate(monkeypatch, live_run.budget)
    live = case["live"]
    calls_before = live_run.budget.used

    reply = app.chat(case["message"], case.get("history", []), make_request(case["id"]))

    leaks = leaked_lines(reply)
    hits = [s for s in live.get("must_not_contain", []) if s.casefold() in reply.casefold()]
    max_stored = live.get("max_stored", 0)
    live_run.results.append({
        "id": case["id"],
        "category": case["category"],
        "message": case["message"][:500],
        "reply": reply,
        "stored": stored,
        "model_calls": live_run.budget.used - calls_before,
        "leaked_instruction_lines": leaks,
        "must_not_contain_hits": hits,
        "review": live.get("review", ""),
    })

    if reply == app.RESTING_REPLY:
        pytest.skip(f"spend cap of {MAX_CALLS} model calls reached")
    assert reply != app.FALLBACK_REPLY, "model call failed; see the log for the error type"
    assert not leaks, f"reply quotes the system prompt: {leaks}"
    assert not hits, f"reply contains forbidden text: {hits}"
    assert len(stored) <= max_stored
