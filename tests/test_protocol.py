import json
import unittest

from app import protocol as proto
from app.gate.sessions import request_session, session_tag
from app.protocol.sse import SSEParser


def sse(*events):
    out = b""
    for name, obj in events:
        if name:
            out += f"event: {name}\n".encode()
        out += b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"
    return out


ANTHROPIC_STREAM = sse(
    ("message_start", {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 12, "cache_read_input_tokens": 600000, "cache_creation_input_tokens": 3000,
        "cache_creation": {"ephemeral_5m_input_tokens": 3000, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 1}}}),
    ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 420}}),
    ("message_stop", {"type": "message_stop"}),
)


class ProtocolTestCase(unittest.TestCase):
    def test_sse_split_anywhere(self):
        data = ANTHROPIC_STREAM
        for size in (1, 7, 64, len(data)):
            p, events = SSEParser(), []
            for i in range(0, len(data), size):
                events += list(p.feed(data[i:i + size]))
            events += list(p.close())
            self.assertEqual([e for e, _ in events],
                             ["message_start", "content_block_delta", "message_delta", "message_stop"])

    def test_sse_crlf_and_comments(self):
        p = SSEParser()
        events = list(p.feed(b": ping\r\nevent: x\r\ndata: {\"a\":1}\r\n\r\n"))
        self.assertEqual(events, [("x", '{"a":1}')])

    def test_anthropic_input_ready_at_message_start(self):
        t = proto.TRACKERS[proto.ANTHROPIC](True)
        head, rest = ANTHROPIC_STREAM.split(b"event: content_block_delta", 1)
        t.feed(head)
        self.assertTrue(t.input_ready)
        self.assertFalse(t.complete)
        self.assertEqual(t.usage.cache_read, 600000)
        self.assertEqual(t.usage.cache_write_5m, 3000)
        t.feed(b"event: content_block_delta" + rest)
        t.close()
        self.assertTrue(t.complete)
        self.assertEqual(t.usage.output, 420)
        self.assertEqual(t.usage.input, 12)
        self.assertEqual(t.model, "claude-opus-5-5")

    def test_anthropic_1h_split(self):
        t = proto.TRACKERS[proto.ANTHROPIC](False)
        t.feed(json.dumps({"model": "m", "usage": {"input_tokens": 1, "cache_creation_input_tokens": 300,
                                                   "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                                                      "ephemeral_1h_input_tokens": 200},
                                                   "output_tokens": 5}}).encode())
        t.close()
        self.assertEqual((t.usage.cache_write_5m, t.usage.cache_write_1h, t.usage.output), (100, 200, 5))

    def test_responses(self):
        t = proto.TRACKERS[proto.OPENAI_RESPONSES](True)
        t.feed(sse((None, {"type": "response.created", "response": {"model": "gpt-6.1-sol"}}),
                   (None, {"type": "response.completed", "response": {"model": "gpt-6.1-sol", "usage": {
                       "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800},
                       "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}}}})))
        t.close()
        self.assertTrue(t.complete)
        self.assertEqual((t.usage.input, t.usage.cache_read, t.usage.output, t.usage.reasoning), (200, 800, 50, 30))

    def test_chat_with_and_without_usage(self):
        t = proto.TRACKERS[proto.OPENAI_CHAT](True)
        t.feed(sse((None, {"model": "gpt", "choices": [{"delta": {"content": "x"}}]}),
                   (None, {"model": "gpt", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7,
                                                                    "prompt_tokens_details": {"cached_tokens": 60}}}),
                   (None, b"[DONE]")))
        t.close()
        self.assertTrue(t.complete)
        self.assertEqual((t.usage.input, t.usage.cache_read, t.usage.output), (40, 60, 7))
        t = proto.TRACKERS[proto.OPENAI_CHAT](True)
        t.feed(sse((None, {"model": "gpt", "choices": [{"delta": {"content": "x"}}]}), (None, b"[DONE]")))
        t.close()
        self.assertFalse(t.complete)

    def test_read_fields_never_fails(self):
        self.assertEqual(proto.read_fields(b"not json"), {})
        f = proto.read_fields(json.dumps({"model": "m", "stream": True,
                                          "metadata": {"user_id": "user_x_account_y_session_1234abcd-ef"}}).encode())
        self.assertEqual((f["model"], f["stream"]), ("m", True))
        self.assertEqual(session_tag(f["user_id"]), "1234abcd-ef")
        self.assertEqual(session_tag(json.dumps({"session_id": "s-1"})), "s-1")

    def test_error_bodies_and_classes(self):
        self.assertEqual(proto.error_body(proto.ANTHROPIC, "x")["error"]["type"], "overloaded_error")
        self.assertEqual(proto.error_body(proto.OPENAI_CHAT, "x")["error"]["code"], "lingqu_throttled")
        self.assertEqual(proto.classify_error(401, ""), "account_auth")
        self.assertEqual(proto.classify_error(400, "OAuth access token has been revoked"), "account_auth")
        self.assertEqual(proto.classify_error(529, ""), "upstream_overloaded")
        self.assertEqual(proto.classify_error(429, ""), "upstream_rate_limited")
        self.assertEqual(proto.classify_error(500, "fetch failed"), "upstream_timeout")
        self.assertEqual(proto.classify_error(400, "bad"), "client_error")
        self.assertIsNone(proto.classify_error(200, ""))


class M3ProtocolTestCase(unittest.TestCase):
    def test_session_from_prompt_cache_key(self):
        f = proto.read_fields(json.dumps({"model": "gpt-6.1-sol", "prompt_cache_key": "conv-42"}).encode())
        self.assertEqual(request_session(f), "conv-42")
        f = proto.read_fields(json.dumps({"model": "m", "metadata": {"user_id": "user_x_session_abcd1234-ef"},
                                          "prompt_cache_key": "other"}).encode())
        self.assertEqual(request_session(f), "abcd1234-ef")         # Claude Code's own id wins
        self.assertEqual(request_session(proto.read_fields(b'{"prompt_cache_key": 5}')), "")

    def test_more_error_signals(self):
        c = proto.classify_error
        self.assertEqual(c(403, "Forbidden"), "account_auth")
        self.assertEqual(c(500, '{"error":"invalid_grant: refresh token expired"}'), "account_auth")
        self.assertEqual(c(400, '{"error":{"code":"refresh_token_reused"}}'), "account_auth")
        self.assertEqual(c(429, ""), "upstream_rate_limited")
        self.assertEqual(c(400, "usage_limit_reached"), "upstream_rate_limited")
        self.assertEqual(c(400, "timeout must be positive"), "client_error")   # a 4xx about a timeout field
        self.assertEqual(c(504, ""), "upstream_timeout")
        self.assertEqual(c(500, "socket hang up"), "upstream_timeout")
        self.assertEqual(c(500, "boom"), "upstream_error")

    def test_errors_inside_a_200_stream(self):
        t = proto.TRACKERS[proto.ANTHROPIC](True)
        t.feed(ANTHROPIC_STREAM.split(b"event: message_delta")[0])
        t.feed(sse(("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})))
        t.close()
        self.assertEqual(t.error, ("overloaded_error", "Overloaded"))
        self.assertEqual(proto.classify_stream_error(t.error), "upstream_overloaded")

        t = proto.TRACKERS[proto.OPENAI_RESPONSES](True)
        t.feed(sse(("response.failed", {"type": "response.failed", "response": {
            "model": "gpt", "error": {"code": "rate_limit_exceeded", "message": "slow down"}}})))
        self.assertEqual(proto.classify_stream_error(t.error), "upstream_rate_limited")
        t = proto.TRACKERS[proto.OPENAI_RESPONSES](True)
        t.feed(sse(("error", {"type": "error", "code": "server_error", "message": "x"})))
        self.assertEqual(proto.classify_stream_error(t.error), "upstream_error")

        t = proto.TRACKERS[proto.OPENAI_CHAT](True)
        t.feed(sse((None, {"error": {"type": "authentication_error", "message": "token has been revoked"}})))
        self.assertEqual(proto.classify_stream_error(t.error), "account_auth")

        t = proto.TRACKERS[proto.ANTHROPIC](True)
        t.feed(ANTHROPIC_STREAM)
        t.close()
        self.assertIsNone(t.error)
