"""Incremental server-sent events parser: only splits lines and collects data fields."""


class SSEParser:
    def __init__(self):
        self.buf = b""
        self.event, self.data = None, []

    def feed(self, chunk):
        """Yield (event name, data string) for each complete event in the bytes seen so far."""
        self.buf += chunk
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line, self.buf = self.buf[:i], self.buf[i + 1:]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                if self.data:
                    yield self.event, "\n".join(self.data)
                self.event, self.data = None, []
                continue
            if line.startswith(b":"):
                continue
            name, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if name == b"event":
                self.event = value.decode("utf-8", "replace")
            elif name == b"data":
                self.data.append(value.decode("utf-8", "replace"))
        # A pathological line without newlines must not grow without bound.
        if len(self.buf) > 8 * 1024 * 1024:
            self.buf = b""

    def close(self):
        """Yield a final event that had no trailing blank line."""
        if self.buf:
            yield from self.feed(b"\n")
        if self.data:
            yield self.event, "\n".join(self.data)
        self.event, self.data = None, []
