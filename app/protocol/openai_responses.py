"""OpenAI /v1/responses: the usage is in response.completed (or .incomplete / .failed)."""
from .base import Tracker, error_of, num

FINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")


def read_usage(u, usage):
    cached = num(u, "input_tokens_details", "cached_tokens")
    usage.cache_read = cached
    usage.input = max(0, num(u, "input_tokens") - cached)
    usage.output = num(u, "output_tokens")
    usage.reasoning = num(u, "output_tokens_details", "reasoning_tokens")


class ResponsesTracker(Tracker):
    def on_event(self, event, obj):
        kind = obj.get("type") or event
        resp = obj.get("response") if isinstance(obj.get("response"), dict) else None
        if resp and resp.get("model"):
            self.model = resp["model"]
        if kind in FINAL_EVENTS and resp and isinstance(resp.get("usage"), dict):
            read_usage(resp["usage"], self.usage)
            self.input_ready = self.complete = True
        if kind == "response.failed" and resp:
            self.error = error_of(resp) or ("server_error", "response.failed")
        elif kind == "error":
            self.error = error_of(obj) or (str(obj.get("code") or "error"), str(obj.get("message") or "")[:500])

    def _final(self, obj):
        self.model = obj.get("model") or self.model
        if isinstance(obj.get("usage"), dict):
            read_usage(obj["usage"], self.usage)
            self.input_ready = self.complete = True
