"""A fake 9router on a random local port: records each request and answers from a script."""
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeRouter:
    def __init__(self):
        self.requests = []
        self.reply = None   # callable(path, headers, body) -> (status, headers, chunks, delay)
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                router.requests.append({"method": self.command, "path": self.path,
                                        "headers": {k.lower(): v for k, v in self.headers.items()},
                                        "body": body})
                status, headers, chunks, delay = router.reply(self.path, self.headers, body)
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for c in chunks:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
                        self.wfile.flush()
                        if delay:
                            time.sleep(delay)
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sse_chunks(*events):
    out = []
    for name, obj in events:
        out.append((f"event: {name}\n" if name else "").encode() + b"data: " + json.dumps(obj).encode() + b"\n\n")
    return out


def anthropic_stream(model="claude-opus-5-5", cache_read=600_000, cache_write=0, inp=10, out=500):
    return sse_chunks(
        ("message_start", {"type": "message_start", "message": {"model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write, "output_tokens": 1}}}),
        ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": out}}),
        ("message_stop", {"type": "message_stop"}),
    )


def gzipped(chunks):
    return [gzip.compress(b"".join(chunks))]
