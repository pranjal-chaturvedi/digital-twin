"""Shared harness for the adversarial fixtures (tests/fixtures/adversarial.json)."""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import app
from digital_twin import tools
from digital_twin.context import TWIN_SYSTEM_PROMPT
from digital_twin.ratelimit import SlidingWindowRateLimiter
from digital_twin.spend import CircuitBreaker, DailyBudget
from digital_twin.toolguards import ToolWriteGuard

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "adversarial.json"

# What Render's proxies append after anything the client sent (see .context/audit.md)
REAL_CLIENT = "99.61.165.29"
CLOUDFLARE_EDGE = "104.22.17.40"
RENDER_PROXY_PEER = "10.21.157.68"

# Fixed caps, so a local .env can't change what the tests expect
SESSION_LIMITS = {"record_user_details": 3, "record_unknown_question": 5}
CLIENT_WRITE_LIMIT = 10


def _expand(value):
    """Expand {"repeat": s, "times": n} placeholders anywhere in a fixture value."""
    if isinstance(value, dict):
        if set(value) == {"repeat", "times"}:
            return value["repeat"] * value["times"]
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_cases():
    with FIXTURES_PATH.open(encoding="utf-8") as f:
        return [_expand(case) for case in json.load(f)["cases"]]


def make_request(case_id, forged_prefix=None, host=RENDER_PROXY_PEER):
    """A gr.Request-like object as the app would see it behind Cloudflare + Render."""
    hops = [hop for hop in [forged_prefix, REAL_CLIENT, CLOUDFLARE_EDGE] if hop is not None]
    return SimpleNamespace(
        headers={"x-forwarded-for": ", ".join(hops)},
        client=SimpleNamespace(host=host),
        session_hash=f"session-{case_id}",
    )


def isolate(monkeypatch, budget=None):
    """Fresh guards and in-memory storage for one run; returns the list writes land in."""
    stored = []
    monkeypatch.setattr(tools, "_append_record", lambda table, record: stored.append({"table": table, **record}))
    monkeypatch.setattr(tools, "write_guard", ToolWriteGuard(
        session_limits=SESSION_LIMITS, client_limit=CLIENT_WRITE_LIMIT, client_window_seconds=3600,
    ))
    monkeypatch.setattr(app, "rate_limiter", SlidingWindowRateLimiter(1000, 60))
    monkeypatch.setattr(app, "daily_budget", budget or DailyBudget(1000))
    monkeypatch.setattr(app, "circuit_breaker", CircuitBreaker(5, 300))
    return stored


class FakeModel:
    """Makes the fixture's tool calls every round (until tools are disabled), then answers.

    `{n}` in string arguments is replaced with a running counter, so each call can differ.
    """

    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []
        self.requests = []
        self._n = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _fill(self, value):
        if isinstance(value, str):
            return value.replace("{n}", str(self._n))
        if isinstance(value, dict):
            return {k: self._fill(v) for k, v in value.items()}
        return value

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.tool_calls or kwargs.get("tool_choice") == "none":
            message = SimpleNamespace(content="fake answer", tool_calls=None)
        else:
            calls = []
            for spec in self.tool_calls:
                for _ in range(spec.get("times", 1)):
                    self._n += 1
                    calls.append(SimpleNamespace(
                        id=f"call_{self._n}",
                        function=SimpleNamespace(
                            name=spec["name"],
                            arguments=json.dumps(self._fill(spec["arguments"]), ensure_ascii=False),
                        ),
                    ))
            message = SimpleNamespace(
                content=None,
                tool_calls=calls,
                model_dump=lambda exclude_none=False: {"role": "assistant", "content": None},
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# --- Prompt-leak detection ---------------------------------------------------------------

def _normalise(text):
    return " ".join(text.casefold().split())


def instruction_lines(min_length=25):
    """Lines of the system prompt that are instructions, not the (public) LinkedIn context.

    A reply that contains one of these verbatim has leaked the instructions.
    """
    lines, in_context = [], False
    for line in TWIN_SYSTEM_PROMPT.splitlines():
        if line.startswith("# "):
            in_context = line.strip() == "# Context"
            continue
        if not in_context and len(line.strip()) >= min_length:
            lines.append(_normalise(line))
    return lines


def leaked_lines(reply):
    reply_norm = _normalise(reply)
    # Compare on sentence-sized pieces too, so a leak split across lines is still caught
    pieces = set()
    for line in instruction_lines():
        pieces.add(line)
        pieces.update(p.strip() for p in re.split(r"(?<=[.:])\s", line) if len(p.strip()) >= 25)
    return sorted(p for p in pieces if p in reply_norm)
