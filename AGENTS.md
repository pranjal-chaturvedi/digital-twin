# AGENTS.md

Instructions for AI coding agents working in this repo. This file is the single source of truth; tool-specific files (CLAUDE.md, etc.) only point here.

## Project
A Gradio chat app (`app.py`) that answers questions as a "digital twin" using an OpenAI-compatible model, with two write tools (`digital_twin/tools.py`) that store contacts and unanswered questions in a private Hugging Face dataset. Deployed on Render.

- `digital_twin/config.py`: every env-driven limit, with safe defaults. Add new limits here, not inline.
- `digital_twin/guards.py`: abuse-protection guards (caps, rate limiter, budget, circuit breaker, tool limits, input validation).
- `tests/`: pytest suite.
- `.context/` (untracked): planning notes, including `roadmap.md` and `audit.md` (where each guard hooks in and what's still open). Read the audit instead of re-auditing the repo.
- `docs/`: project documentation only. Planning and audit notes go in `.context/`.

## Rules
- **Stay framework-free.** Plain Python plus the existing dependencies; no web frameworks, DI containers or validation libraries.
- **Never log tool arguments, message content or personal data** (names, emails, email domains, IPs, session ids). Log event names and counts only.
- **Run `uv run pytest` before finishing** any change, and report the result.
- **Add no new dependencies without asking first.**
- **No Redis or other external store.** Limiters and counters keep their in-memory backend behind an interface (`LIMITER_BACKEND=memory`).
- **Don't read `.env`** or print secret values. Refer to env vars by name only.
