"""The proxy end to end: the app behind its port dispatcher, against a fake 9router."""
import json
import time
import unittest

from werkzeug.test import Client

from app import create_app, db
from app.ports import PortDispatcher
from tests.fake_router import FakeRouter, anthropic_stream, gzipped, sse_chunks
from tests.helpers import BASE, load

SSE = [("Content-Type", "text/event-stream")]
JSON = [("Content-Type", "application/json")]


class ProxyTestCase(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()
        self.router.reply = lambda path, headers, body: (200, SSE, anthropic_stream(), 0)
        s = load(BASE.replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.app = create_app("testing", settings=s)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]
        self.disp = PortDispatcher(self.app)
        self.proxy = Client(self.disp)
        self.env = {"SERVER_PORT": "8090"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.router.close()

    def post(self, path, body, key="lq-a-claude", headers=None, port="8090"):
        h = {"x-api-key": key, "anthropic-version": "2023-06-01", **(headers or {})}
        return self.proxy.post(path, data=json.dumps(body) if not isinstance(body, bytes) else body,
                               headers=h, content_type="application/json",
                               environ_overrides={"SERVER_PORT": port})

    def rows(self):
        self.rt.ledger.flush()
        from app.models import Ledger
        return Ledger.query.order_by(Ledger.id).all()

    def test_unknown_key(self):
        r = self.post("/v1/messages", {"model": "m"}, key="nope")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.router.requests, [])

    def test_passthrough_byte_for_byte_and_key_swap(self):
        raw = b'{"model":"claude-opus-5-5","stream":true,  "metadata":{"user_id":"u_session_abcdef12-3"},"x":[1, 2]}'
        r = self.post("/v1/messages?beta=true", raw, headers={"anthropic-beta": "oauth-2025",
                                                              "User-Agent": "claude-cli/2.1"})
        body = r.get_data()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body, b"".join(anthropic_stream()))
        req = self.router.requests[0]
        self.assertEqual(req["body"], raw)
        self.assertEqual(req["path"], "/v1/messages?beta=true")
        self.assertEqual(req["headers"]["x-api-key"], "k1")
        self.assertEqual(req["headers"]["anthropic-beta"], "oauth-2025")
        self.assertEqual(req["headers"]["user-agent"], "claude-cli/2.1")
        self.assertNotIn("lq-a-claude", json.dumps(req["headers"]))
        self.assertEqual(r.headers["X-Lingqu-Pool"], "a/claude")

    def test_bearer_key_swap(self):
        self.router.reply = lambda *a: (200, JSON, [b'{"model":"gpt-6.1-sol","usage":{"input_tokens":10,"output_tokens":2}}'], 0)
        self.proxy.post("/v1/responses", data=b'{"model":"gpt-6.1-sol"}', content_type="application/json",
                        headers={"Authorization": "Bearer lq-a-codex"}, environ_overrides=self.env)
        h = self.router.requests[0]["headers"]
        self.assertEqual(h["authorization"], "Bearer k1")
        self.assertNotIn("x-api-key", h)

    def test_metering_anthropic(self):
        self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True,
                                   "metadata": {"user_id": "user_1_account_2_session_aaaa1111-22"}}).get_data()
        row = self.rows()[0]
        expect_in = (10 * 5 + 600_000 * 0.5) / 1e6
        expect = expect_in + 500 * 25 / 1e6
        self.assertAlmostEqual(row.eusd, expect, places=6)
        self.assertAlmostEqual(row.eusd_input, expect_in, places=6)
        self.assertEqual(row.usage_source, "upstream")
        self.assertEqual(row.session_tag, "aaaa1111-22")
        self.assertEqual(row.gate_result, "pass")
        self.assertAlmostEqual(self.rt.gate.counters.spent("a/claude", "*", time.time()), expect, places=6)

    def test_input_counted_before_stream_ends(self):
        chunks = anthropic_stream(cache_read=6_000_000)  # $3 of cache reads
        self.router.reply = lambda *a: (200, SSE, chunks, 0.05)
        r = self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True})
        it = iter(r.response)
        self.assertIn(b"message_start", next(it))
        # Each chunk goes to the client before it is parsed: message_start is counted while the
        # proxy waits for the next chunk, long before the stream ends.
        self.assertNotIn(b"message_stop", next(it))
        self.assertGreaterEqual(self.rt.gate.counters.spent("a/claude", "*", time.time()), 3.0)
        r.close()

    def test_gzip_response_still_metered(self):
        self.router.reply = lambda *a: (200, SSE + [("Content-Encoding", "gzip")], gzipped(anthropic_stream()), 0)
        r = self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True})
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        r.get_data()
        self.assertEqual(self.rows()[0].usage_source, "upstream")

    def test_chat_without_usage_estimates(self):
        self.router.reply = lambda *a: (200, SSE, sse_chunks((None, {"model": "gpt-6.1-sol", "choices": [
            {"delta": {"content": "x" * 400}}]})), 0)
        self.proxy.post("/v1/chat/completions", data=b'{"model":"gpt-6.1-sol","stream":true}',
                        content_type="application/json", headers={"Authorization": "Bearer lq-a-codex"},
                        environ_overrides=self.env).get_data()
        row = self.rows()[0]
        self.assertEqual(row.usage_source, "estimated")
        self.assertGreater(row.output_tokens, 90)
        self.assertGreater(row.eusd, 0)

    def test_upstream_error_passthrough_and_class(self):
        body = b'{"type":"error","error":{"type":"authentication_error","message":"OAuth access token has been revoked"}}'
        self.router.reply = lambda *a: (401, JSON, [body], 0)
        r = self.post("/v1/messages", {"model": "claude-opus-5-5"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_data(), body)
        row = self.rows()[0]
        self.assertEqual(row.error_class, "account_auth")
        self.assertEqual(row.eusd, 0)
        self.assertEqual(self.rt.last_errors["a/claude"]["class"], "account_auth")

    def test_count_tokens_not_metered(self):
        self.router.reply = lambda *a: (200, JSON, [b'{"input_tokens": 5}'], 0)
        r = self.post("/v1/messages/count_tokens", {"model": "claude-opus-5-5"})
        self.assertEqual(r.get_json(), {"input_tokens": 5})
        self.assertEqual(self.rows(), [])

    def test_models_passthrough(self):
        self.router.reply = lambda *a: (200, JSON, [b'{"data":[{"id":"m"}]}'], 0)
        r = self.proxy.get("/v1/models", headers={"Authorization": "Bearer lq-a-claude"}, environ_overrides=self.env)
        self.assertEqual(r.get_json(), {"data": [{"id": "m"}]})

    def test_throttled_reject_body_follows_protocol(self):
        self.rt.settings.pools["a/claude"].rules[0].on_exceed = "reject"
        self.rt.gate.charge("a/claude", "", 25)
        r = self.post("/v1/messages", {"model": "claude-opus-5-5"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["error"]["type"], "overloaded_error")
        self.assertIn("spent $25.00 of $20", r.get_json()["error"]["message"])
        self.assertEqual(self.router.requests, [])
        self.assertEqual(self.rows()[0].gate_result, "rejected")

    def test_ports_are_separate(self):
        r = self.proxy.get("/status", environ_overrides={"SERVER_PORT": "8090"})
        self.assertEqual(r.status_code, 404)
        r = self.post("/v1/messages", {"model": "m"}, port="8091")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.router.requests, [])
        r = self.proxy.get("/status", environ_overrides={"SERVER_PORT": "8091"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pools"][0]["name"], "a/claude")

    def test_status_and_stats(self):
        self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True}).get_data()
        self.rows()
        st = self.proxy.get("/status", environ_overrides={"SERVER_PORT": "8091"}).get_json()
        pool = [p for p in st["pools"] if p["name"] == "a/claude"][0]
        self.assertGreater(pool["spent_1h"], 0)
        self.assertEqual(pool["state"], "open")
        stats = self.proxy.get("/stats?period=24h", environ_overrides={"SERVER_PORT": "8091"}).get_json()
        self.assertEqual(stats["pools"]["a/claude"]["requests"], 1)
        self.assertGreater(stats["pools"]["a/claude"]["cache_hit_rate"], 0.99)

    def test_unpriced_model_uses_default(self):
        self.router.reply = lambda *a: (200, SSE, anthropic_stream(model="claude-new-9", cache_read=0, inp=1_000_000, out=0), 0)
        self.post("/v1/messages", {"model": "claude-new-9", "stream": True}).get_data()
        row = self.rows()[0]
        self.assertAlmostEqual(row.eusd, 5.0, places=4)
        self.assertEqual(row.price_ref, "default:claude")
        self.assertIn("claude-new-9", self.rt.pricing.unpriced)

    def test_admin_override_needs_token(self):
        env = {"SERVER_PORT": "8091"}
        r = self.proxy.post("/admin/pools/a/claude/override", json={"action": "lift", "until": time.time() + 60},
                            environ_overrides=env)
        self.assertEqual(r.status_code, 401)
        r = self.proxy.post("/admin/pools/a/claude/override", json={"action": "lift", "until": time.time() + 60},
                            headers={"Authorization": "Bearer adm"}, environ_overrides=env)
        self.assertEqual(r.get_json()["state"], "override")

    def test_restore_counters_from_ledger(self):
        self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True}).get_data()
        self.rows()
        spent = self.rt.gate.counters.spent("a/claude", "*", time.time())
        self.rt.gate.counters.reset("a/claude")
        from app.ledger import restore_counters
        restore_counters(self.app, self.rt.gate)
        self.assertAlmostEqual(self.rt.gate.counters.spent("a/claude", "*", time.time()), spent, places=6)


class DashboardGateTestCase(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()
        self.router.reply = lambda path, headers, body: (200, [("Content-Type", "text/html"),
                                                               ("Set-Cookie", "auth_token=x; Path=/")], [b"<html>dash</html>"], 0)
        s = load(BASE.replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.app = create_app("testing", settings=s)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]
        self.rt.exposure.load()
        self.c = Client(PortDispatcher(self.app))

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.router.close()

    def gate(self, path, port="8101", **kw):
        return self.c.get(path, environ_overrides={"SERVER_PORT": port}, **kw)

    def admin(self, method, path, token="adm", **kw):
        return self.c.open(path, method=method, headers={"Authorization": f"Bearer {token}"},
                           environ_overrides={"SERVER_PORT": "8091"}, **kw)

    def test_closed_by_default(self):
        r = self.gate("/dashboard")
        self.assertEqual(r.status_code, 503)
        self.assertIn("已关闭", r.get_data(as_text=True))
        self.assertEqual(self.router.requests, [])

    def test_open_forwards_dashboard_never_llm(self):
        self.assertEqual(self.admin("POST", "/admin/dashboards/r1/open", json={"for": "30m"}).status_code, 200)
        self.c.set_cookie("auth_token", "t")
        r = self.gate("/dashboard/providers?x=1", headers={"X-Forwarded-For": "127.0.0.1",
                                                           "x-9r-peer-token": "guess"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), b"<html>dash</html>")
        self.assertIn("auth_token=x", r.headers.get("Set-Cookie"))
        sent = self.router.requests[0]["headers"]
        self.assertNotIn("x-forwarded-for", sent)
        self.assertNotIn("x-9r-peer-token", sent)
        self.assertEqual(sent["cookie"], "auth_token=t")
        for path in ("/v1/models", "/v1/messages", "/api/v1/chat", "/codex/x", "/responses", "/v1beta/m"):
            self.assertEqual(self.gate(path).status_code, 404, path)
        self.assertEqual(len(self.router.requests), 1)
        self.assertEqual(self.gate("/dashboard", port="8102").status_code, 503)  # r2 still closed

    def test_temporary_expires(self):
        clock = [time.time()]
        self.rt.exposure.clock = lambda: clock[0]
        self.admin("POST", "/admin/dashboards/r1/open", json={"for": "10m"})
        self.assertEqual(self.gate("/dashboard").status_code, 200)
        clock[0] += 601
        self.assertEqual(self.gate("/dashboard").status_code, 503)
        self.rt.exposure.expire()
        from app.models import ExposureEvent
        self.assertEqual([e.action for e in ExposureEvent.query.order_by(ExposureEvent.id)], ["open", "expire"])

    def test_ttl_capped(self):
        st = self.admin("POST", "/admin/dashboards/r1/open", json={"for": "48h"}).get_json()
        self.assertLessEqual(st["until"] - time.time(), 4 * 3600 + 1)

    def test_opener_scope_and_persistent_rule(self):
        self.assertEqual(self.admin("GET", "/admin/dashboards", token="op1").get_json()[0]["upstream"], "r1")
        self.assertEqual(len(self.admin("GET", "/admin/dashboards", token="op1").get_json()), 1)
        self.assertEqual(self.admin("POST", "/admin/dashboards/r2/open", token="op1").status_code, 403)
        self.assertEqual(self.admin("POST", "/admin/dashboards/r1/open", token="bad").status_code, 401)
        r = self.admin("POST", "/admin/dashboards/r2/open", json={"persistent": True})
        self.assertEqual(r.status_code, 400)
        r = self.admin("POST", "/admin/dashboards/r1/open", token="op1", json={"persistent": True})
        self.assertEqual(r.get_json()["state"], "persistent")
        self.assertEqual(r.get_json()["opened_by"], "team-a")

    def test_page_login_and_open(self):
        env = {"SERVER_PORT": "8091"}
        r = self.c.get("/dashboards", environ_overrides=env)
        self.assertIn("登录", r.get_data(as_text=True))
        r = self.c.post("/dashboards", data={"action": "login", "token": "op1"}, environ_overrides=env)
        self.assertEqual(r.status_code, 303)
        r = self.c.post("/dashboards", data={"action": "open", "upstream": "r1"}, environ_overrides=env)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.rt.exposure.is_open("r1"))
        page = self.c.get("/dashboards", environ_overrides=env).get_data(as_text=True)
        self.assertIn("临时打开", page)
        self.assertNotIn("r2", page)

    def test_overview_page_is_read_only(self):
        env = {"SERVER_PORT": "8091"}
        self.rt.gate.set_override("a/claude", "limit", 1, time.time() + 60)
        self.rt.gate.charge("a/claude", "claude-opus-5-5", 2.0)
        self.rt.ledger.flush()  # events reach the database through the ledger writer
        r = self.c.get("/", environ_overrides=env)
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("a/claude", page)
        self.assertIn("排队中", page)
        self.assertIn("管理员覆盖", page)
        self.assertNotIn("<form", page)
        self.assertEqual(self.c.get("/?period=7d", environ_overrides=env).status_code, 200)
        # Only on the status port: the proxy port serves /v1/* alone.
        self.assertEqual(self.c.get("/", environ_overrides={"SERVER_PORT": "8090"}).status_code, 404)

    def test_state_survives_restart(self):
        self.admin("POST", "/admin/dashboards/r1/open", json={"persistent": True})
        from app.dashboard.exposure import Exposure
        fresh = Exposure(self.app, self.rt.settings)
        fresh.load()
        self.assertTrue(fresh.is_open("r1"))
        self.assertFalse(fresh.is_open("r2"))
