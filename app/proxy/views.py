"""Data path on the proxy port (DESIGN §5): auth → parse → price → gate → forward → stream back
→ account → ledger."""
import json
import time
import zlib

import httpx
from flask import Response, current_app, request

from .. import protocol as proto
from ..gate.sessions import request_session
from ..pricing.table import Usage, cost
from . import proxy
from .forward import client_gone_probe, response_headers, stream_body, upstream_headers

BYTES_PER_TOKEN = 4
ERROR_SNIPPET = 4096
UNKNOWN_POOL = "unknown"


def lq_key():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def plain_error(status, protocol, message):
    body = proto.error_body(protocol or proto.OPENAI_CHAT, message)
    return Response(json.dumps(body), status=status, content_type="application/json")


def resolve(rt, binding, model, protocol):
    """(pool name, kind) for a request: the key's pool, or by model name when the key spans pools."""
    s = rt.settings
    if binding.pool:
        p = s.pools[binding.pool]
        return p.name, p.kind
    for name in binding.pools:
        if s.pools[name].matches(model):
            return name, s.pools[name].kind
    return UNKNOWN_POOL, "claude" if protocol == proto.ANTHROPIC else "codex"


class Decoder:
    """A decompressed copy of the response for the parser; the client gets the original bytes."""

    def __init__(self, encoding):
        enc = (encoding or "").lower().strip()
        self.d = None
        self.ok = enc in ("", "identity", "gzip", "x-gzip", "deflate")
        if enc in ("gzip", "x-gzip"):
            self.d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            self.d = zlib.decompressobj()

    def __call__(self, chunk):
        if not self.ok:
            return b""
        if self.d is None:
            return chunk
        try:
            return self.d.decompress(chunk)
        except zlib.error:
            self.ok = False
            return b""


@proxy.route("/v1/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def relay(sub):
    rt = current_app.extensions["lingqu"]
    s = rt.settings
    arrival = time.time()
    path = "/v1/" + sub
    protocol = proto.protocol_of(path)
    binding = s.keys.get(lq_key())
    if binding is None:
        return plain_error(401, protocol, "lingqu: unknown key")
    upstream = s.upstreams.get(binding.upstream)
    if upstream is None:
        return plain_error(503, protocol, "lingqu: the key's upstream is not configured")

    body = request.get_data(cache=False)
    fields = proto.read_fields(body) if request.method == "POST" else {}
    model = fields.get("model", "")
    session = request_session(fields)
    pool, kind = resolve(rt, binding, model, protocol)
    if pool == UNKNOWN_POOL and protocol:
        rt.notifier.alert("unknown_pool", model or "-", f"model {model!r} matched no pool; metered as {UNKNOWN_POOL}")
    metered = protocol is not None and request.method == "POST"

    ticket = None
    if metered:
        ticket = rt.gate.admit(pool, model, gone=client_gone_probe(request.environ), session=session)
        if ticket.rejected:
            row = base_row(rt, pool, protocol, path, fields, session, arrival, ticket)
            row.update(status=503, error_class="throttled", usage_source="none", duration_ms=0)
            rt.ledger.add(row)
            msg = f"lingqu: {ticket.message}"
            return plain_error(503, protocol, msg)

    client = rt.client(upstream.name)
    url = upstream.base_url + path + (("?" + request.query_string.decode()) if request.query_string else "")
    headers = upstream_headers(request.headers.items(), upstream.api_key)
    sent_at = time.time()
    try:
        req = client.build_request(request.method, url, headers=headers, content=body)
        resp = client.send(req, stream=True)
    except httpx.HTTPError as e:
        # Not connecting at all is Lingqu's side (9router down, network policy); a timeout after
        # connecting is 9router waiting on its upstream.
        error_class = "router_unreachable" if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) \
            else "upstream_timeout"
        if ticket:
            rt.gate.finish(ticket)
            row = base_row(rt, pool, protocol, path, fields, session, arrival, ticket)
            row.update(status=502, error_class=error_class, usage_source="none",
                       duration_ms=int((time.time() - sent_at) * 1000))
            rt.ledger.add(row)
            rt.upstream_result(pool, error_class, f"{upstream.name}: {e}")
        return plain_error(502, protocol, f"lingqu: upstream {upstream.name} unreachable: {e}")

    out_headers = response_headers(resp)
    if ticket:
        out_headers += [("X-Lingqu-Pool", pool), ("X-Lingqu-Held-Ms", str(int(ticket.held * 1000))),
                        ("X-Lingqu-Ratio", f"{ticket.ratio:.3f}")]
    stream = "text/event-stream" in resp.headers.get("content-type", "")
    if stream:
        names = {k.lower() for k, _ in out_headers}
        if "cache-control" not in names:
            out_headers.append(("Cache-Control", "no-cache"))
        out_headers.append(("X-Accel-Buffering", "no"))
    if not metered:
        return Response(stream_body(resp), status=resp.status_code, headers=out_headers, direct_passthrough=True)

    row = base_row(rt, pool, protocol, path, fields, session, arrival, ticket)
    gen = metered_body(rt, resp, pool, kind, model, protocol, row, sent_at, ticket, stream)
    out = Response(gen, status=resp.status_code, headers=out_headers, direct_passthrough=True)

    @out.call_on_close
    def release():
        # A generator closed before its first chunk never runs its finally block.
        resp.close()
        rt.gate.finish(ticket)
    return out


def base_row(rt, pool, protocol, path, fields, session, arrival, ticket):
    key = lq_key()
    return {"ts": arrival, "pool": pool, "key_ref": key[-4:] if key else "", "protocol": protocol,
            "endpoint": path, "model_req": fields.get("model", ""), "stream": fields.get("stream", False),
            "session_tag": session, "user_tag": (fields.get("user") or "")[:128],
            "cache_state": (ticket.cache or None) if ticket else None,
            "held_ms": int(ticket.held * 1000) if ticket else 0, "gate_result": ticket.result if ticket else None}


def metered_body(rt, resp, pool, kind, model, protocol, row, sent_at, ticket, stream):
    """Write each chunk to the client first, then parse a copy; count the input side as soon as
    it is known and the rest when the response ends, however it ends."""
    tracker = proto.TRACKERS[protocol](stream)
    decode = Decoder(resp.headers.get("content-encoding"))
    status = resp.status_code
    error_text = bytearray()
    sent_bytes = 0
    counted = 0.0
    counted_at = None
    first_byte = None
    price, price_ref = rt.pricing.price(model, kind)
    try:
        for chunk in resp.iter_raw():
            if not chunk:
                continue
            if first_byte is None:
                first_byte = time.time()
            if status >= 400:
                # Keep the error text before passing the chunk on: a client that hangs up on an
                # error must not take its class (and the alert's detail) with it.
                if len(error_text) < ERROR_SNIPPET:
                    error_text += decode(chunk)[:ERROR_SNIPPET - len(error_text)]
                yield chunk
                sent_bytes += len(chunk)
                continue
            yield chunk
            sent_bytes += len(chunk)
            plain = decode(chunk)
            tracker.feed(plain)
            if tracker.input_ready and counted_at is None:
                if tracker.model and tracker.model != model:
                    price, price_ref = rt.pricing.price(tracker.model, kind)
                counted = cost(price, tracker.usage, input_only=True)
                counted_at = time.time()
                rt.gate.charge(pool, tracker.model or model, counted)
    finally:
        resp.close()
        end = time.time()
        try:
            if status < 400:
                tracker.close()
            if tracker.model and tracker.model != model and counted_at is None:
                price, price_ref = rt.pricing.price(tracker.model, kind)
            usage = tracker.usage
            if tracker.complete:
                source = "upstream"
            elif status < 400:
                source = "estimated"
                if not tracker.input_ready:
                    usage = Usage()
                usage.output = max(usage.output, sent_bytes // BYTES_PER_TOKEN)
            else:
                source = "none" if not tracker.input_ready else "upstream"
            total = cost(price, usage) if status < 400 or tracker.input_ready else 0.0
            if total > counted:
                rt.gate.charge(pool, tracker.model or model, total - counted)
            detail = bytes(error_text).decode("utf-8", "replace")
            error_class = proto.classify_error(status, detail)
            if error_class is None and tracker.error:
                error_class = proto.classify_stream_error(tracker.error)
                detail = " ".join(tracker.error)
            if error_class is None and not tracker.complete and status < 400 and sent_bytes == 0:
                error_class = "upstream_timeout"
            u = usage
            row.update(model_actual=tracker.model or model, status=status, error_class=error_class,
                       input_tokens=u.input, cache_read_tokens=u.cache_read, cache_write_5m_tokens=u.cache_write_5m,
                       cache_write_1h_tokens=u.cache_write_1h, output_tokens=u.output, reasoning_tokens=u.reasoning,
                       usage_source=source, eusd=max(total, counted), eusd_input=counted, counted_at=counted_at,
                       price_ref=price_ref, ttfb_ms=int((first_byte - sent_at) * 1000) if first_byte else None,
                       duration_ms=int((end - sent_at) * 1000))
            rt.ledger.add(row)
            rt.upstream_result(pool, error_class, detail)
        finally:
            rt.gate.finish(ticket)
