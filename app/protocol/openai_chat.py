"""OpenAI /v1/chat/completions: usage arrives in the last chunk, and only when the client asked
for stream_options.include_usage; without it the output is estimated from the bytes sent."""
from .base import Tracker, num


def read_usage(u, usage):
    cached = num(u, "prompt_tokens_details", "cached_tokens")
    # Some converters (Claude behind an OpenAI surface) also report the cache write.
    written = num(u, "prompt_tokens_details", "cache_creation_tokens") or num(u, "cache_creation_input_tokens")
    usage.cache_read, usage.cache_write_5m = cached, written
    usage.input = max(0, num(u, "prompt_tokens") - cached - written)
    usage.output = num(u, "completion_tokens")
    usage.reasoning = num(u, "completion_tokens_details", "reasoning_tokens")


class ChatTracker(Tracker):
    def on_event(self, event, obj):
        self.model = obj.get("model") or self.model
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            read_usage(obj["usage"], self.usage)
            self.input_ready = self.complete = True

    def _final(self, obj):
        self.on_event(None, obj)
