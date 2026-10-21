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
    """model, stream, metadata.user_id, user and prompt_cache_key from the raw body, which is
    never rewritten."""
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
            "user": obj.get("user") if isinstance(obj.get("user"), str) else "",
            "prompt_cache_key": obj.get("prompt_cache_key") if isinstance(obj.get("prompt_cache_key"), str) else ""}


def error_body(protocol, message):
    if protocol == ANTHROPIC:
        return {"type": "error", "error": {"type": "overloaded_error", "message": message}}
    return {"error": {"message": message, "type": "server_error", "code": "lingqu_throttled", "param": None}}


# Error classes (DESIGN §4.7). Signals are matched on the lower-cased error body, whatever the
# status: 9router answers a failed OAuth refresh with a 5xx whose text says invalid_grant.
AUTH_SIGNS = ("authentication_error", "oauth access token has been revoked", "token has been revoked",
              "invalid_grant", "account_on_hold", "account is on hold", "token_expired", "token has expired",
              "refresh_token_reused", "refresh_token_expired", "account_deactivated", "deactivated_workspace",
              "organization has been disabled", "account has been disabled", "account has been suspended",
              "permission_error")
OVERLOAD_SIGNS = ("overloaded",)
RATE_SIGNS = ("rate_limit", "rate limit", "usage_limit_reached", "usage limit", "too many requests")
TIMEOUT_SIGNS = ("etimedout", "fetch failed", "timed out", "timeout", "econnreset", "socket hang up")
# Error types inside a stream that already answered 200, mapped to the status they stand for.
STREAM_ERROR_STATUS = {"overloaded_error": 529, "rate_limit_error": 429, "authentication_error": 401,
                       "permission_error": 403, "invalid_request_error": 400, "not_found_error": 404,
                       "request_too_large": 413, "api_error": 500, "server_error": 500, "timeout_error": 504}


def classify_error(status, text):
    """Error class for the ledger (DESIGN §4.7); None for a success.

    account_auth          401, 403, or an OAuth / account signal: a person has to re-authorize
    upstream_overloaded   529, overloaded
    upstream_rate_limited 429, rate / usage limit: the subscription's window is used up
    upstream_timeout      408, 502, 504, or a 5xx that says it timed out
    client_error          any other 4xx: the request itself
    upstream_error        any other 5xx
    """
    if status < 400:
        return None
    t = (text or "").lower()
    if status in (401, 403) or any(s in t for s in AUTH_SIGNS):
        return "account_auth"
    if status == 529 or any(s in t for s in OVERLOAD_SIGNS):
        return "upstream_overloaded"
    if status == 429 or any(s in t for s in RATE_SIGNS):
        return "upstream_rate_limited"
    if status in (408, 502, 504) or (status >= 500 and any(s in t for s in TIMEOUT_SIGNS)):
        return "upstream_timeout"
    if 400 <= status < 500:
        return "client_error"
    return "upstream_error"


def classify_stream_error(error):
    """Class of an error event that arrived after the response had started with a 200:
    error is the tracker's (type or code, message)."""
    kind, message = error
    return classify_error(STREAM_ERROR_STATUS.get(kind, 500), f"{kind} {message}")
