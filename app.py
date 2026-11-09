from openai import OpenAI
from digital_twin.context import TWIN_SYSTEM_PROMPT
from digital_twin.tools import tools, handle_tool_calls
from digital_twin.styles import CSS, JS, EXAMPLES
from digital_twin.config import MAX_TOOL_ROUNDS
from digital_twin.guards import (
    validate_message,
    get_client_ip,
    rate_limiter,
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

openai = OpenAI(base_url=BASE_URL, api_key=API_KEY)

system = [{"role": "system", "content": TWIN_SYSTEM_PROMPT}]


def _to_model_messages(history):
    """Reduce Gradio chat history to plain role/content dicts the model API accepts."""
    cleaned = []
    for msg in history:
        content = msg["content"]
        if isinstance(content, str):
            text = content
        else:
            # Gradio 6 gives a list of parts; keep only the text ones (drops files, components)
            text = "\n".join(
                part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        if text:
            cleaned.append({"role": msg["role"], "content": text})
    return cleaned


def chat(message, history, request: gr.Request = None):
    # 1. Check circuit breaker state
    if not circuit_breaker.can_proceed():
        return "Service is temporarily experiencing high error rates. Please try again shortly."

    # 2. Extract client IP and check per-client rate limit
    headers = request.headers if request and hasattr(request, "headers") else {}
    client_ip = get_client_ip(headers)
    
    allowed, rate_limit_msg = rate_limiter.check_rate_limit(client_ip)
    if not allowed:
        return rate_limit_msg

    # 3. Check global daily request budget
    allowed, budget_msg = daily_budget.check_budget()
    if not allowed:
        return budget_msg

    # 4. Validate message and history length
    try:
        validate_message(message, history)
    except GuardError as e:
        return e.user_friendly_message

    messages = system + \
        _to_model_messages(history) + [{"role": "user", "content": message}]
    
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = openai.chat.completions.create(
                model=MODEL_NAME, messages=messages, tools=tools, max_tokens=MAX_TOKENS_REPLY)  # type: ignore
            reply = response.choices[0].message
            if not reply.tool_calls:
                circuit_breaker.record_success()
                return reply.content or FALLBACK_REPLY
            messages.append(reply.model_dump(exclude_none=True))
            messages.extend(handle_tool_calls(reply.tool_calls))
        
        # Tool budget spent: forbid further tool calls so the model must answer with what it has
        response = openai.chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=tools, tool_choice="none", max_tokens=MAX_TOKENS_REPLY  # type: ignore
        )
        circuit_breaker.record_success()
        return response.choices[0].message.content or FALLBACK_REPLY
    except Exception as e:
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
    ).launch(css=CSS, js=JS, theme=gr.themes.Base())
