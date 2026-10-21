import json

from ..pricing.table import Usage
from .sse import SSEParser


class Tracker:
    """Watches one response and collects its usage.

    `input_ready` turns true as soon as the input side is known (Anthropic message_start), so
    the proxy can count it before the response ends; `complete` when the final usage arrived.
    `error` is (type or code, message) of an error the upstream reported inside a response
    that had already started with a 200.
    """

    def __init__(self, stream):
        self.stream = stream
        self.usage = Usage()
        self.model = ""
        self.input_ready = False
        self.complete = False
        self.error = None
        self.sse = SSEParser() if stream else None
        self.body = bytearray()

    def feed(self, chunk):
        if self.stream:
            for event, data in self.sse.feed(chunk):
                self._event(event, data)
        elif len(self.body) < 64 * 1024 * 1024:
            self.body += chunk

    def close(self):
        if self.stream:
            for event, data in self.sse.close():
                self._event(event, data)
        else:
            try:
                obj = json.loads(bytes(self.body))
            except (ValueError, UnicodeDecodeError):
                return
            if isinstance(obj, dict):
                self._final(obj)

    def _event(self, event, data):
        if data.strip() == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        if isinstance(obj, dict):
            self.on_event(event, obj)

    def on_event(self, event, obj):
        raise NotImplementedError

    def _final(self, obj):
        raise NotImplementedError


def error_of(obj):
    """(type or code, message) from an {"error": {...}} or {"error": "text"} object, else None."""
    e = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(e, dict):
        return str(e.get("type") or e.get("code") or "error"), str(e.get("message") or "")[:500]
    if isinstance(e, str) and e:
        return "error", e[:500]
    return None


def num(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return 0
        d = d.get(p)
    return int(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else 0
