import inspect
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import CommitScheduler

from digital_twin.config import MAX_FIELD_LENGTH

load_dotenv(override=True)

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO")
PUSH_EVERY_MINUTES = 1

# A Space starts each run with an empty disk. Reusing a file name would overwrite the
# copy already on the Hub instead of adding to it, so every run writes its own files.
_RUN_ID = uuid.uuid4().hex
_scheduler = None
_scheduler_init_lock = threading.Lock()


def _get_scheduler():
    """Create the Hub uploader on first use, so the app can start without storage configured."""
    global _scheduler
    with _scheduler_init_lock:
        if _scheduler is None:
            if not HF_DATASET_REPO:
                raise RuntimeError("HF_DATASET_REPO is not set")
            scheduler = CommitScheduler(
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                private=True,
                folder_path=tempfile.mkdtemp(prefix="digital-twin-"),
                every=PUSH_EVERY_MINUTES,
            )
            # `private=True` is ignored for a repo that already exists, so check it explicitly
            if not scheduler.api.repo_info(HF_DATASET_REPO, repo_type="dataset").private:
                scheduler.stop()
                raise RuntimeError("HF_DATASET_REPO is public; refusing to store personal data in it")
            _scheduler = scheduler
    return _scheduler


def _append_record(table, record):
    """Append a record to this run's JSON Lines file for `table`; the scheduler pushes it to the Hub."""
    scheduler = _get_scheduler()
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **record}
    path = scheduler.folder_path / table / f"{_RUN_ID}.jsonl"
    with scheduler.lock:
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _clip(value):
    """Cap a model-supplied value so one call can't store an arbitrarily large blob."""
    return str(value)[:MAX_FIELD_LENGTH]


def record_user_details(email, name="Name not provided", notes="not provided"):
    _append_record("contacts", {"name": _clip(name), "email": _clip(email), "notes": _clip(notes)})
    return "OK"


def record_unknown_question(question):
    _append_record("questions", {"question": _clip(question)})
    return "OK"


record_user_details_json = {
    "name": "record_user_details",
    "description": "Use this tool to record that a user is interested in being in touch and provided an email address",
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The email address of this user"},
            "name": {"type": "string", "description": "The user's name, if they provided it"},
            "notes": {
                "type": "string",
                "description": "Any additional info about the conversation that's worth recording to give context",
            },
        },
        "required": ["email"],
        "additionalProperties": False,
    },
}

record_unknown_question_json = {
    "name": "record_unknown_question",
    "description": "Always use this tool to record any question that couldn't be answered as you didn't know the answer",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question that couldn't be answered"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

tools = [
    {"type": "function", "function": record_user_details_json},
    {"type": "function", "function": record_unknown_question_json},
]

tool_map = {
    "record_user_details": record_user_details,
    "record_unknown_question": record_unknown_question,
}


def _run_tool(tool_name, raw_arguments):
    """Run one tool call; problems come back as {"error": ...} for the model to read and correct."""
    tool = tool_map.get(tool_name)
    if tool is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"error": "Arguments were not valid JSON. Retry with a JSON object matching the tool schema."}
    if not isinstance(arguments, dict):
        return {"error": "Arguments must be a JSON object matching the tool schema."}
    try:
        # Check argument names against the function signature before calling it
        inspect.signature(tool).bind(**arguments)
    except TypeError as e:
        return {"error": f"Invalid arguments: {e}"}
    try:
        return tool(**arguments)
    except Exception as e:
        # Arguments may hold PII (emails, names), so log only the error type
        print(f"Tool failed: {tool_name} ({type(e).__name__})", flush=True)
        return {"error": f"The tool failed with {type(e).__name__}. Do not retry; tell the user it didn't work."}


def handle_tool_calls(tool_calls):
    # Every tool_call must get exactly one tool reply, or the next API call is rejected
    results = []
    for tool_call in tool_calls:
        tool_name = tool_call.function.name
        print(f"Tool called: {tool_name}", flush=True)
        result = _run_tool(tool_name, tool_call.function.arguments)
        results.append(
            {"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id}
        )
    return results