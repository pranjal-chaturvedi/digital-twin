import logging
from functools import lru_cache

from openai import OpenAI
from digital_twin.context import TWIN_SYSTEM_PROMPT
from digital_twin.tools import tools, handle_tool_calls
from digital_twin.styles import CSS, JS, EXAMPLES
from digital_twin.config import MAX_TOOL_ROUNDS, MAX_TOKENS_REPLY, QUEUE_MAX_SIZE, CONCURRENCY_LIMIT
from digital_twin.ratelimit import client_key, rate_limiter
from digital_twin.spend import daily_budget, circuit_breaker
from digital_twin.guards import validate_message, prepare_history, GuardError
from dotenv import load_dotenv
import gradio as gr
import os

logger = logging.getLogger(__name__)

load_dotenv(override=True)

MODEL_NAME = os.getenv("MODEL_NAME")
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("MODEL_API_KEY")

FALLBACK_REPLY = "Sorry, I couldn't put together an answer to that. Could you try rephrasing?"
RATE_LIMITED_REPLY = "Too many requests. Please wait a moment before trying again."
RESTING_REPLY = "I'm resting for today after a lot of conversations. Please come back tomorrow!"
CIRCUIT_OPEN_REPLY = "Service is temporarily experiencing high error rates. Please try again shortly."

system = [{"role": "system", "content": TWIN_SYSTEM_PROMPT}]


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Create the model client on first use, so the module imports without credentials."""
    return OpenAI(base_url=BASE_URL, api_key=API_KEY)


class BudgetExhausted(Exception):
    """The daily model-call budget ran out; reply without calling the model."""


def _complete(messages, **kwargs):
    """One model call: charged to the daily budget and reported to the circuit breaker."""
    if not daily_budget.try_spend():
        raise BudgetExhausted
    try:
        response = get_client().chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=tools, max_tokens=MAX_TOKENS_REPLY, **kwargs  # type: ignore
        )
    except Exception as e:
        # Exception type only: messages and arguments may hold personal data
        logger.warning("model_call_failed error=%s", type(e).__name__)
        circuit_breaker.record_failure()
        raise
    circuit_breaker.record_success()
    return response


def chat(message, history, request: gr.Request = None):
    # Cheap checks first; only the model calls below cost money.
    # 1. Per-client rate limit (counts invalid requests too, so they can't be used to flood)
    if not rate_limiter.allow(client_key(request)):
        return RATE_LIMITED_REPLY

    # 2. Reject bad input before it costs any budget
    try:
        validate_message(message)
    except GuardError as e:
        return e.user_friendly_message

    # 3. Global daily budget: refuse early when it's already gone (each call is charged in _complete)
    if not daily_budget.has_remaining():
        return RESTING_REPLY

    # 4. Upstream health, checked last: in HALF_OPEN this admits the single probe,
    #    which must go on to make a model call and report back
    if not circuit_breaker.can_proceed():
        return CIRCUIT_OPEN_REPLY

    messages = system + prepare_history(history) + [{"role": "user", "content": message}]

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            reply = _complete(messages).choices[0].message
            if not reply.tool_calls:
                return reply.content or FALLBACK_REPLY
            messages.append(reply.model_dump(exclude_none=True))
            messages.extend(handle_tool_calls(reply.tool_calls))

        # Tool rounds used up: forbid further tool calls so the model must answer with what it has
        reply = _complete(messages, tool_choice="none").choices[0].message
        return reply.content or FALLBACK_REPLY
    except BudgetExhausted:
        return RESTING_REPLY
    except Exception as e:
        logger.warning("chat_failed error=%s", type(e).__name__)
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
