"""Guards on what the user sends: the new message and the client-supplied history."""
import logging

from digital_twin.config import (
    MAX_MESSAGE_LENGTH,
    MAX_HISTORY_TURNS,
    MAX_HISTORY_CHARS,
)

logger = logging.getLogger(__name__)


class GuardError(Exception):
    def __init__(self, message, user_friendly_message=None):
        super().__init__(message)
        self.user_friendly_message = user_friendly_message or "Sorry, I couldn't process your request. Please try again."


def validate_message(message: str):
    """Reject a new user message that shouldn't reach the model. Raises GuardError."""
    if not isinstance(message, str):
        logger.warning("guard_invalid_message_type")
        raise GuardError("Message must be a string", "Sorry, your message format wasn't recognized.")

    if not message.strip():
        logger.warning("guard_empty_message")
        raise GuardError("Message is empty", "Please type a message first.")

    if len(message) > MAX_MESSAGE_LENGTH:
        logger.warning("guard_max_message_length_exceeded", extra={"limit": MAX_MESSAGE_LENGTH})
        raise GuardError(
            f"Message exceeds maximum allowed length of {MAX_MESSAGE_LENGTH}",
            f"Your message is too long. Please keep messages under {MAX_MESSAGE_LENGTH} characters."
        )


_HISTORY_ROLES = {"user", "assistant"}


def _history_text(content) -> str:
    """Text of one history entry; Gradio 6 gives a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Keep only text parts (drops files and components)
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def prepare_history(history) -> list[dict]:
    """Turn client-supplied chat history into model messages that are safe to send.

    History arrives from the client, so it's untrusted: only user/assistant roles are
    kept (no injected system or tool messages), malformed entries are skipped, and the
    result is truncated to the most recent MAX_HISTORY_TURNS turns and MAX_HISTORY_CHARS
    characters. Older context is dropped rather than rejecting the request.
    """
    if not isinstance(history, list):
        return []

    cleaned = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") not in _HISTORY_ROLES:
            continue
        text = _history_text(msg.get("content"))
        if text:
            cleaned.append({"role": msg["role"], "content": text})

    # Walk back from the newest message until either cap is reached
    kept = []
    total_chars = 0
    for msg in reversed(cleaned[-(MAX_HISTORY_TURNS * 2):]):
        total_chars += len(msg["content"])
        if total_chars > MAX_HISTORY_CHARS:
            break
        kept.append(msg)
    kept.reverse()

    if len(kept) < len(cleaned):
        logger.info("guard_history_truncated", extra={"kept": len(kept), "dropped": len(cleaned) - len(kept)})
    return kept
