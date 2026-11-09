# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-09

Abuse protection: limits on what one visitor, or all visitors together, can make the app do or spend. See [Protections](https://github.com/pranjal-chaturvedi/digital-twin/blob/main/docs/protections.md).

### Added
- Per-visitor rate limit, with visitors identified from `X-Forwarded-For` read from the right (`TRUSTED_PROXY_COUNT`).
- Daily budget of model calls per UTC day, counting every call in the tool loop, with a "resting until tomorrow" reply when it's spent.
- Circuit breaker that pauses model calls after repeated API errors and tests recovery with a single request.
- Caps on stored records per conversation and per visitor, duplicate detection, and strict validation and cleaning of emails, names, notes and questions.
- Gradio queue size and concurrency limits.
- A Security section in the system prompt (instructions stay private; claims of authority and role-play don't change the rules).
- All limits configurable through environment variables with safe defaults, in one config module.
- Test suite (`uv run pytest`) with adversarial cases, plus an opt-in, spend-capped run against the real model (`--live`).
- `scripts/check_client_ip.py` to check visitor identification on a live deploy at no cost.
- Technical documentation in `docs/`: architecture, protections, and development (setup, tests, branches and releases).
- CI that runs the tests on pushes and pull requests to `develop` and `main`, and creates a GitHub release when a new version reaches `main`.

### Changed
- Long conversations are trimmed to the most recent turns instead of being refused.
- Chats can run in parallel (up to `GUARD_CONCURRENCY_LIMIT`); previously Gradio ran one at a time.
- Numeric email addresses (e.g. QQ mail) and domains that merely contain words like "spam" are accepted; known disposable-email domains are matched exactly.
- The README is a short, non-technical overview; the details moved to `docs/`, and the changelog to `CHANGELOG.md`.

### Removed
- The deploy workflow. Render deploys `main` by itself, so the repo no longer tries to.

### Fixed
- A forged `X-Forwarded-For` header could get a fresh rate limit on every request.
- Chat history from the browser could inject system messages, had no size limit, and could crash the handler if malformed.
- Rejected messages used up the daily budget, and the budget counted chats rather than model calls.
- Contact and question limits and input validation existed but were never applied to the tools.
- Some control and invisible characters were stored unfiltered.
- Logs could include email domains, session ids and message text.

## [0.1.0] - 2026-11-04

First release.

### Added
- Chat app built with Gradio, with a system prompt generated from my LinkedIn profile.
- Agent loop written from scratch, without an agentic framework.
- Limit on tool rounds per message, configurable with `MAX_TOOL_ROUNDS`, with a final tool-free answer when the limit is reached.
- Tool errors (invalid arguments, unknown tools, failing tools) are returned to the model instead of crashing the app.
- Gradio chat history is converted to plain text messages before being sent to the model.
- Tools to record contact details and questions the agent can't answer, stored in a private Hugging Face Dataset.
- Handling of requests to delete stored details.
- A privacy notice in the chat UI explaining what's stored and why.

[Unreleased]: https://github.com/pranjal-chaturvedi/digital-twin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pranjal-chaturvedi/digital-twin/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pranjal-chaturvedi/digital-twin/releases/tag/v0.1.0
