"""Data path on the proxy port (DESIGN §5): auth → parse → price → gate → forward → stream back
→ account → ledger."""
import json
import logging
import time
import uuid
import zlib

import httpx
from flask import Response, current_app, request
from werkzeug.wsgi import ClosingIterator

from .. import protocol as proto
from ..gate.sessions import request_session
from ..pricing.table import Usage, cost
from . import proxy
from .forward import client_gone_probe, response_headers, stream_body, upstream_headers

log = logging.getLogger("lingqu")
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
    matches = [s.pools[n] for n in binding.pools if s.pools[n].matches(model)]
    if matches:
        priority = max(p.priority for p in matches)
        winners = [p for p in matches if p.priority == priority]
        if len(winners) != 1:
            raise ValueError("model matches multiple equal-priority pools")
        return winners[0].name, winners[0].kind
    if binding.unmatched == "fallback":
        p = s.pools[binding.fallback_pool]
        return p.name, p.kind
    if binding.unmatched == "reject":
        raise ValueError("model matched no pool")
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
    path = "/v1/" + sub
    protocol = proto.protocol_of(path)
    if rt.settings.keys.get(lq_key()) is None:
        return plain_error(401, protocol, "lingqu: unknown key")
    slot, reason = rt.capacity.enter(request.content_length or 0)
    if slot is None:
        rt.capacity_rejected(reason)
        return plain_error(503, protocol, f"lingqu: busy, {reason}")
    try:
        out = forward(rt, path, protocol, slot)
    except BaseException:
        slot.close()
        raise
    if not isinstance(out.response, ClosingIterator):
        slot.close()
    return out


def forward(rt, path, protocol, slot):
    """The request after it got a capacity slot; a streamed response closes the slot when it ends."""
    s = rt.settings
    arrival = time.time()
    binding = s.keys.get(lq_key())
    upstream = s.upstreams.get(binding.upstream)
    if upstream is None:
        return plain_error(503, protocol, "lingqu: the key's upstream is not configured")

    body = request.get_data(cache=False)
    slot.grow(len(body))
    fields = proto.read_fields(body) if request.method == "POST" else {}
    model = fields.get("model", "")
    session = request_session(fields)
    metered = protocol is not None and request.method == "POST"
    try:
        pool, kind = resolve(rt, binding, model, protocol) if metered else (binding.pool or UNKNOWN_POOL, "codex")
    except ValueError as e:
        return plain_error(400, protocol, f"lingqu: {e}")
    if pool == UNKNOWN_POOL and protocol:
        rt.notifier.alert("unknown_pool", model or "-", f"model {model!r} matched no pool; metered as {UNKNOWN_POOL}")

    ticket = None
    if metered:
        ticket = rt.gate.admit(pool, model, gone=client_gone_probe(request.environ), session=session, user=binding.user)
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

    rt.quota.headers(pool, resp.headers)
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
    # With direct_passthrough the server gets this iterable itself, so Response.call_on_close
    # would never run: the close hook rides on the iterable. A generator closed before its first
    # chunk never runs its finally block; the hook covers that case.
    if not metered:
        return Response(ClosingIterator(stream_body(resp), [resp.close, slot.close]), status=resp.status_code,
                        headers=out_headers, direct_passthrough=True)
    row = base_row(rt, pool, protocol, path, fields, session, arrival, ticket)
    meter = Meter(rt, resp, pool, kind, model, protocol, row, sent_at, ticket, stream)
    return Response(ClosingIterator(meter.body(), [resp.close, meter.settle, slot.close]), status=resp.status_code,
                    headers=out_headers, direct_passthrough=True)


def base_row(rt, pool, protocol, path, fields, session, arrival, ticket):
    key = lq_key()
    return {"request_id": ticket.id if ticket else uuid.uuid4().hex,
            "ts": arrival, "pool": pool, "key_ref": key[-4:] if key else "", "protocol": protocol,
            "endpoint": path, "model_req": fields.get("model", ""), "stream": fields.get("stream", False),
            "session_tag": session, "user_tag": (fields.get("user") or "")[:128],
            "user_ref": (ticket.user or None) if ticket else None,
            "cache_state": (ticket.cache or None) if ticket else None,
            "held_ms": int(ticket.held * 1000) if ticket else 0, "gate_result": ticket.result if ticket else None}


class Meter:
    """One metered response, from its first upstream byte to its ledger row.

    Each chunk goes to the client first and is parsed after; a chunk already handed over when the
    client hangs up is parsed in settle(), so all usage read from upstream is kept however the
    response ends. settle() runs once: from the body's finally block, or from the response's close
    when the body never started. The input side is charged as soon as it is known, the rest at
    the end. A charge the gate cannot take (shared state down) neither breaks the stream nor
    skips the row: it is kept as pending and retried under the same key (Runtime.charge_failed).
    """

    def __init__(self, rt, resp, pool, kind, model, protocol, row, sent_at, ticket, stream):
        self.rt, self.resp, self.pool, self.kind, self.model = rt, resp, pool, kind, model
        self.row, self.sent_at, self.ticket = row, sent_at, ticket
        self.tracker = proto.TRACKERS[protocol](stream)
        self.decode = Decoder(resp.headers.get("content-encoding"))
        self.status = resp.status_code
        self.error_text = bytearray()
        self.sent_bytes = 0
        self.first_byte = None
        self.handed = None          # the chunk the client has, not parsed yet
        self.started = False
        self.client_closed = False
        self.parse_failed = False
        self.counted = 0.0          # eUSD of the input side, observed at message_start
        self.counted_at = None
        self.pending = 0.0          # observed eUSD the gate has not taken
        self.settled = False
        self.price, self.price_ref = rt.pricing.price(model, kind)

    def body(self):
        self.started = True
        try:
            for chunk in self.resp.iter_raw():
                if not chunk:
                    continue
                if self.first_byte is None:
                    self.first_byte = time.time()
                self.handed = chunk
                try:
                    yield chunk
                except GeneratorExit:
                    self.client_closed = True
                    raise
                self.take()
        finally:
            self.resp.close()
            self.settle()

    def take(self):
        """Account for the chunk handed to the client."""
        chunk, self.handed = self.handed, None
        if chunk is None:
            return
        self.sent_bytes += len(chunk)
        plain = self.decode(chunk)
        if self.status >= 400:
            if len(self.error_text) < ERROR_SNIPPET:
                self.error_text += plain[:ERROR_SNIPPET - len(self.error_text)]
            return
        if self.parse_failed:
            return
        try:
            self.tracker.feed(plain)
        except Exception as e:  # a parser bug must not cut the stream; the row says estimated
            self.parse_failed = True
            log.warning("pool %s: parsing the response failed: %r", self.pool, e)
            return
        t = self.tracker
        if t.input_ready and self.counted_at is None:
            if t.model and t.model != self.model:
                self.price, self.price_ref = self.rt.pricing.price(t.model, self.kind)
            self.counted = cost(self.price, t.usage, input_only=True)
            self.counted_at = self.charge(self.counted, 1)

    def charge(self, amount, seq):
        """Give amount to the gate under "<request id>:<seq>"; returns the charge time. The input
        side is also written as a meter event, so recovery has it while the request still runs."""
        user = self.ticket.user if self.ticket else ""
        event = {"request_id": self.row["request_id"], "seq": seq, "pool": self.pool, "model": self.model,
                 "user_ref": user or None, "amount": amount}
        try:
            at = self.rt.gate.charge(self.pool, self.model, amount, user=user, key=f"{event['request_id']}:{seq}")
        except Exception as e:
            at = time.time()
            self.pending += amount
            self.rt.charge_failed(dict(event, ts=at), e)
        if seq == 1:
            self.rt.ledger.add_meter(dict(event, ts=at))
        return at

    def settle(self):
        if self.settled:
            return
        self.settled = True
        if not self.started:
            self.client_closed = True
        row, t, status = self.row, self.tracker, self.status
        end = time.time()
        error_class, detail = None, ""
        try:
            self.take()
            if status < 400 and not self.parse_failed:
                t.close()
            if t.model and t.model != self.model and self.counted_at is None:
                self.price, self.price_ref = self.rt.pricing.price(t.model, self.kind)
            usage = t.usage
            if t.complete:
                source = "upstream"
            elif status < 400:
                source = "estimated"
                if not t.input_ready:
                    usage = Usage()
                usage.output = max(usage.output, self.sent_bytes // BYTES_PER_TOKEN)
            else:
                source = "none" if not t.input_ready else "upstream"
            total = cost(self.price, usage) if status < 400 or t.input_ready else 0.0
            accounted_at = end
            if total > self.counted:
                accounted_at = self.charge(total - self.counted, 2)
            detail = bytes(self.error_text).decode("utf-8", "replace")
            error_class = proto.classify_error(status, detail)
            if error_class is None and t.error:
                error_class = proto.classify_stream_error(t.error)
                detail = " ".join(t.error)
            if error_class is None and not t.complete and status < 400:
                if self.client_closed:
                    error_class = "client_closed"
                elif self.sent_bytes == 0:
                    error_class = "upstream_timeout"
            u = usage
            row.update(model_actual=t.model or self.model, status=status, error_class=error_class,
                       input_tokens=u.input, cache_read_tokens=u.cache_read, cache_write_5m_tokens=u.cache_write_5m,
                       cache_write_1h_tokens=u.cache_write_1h, output_tokens=u.output, reasoning_tokens=u.reasoning,
                       usage_source=source, eusd=max(total, self.counted), eusd_input=self.counted,
                       counted_at=self.counted_at, accounted_at=accounted_at, price_ref=self.price_ref)
        except Exception as e:  # never lose the row: what was counted so far stays on it
            log.exception("pool %s: settling a metered response failed", self.pool)
            row.update(status=status, error_class=error_class or "metering_error", usage_source="none",
                       eusd=self.counted, eusd_input=self.counted, counted_at=self.counted_at, price_ref=self.price_ref)
            detail = repr(e)
        finally:
            row.update(eusd_pending=self.pending,
                       ttfb_ms=int((self.first_byte - self.sent_at) * 1000) if self.first_byte else None,
                       duration_ms=int((end - self.sent_at) * 1000))
            self.rt.ledger.add(row)
            self.rt.metered(row)
            self.rt.upstream_result(self.pool, error_class, detail)
            self.rt.gate.finish(self.ticket)
