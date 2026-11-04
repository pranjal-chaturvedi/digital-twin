from openai import OpenAI
from context import TWIN_SYSTEM_PROMPT
from tools import tools, handle_tool_calls
from styles import CSS, JS, EXAMPLES
from dotenv import load_dotenv
import gradio as gr
import os

load_dotenv(override=True)

MODEL_NAME = os.getenv("MODEL_NAME")
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("MODEL_API_KEY")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))

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


def chat(message, history):
    messages = system + _to_model_messages(history) + [{"role": "user", "content": message}]
    for _ in range(MAX_TOOL_ROUNDS):
        response = openai.chat.completions.create(model=MODEL_NAME, messages=messages, tools=tools)
        reply = response.choices[0].message
        if not reply.tool_calls:
            return reply.content or FALLBACK_REPLY
        messages.append(reply.model_dump(exclude_none=True))
        messages.extend(handle_tool_calls(reply.tool_calls))
    # Tool budget spent: forbid further tool calls so the model must answer with what it has
    response = openai.chat.completions.create(
        model=MODEL_NAME, messages=messages, tools=tools, tool_choice="none"
    )
    return response.choices[0].message.content or FALLBACK_REPLY


if __name__ == "__main__":
    gr.ChatInterface(
        chat,
        examples=EXAMPLES,
        title="Digital Twin",
        description="Talk to my AI twin about my career",
        chatbot=gr.Chatbot(show_label=False),
    ).launch(css=CSS, js=JS, theme=gr.themes.Base())
