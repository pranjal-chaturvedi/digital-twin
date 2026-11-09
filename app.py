from functools import lru_cache

from openai import OpenAI
from digital_twin.context import TWIN_SYSTEM_PROMPT
from digital_twin.tools import tools, handle_tool_calls
from digital_twin.styles import CSS, JS, EXAMPLES
from digital_twin.config import MAX_TOOL_ROUNDS, QUEUE_MAX_SIZE, CONCURRENCY_LIMIT
from digital_twin.ratelimit import client_key, rate_limiter
from digital_twin.guards import (
    validate_message,
    prepare_history,
    daily_budget,
    circuit_breaker,
    GuardError,
    MAX_TOKENS_REPLY,
)
from dotenv import load_dotenv
import gradio as gr
import os

load_dotenv(override=True)

MODEL_NAME = os.getenv("MODEL_NAME")
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("MODEL_API_KEY")

FALLBACK_REPLY = "Sorry, I couldn't put together an answer to that. Could you try rephrasing?"
RATE_LIMITED_REPLY = "Too many requests. Please wait a moment before trying again."

system = [{"role": "system", "content": TWIN_SYSTEM_PROMPT}]


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Create the model client on first use, so the module imports without credentials."""
    return OpenAI(base_url=BASE_URL, api_key=API_KEY)


def chat(message, history, request: gr.Request = None):
    # Cheap checks first; nothing below the budget check is free.
    # 1. Upstream health
    if not circuit_breaker.can_proceed():
        return "Service is temporarily experiencing high error rates. Please try again shortly."

    # 2. Per-client rate limit (counts invalid requests too, so they can't be used to flood)
    if not rate_limiter.allow(client_key(request)):
        return RATE_LIMITED_REPLY

    # 3. Reject bad input before it costs any budget
    try:
        validate_message(message)
    except GuardError as e:
        return e.user_friendly_message

    # 4. Global daily budget, charged only for requests that will reach the model
    allowed, budget_msg = daily_budget.check_budget()
    if not allowed:
        return budget_msg

    messages = system + prepare_history(history) + [{"role": "user", "content": message}]
    client = get_client()

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=MODEL_NAME, messages=messages, tools=tools, max_tokens=MAX_TOKENS_REPLY)  # type: ignore
            reply = response.choices[0].message
            if not reply.tool_calls:
                circuit_breaker.record_success()
                return reply.content or FALLBACK_REPLY
            messages.append(reply.model_dump(exclude_none=True))
            messages.extend(handle_tool_calls(reply.tool_calls))

        # Tool budget spent: forbid further tool calls so the model must answer with what it has
        response = client.chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=tools, tool_choice="none", max_tokens=MAX_TOKENS_REPLY  # type: ignore
        )
        circuit_breaker.record_success()
        return response.choices[0].message.content or FALLBACK_REPLY
    except Exception:
        circuit_breaker.record_failure()
        return FALLBACK_REPLY


if __name__ == "__main__":
    gr.ChatInterface(
        chat,
        examples=EXAMPLES,
        title="Digital Twin",
        description=(
            "Talk to my AI twin about my career.\n\n"
            "<sub>If you share your name or email in the chat, they're stored privately so I can "
            "follow up, and questions the twin can't answer are saved so I can improve it. "
            "Ask in the chat to have your details deleted.</sub>"
        ),
        chatbot=gr.Chatbot(show_label=False),
    ).queue(
        # A flood waits in a bounded queue instead of piling up slow model calls
        max_size=QUEUE_MAX_SIZE,
        default_concurrency_limit=CONCURRENCY_LIMIT,
    ).launch(css=CSS, js=JS, theme=gr.themes.Base())
