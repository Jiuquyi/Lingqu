"""Anthropic /v1/messages: message_start carries the input side, message_delta the running output."""
from .base import Tracker, num


def read_usage(u, usage):
    """Anthropic usage fields into a Usage; input_tokens already excludes the cached part."""
    if not isinstance(u, dict):
        return
    if "input_tokens" in u:
        usage.input = num(u, "input_tokens")
    if "cache_read_input_tokens" in u:
        usage.cache_read = num(u, "cache_read_input_tokens")
    split = u.get("cache_creation")
    if isinstance(split, dict) and ("ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split):
        usage.cache_write_5m = num(split, "ephemeral_5m_input_tokens")
        usage.cache_write_1h = num(split, "ephemeral_1h_input_tokens")
    elif "cache_creation_input_tokens" in u:
        usage.cache_write_5m, usage.cache_write_1h = num(u, "cache_creation_input_tokens"), 0
    if "output_tokens" in u:
        usage.output = num(u, "output_tokens")


class AnthropicTracker(Tracker):
    def on_event(self, event, obj):
        kind = obj.get("type") or event
        if kind == "message_start":
            msg = obj.get("message") or {}
            self.model = msg.get("model") or self.model
            read_usage(msg.get("usage"), self.usage)
            self.input_ready = True
        elif kind == "message_delta":
            # output_tokens is cumulative; with server tools the input fields may be updated too.
            u = obj.get("usage") or {}
            read_usage({k: v for k, v in u.items() if v is not None and (v or k == "output_tokens")}, self.usage)
        elif kind == "message_stop":
            self.complete = True

    def _final(self, obj):
        self.model = obj.get("model") or self.model
        if isinstance(obj.get("usage"), dict):
            read_usage(obj["usage"], self.usage)
            self.input_ready = self.complete = True
