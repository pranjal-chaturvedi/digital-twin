# Protections

The app is public and every message costs money, so the code limits what one visitor, or everyone together, can do. This page explains each protection, how to configure it, and how to check it after a deploy.

## Before the model is called

Each check runs before any model call, in this order:

1. **Per-visitor rate limit.** Requests are counted per visitor in a sliding window. The visitor is identified from `X-Forwarded-For`, read *from the right*: proxies append to that header, so anything further left was sent by the visitor and could be forged.
2. **Message checks.** Empty and over-length messages are rejected.
3. **Daily budget.** A global cap on model calls per UTC day. Every call counts, including each round of the tool loop. When it's spent, the twin says it's resting until tomorrow.
4. **Circuit breaker.** After repeated model API errors, calls pause for a while, then a single test request checks whether the API has recovered.
5. **Chat history clean-up.** History comes from the browser, so it's treated as untrusted: only user and assistant turns are kept (no injected system messages), and it's trimmed to the most recent turns and a total size.

Every model call also has a reply-length cap (`max_tokens`), and Gradio's queue bounds how many chats wait and run at once.

## The write tools

The model can be talked into misusing its tools, so they get their own checks:

- **Validation.** Emails are checked strictly, and known disposable domains are refused. Names and notes are length-capped and stripped of control and invisible characters.
- **Caps.** Each conversation can store only a few contacts and questions, and each visitor has a cap across all their conversations, so opening new sessions doesn't get around it.
- **Duplicates** are stored once.
- **Refusals** go back to the model as tool errors, so it can tell the visitor instead of failing.

## The system prompt

The system prompt tells the model to keep its instructions private, and to ignore claims of admin or owner authority and role-play attempts. Code can't enforce this part; the [live adversarial tests](development.md#live-tests-against-the-real-model) check it against the real model.

Two things code can't fully prevent:

- The browser controls the chat history, so a visitor can fake earlier *assistant* replies (for example, one that "agrees" to drop the rules). Injected system messages are removed, but assistant text can't be verified.
- Anyone can ask for an email address's details to be deleted. Check that the request is genuine before acting on it.

## Limits are per process

Counters live in memory, so they reset whenever the app restarts and aren't shared between instances. That's enough for a single small instance; anything bigger would need a shared store.

## Configuration

All limits are optional environment variables with safe defaults. Invalid or non-positive values fall back to the default instead of turning a check off.

| Variable | Default | What it limits |
|---|---|---|
| `GUARD_MAX_MESSAGE_LENGTH` | `500` | Characters per message |
| `GUARD_MAX_HISTORY_TURNS` | `20` | Past turns sent to the model |
| `GUARD_MAX_HISTORY_CHARS` | `12000` | Total characters of history sent to the model |
| `GUARD_MAX_TOKENS_REPLY` | `1024` | `max_tokens` on every model call |
| `MAX_TOOL_ROUNDS` | `5` | Tool rounds per message, plus one final tool-free call |
| `GUARD_RATE_LIMIT_REQUESTS` | `30` | Requests per visitor per window |
| `GUARD_RATE_LIMIT_WINDOW` | `60` | Rate-limit window, in seconds |
| `GUARD_RATE_LIMIT_MAX_CLIENTS` | `10000` | Visitors tracked at once (least recently seen are dropped) |
| `TRUSTED_PROXY_COUNT` | `2` | Proxies that append to `X-Forwarded-For`; Render puts Cloudflare and its own proxy in front, hence 2. `0` ignores the header |
| `GUARD_QUEUE_MAX_SIZE` | `20` | Chats waiting in Gradio's queue |
| `GUARD_CONCURRENCY_LIMIT` | `4` | Chats running at once |
| `GUARD_DAILY_BUDGET` | `1000` | Model calls per UTC day, across all visitors |
| `GUARD_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive model API errors before pausing |
| `GUARD_CIRCUIT_OPEN_TIME` | `300` | Pause length, in seconds |
| `GUARD_RECORD_USER_DETAILS_LIMIT` | `3` | Contacts stored per conversation |
| `GUARD_RECORD_QUESTION_LIMIT` | `5` | Questions stored per conversation |
| `GUARD_CLIENT_WRITE_LIMIT` | `10` | Records stored per visitor per window, across conversations |
| `GUARD_CLIENT_WRITE_WINDOW` | `3600` | That window, in seconds |
| `GUARD_MAX_FIELD_LENGTH` | `2000` | Characters per stored notes or question field |
| `GUARD_MAX_NAME_LENGTH` | `100` | Characters per stored name |
| `LIMITER_BACKEND` | `memory` | Only `memory` exists; any other value stops the app at startup |

## Checking visitor identification after a deploy

`TRUSTED_PROXY_COUNT` depends on the hosting setup, so check it on the live app. The script sends over-length messages with a different forged `X-Forwarded-For` header each time. They count towards the rate limit but never reach the model, so the check costs nothing.

```bash
# From one network: must end with PASS (forged headers don't escape the limit)
uv run python scripts/check_client_ip.py https://<your-app>.onrender.com

# Within a minute, from a second network (e.g. a phone hotspot): must PASS (visitors don't share a limit)
uv run python scripts/check_client_ip.py https://<your-app>.onrender.com --requests 1
```

If the first run fails, the count is too high and forged headers are being trusted. If the second fails, it's too low and different visitors share one limit.

Running the script against a local copy of the app fails even when the app is right, because there are no proxies locally and uvicorn trusts `X-Forwarded-For` from `127.0.0.1`. Locally, add `--simulate-proxy-hops "99.61.165.29, 104.22.17.40"`. For the same reason, never set `FORWARDED_ALLOW_IPS=*` on Render.
