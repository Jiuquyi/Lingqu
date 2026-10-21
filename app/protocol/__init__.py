"""Request field extraction, incremental SSE parsing, usage and error bodies (DESIGN §4.4, §5)."""
import json

from .anthropic_messages import AnthropicTracker
from .openai_chat import ChatTracker
from .openai_responses import ResponsesTracker

ANTHROPIC = "anthropic"
OPENAI_CHAT = "openai.chat"
OPENAI_RESPONSES = "openai.responses"

ENDPOINTS = {
    "/v1/messages": ANTHROPIC,
    "/v1/chat/completions": OPENAI_CHAT,
    "/v1/responses": OPENAI_RESPONSES,
}
TRACKERS = {ANTHROPIC: AnthropicTracker, OPENAI_CHAT: ChatTracker, OPENAI_RESPONSES: ResponsesTracker}


def protocol_of(path):
    return ENDPOINTS.get(path.rstrip("/"))


def read_fields(body):
    """model, stream and metadata.user_id from the raw body, which is never rewritten."""
    try:
        obj = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    return {"model": obj.get("model") if isinstance(obj.get("model"), str) else "",
            "stream": obj.get("stream") is True,
            "user_id": meta.get("user_id") if isinstance(meta.get("user_id"), str) else "",
            "user": obj.get("user") if isinstance(obj.get("user"), str) else ""}


def error_body(protocol, message):
    if protocol == ANTHROPIC:
        return {"type": "error", "error": {"type": "overloaded_error", "message": message}}
    return {"error": {"message": message, "type": "server_error", "code": "lingqu_throttled", "param": None}}


def classify_error(status, text):
    """Error class for the ledger (DESIGN §4.7); None for a success."""
    if status < 400:
        return None
    t = (text or "").lower()
    if status == 401 or any(s in t for s in ("oauth access token has been revoked", "invalid_grant",
                                             "account_on_hold", "token has been revoked")):
        return "account_auth"
    if status == 529 or "overloaded" in t:
        return "upstream_overloaded"
    if status == 429 or "rate_limit" in t or "rate limit" in t:
        return "upstream_rate_limited"
    if status in (502, 504) or any(s in t for s in ("etimedout", "fetch failed", "timed out", "timeout")):
        return "upstream_timeout"
    if 400 <= status < 500:
        return "client_error"
    return "upstream_error"
