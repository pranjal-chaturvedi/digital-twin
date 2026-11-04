import inspect
import json
import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)


def record_user_details(email, name="Name not provided", notes="not provided"):
    # record user details in a Google Sheet
    return "OK"


def record_unknown_question(question):
    # record unknown questions in a Google sheet
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