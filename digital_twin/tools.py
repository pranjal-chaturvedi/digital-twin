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
from digital_twin.toolguards import (
    ToolCaller,
    ToolInputError,
    clean_name,
    clean_text,
    normalise_email,
    write_guard,
)

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


# Each write tool validates its model-supplied arguments and returns
# (table, record to store, dedupe key). Signatures must match the JSON schemas below.

def record_user_details(email, name=None, notes=None):
    email = normalise_email(email)
    record = {
        "name": clean_name(name) or "Name not provided",
        "email": email,
        "notes": clean_text(notes, MAX_FIELD_LENGTH) or "not provided",
    }
    return "contacts", record, email.lower()


def record_unknown_question(question):
    question = clean_text(question, MAX_FIELD_LENGTH)
    if not question:
        raise ToolInputError("The question is empty. Pass the user's question as text.")
    return "questions", {"question": question}, " ".join(question.casefold().split())


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


LIMIT_REACHED_ERROR = (
    "Recording limit reached for now. Do not call this tool again in this conversation; "
    "tell the user you couldn't save it this time."
)
ALREADY_RECORDED = "Already recorded; nothing more to do."


def _run_tool(tool_name, raw_arguments, caller: ToolCaller):
    """Run one tool call; problems come back as {"error": ...} for the model to read and correct.

    Order: parse -> validate/clean -> dedupe -> write caps -> store. Duplicates and invalid
    input don't use up the caps; only writes that are about to be stored do.
    """
    tool = tool_map.get(tool_name)
    if tool is None:
        return {"error": "Unknown tool. Use only the tools you were given."}
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError):
        return {"error": "Arguments were not valid JSON. Retry with a JSON object matching the tool schema."}
    if not isinstance(arguments, dict):
        return {"error": "Arguments must be a JSON object matching the tool schema."}
    try:
        # Check argument names against the function signature before calling it
        inspect.signature(tool).bind(**arguments)
    except TypeError as e:
        return {"error": f"Invalid arguments: {e}"}

    try:
        table, record, dedupe_key = tool(**arguments)
    except ToolInputError as e:
        print(f"Tool input rejected: {tool_name}", flush=True)
        return {"error": str(e)}

    if write_guard.is_duplicate(caller, tool_name, dedupe_key):
        return ALREADY_RECORDED
    if not write_guard.allow(caller, tool_name):
        print(f"Tool limit reached: {tool_name}", flush=True)
        return {"error": LIMIT_REACHED_ERROR}

    try:
        _append_record(table, record)
    except Exception as e:
        # Arguments may hold PII (emails, names), so log only the error type
        print(f"Tool failed: {tool_name} ({type(e).__name__})", flush=True)
        return {"error": f"The tool failed with {type(e).__name__}. Do not retry; tell the user it didn't work."}
    write_guard.mark_written(caller, tool_name, dedupe_key)
    return "OK"


def handle_tool_calls(tool_calls, caller: ToolCaller):
    # Every tool_call must get exactly one tool reply, or the next API call is rejected
    results = []
    for tool_call in tool_calls:
        tool_name = tool_call.function.name
        # The name comes from the model, so only known names reach the log
        print(f"Tool called: {tool_name if tool_name in tool_map else '<unknown>'}", flush=True)
        result = _run_tool(tool_name, tool_call.function.arguments, caller)
        results.append(
            {"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id}
        )
    return results