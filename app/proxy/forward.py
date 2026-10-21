"""Upstream request building and raw streaming (DESIGN §5): the body goes through byte for byte,
only the credential header is swapped and hop-by-hop headers dropped."""
import socket

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade", "proxy-connection"}


def upstream_headers(headers, api_key):
    """Client headers for 9router, with the lq- key replaced by the 9router key in whichever of
    Authorization / x-api-key carried it."""
    out = []
    had_bearer = had_xkey = False
    for k, v in headers:
        name = k.lower()
        if name in HOP_BY_HOP or name in ("host", "content-length"):
            continue
        if name == "authorization":
            had_bearer = True
            continue
        if name == "x-api-key":
            had_xkey = True
            continue
        out.append((k, v))
    if had_xkey:
        out.append(("x-api-key", api_key))
    if had_bearer or not had_xkey:
        out.append(("Authorization", f"Bearer {api_key}"))
    return out


def response_headers(resp):
    return [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in HOP_BY_HOP]


def stream_body(resp):
    """Raw upstream bytes, closing the upstream response however the client side ends."""
    try:
        for chunk in resp.iter_raw():
            if chunk:
                yield chunk
    finally:
        resp.close()


def client_gone_probe(environ):
    """A callable that tells whether the client hung up, for a request whose body is fully read;
    None when the server gives no access to the socket (only gunicorn does)."""
    sock = environ.get("gunicorn.socket")
    if sock is None:
        return None

    def gone():
        try:
            return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
    return gone
