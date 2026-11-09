"""The proxy end to end: the app behind its port dispatcher, against a fake 9router."""
import json
import time
import unittest

from werkzeug.test import Client

from app import create_app, db
from app.ports import PortDispatcher
from tests.fake_router import FakeRouter, anthropic_stream, gzipped, sse_chunks
from app.settings import load as read_settings
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

    # ------------------------------------------------------------ M3: sessions, error classes, alerts

    def session_body(self, session):
        return {"model": "claude-opus-5-5", "stream": True,
                "metadata": {"user_id": f"user_x_account_y_session_{session}"}}

    def test_cache_state_follows_the_session(self):
        self.post("/v1/messages", self.session_body("aaaa1111-0001")).get_data()
        self.post("/v1/messages", self.session_body("aaaa1111-0001")).get_data()
        self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True}).get_data()
        rows = self.rows()
        self.assertEqual([r.cache_state for r in rows], ["cold", "warm", "none"])
        self.assertEqual(rows[0].session_tag, "aaaa1111-0001")

    def test_codex_session_from_prompt_cache_key(self):
        self.router.reply = lambda *a: (200, JSON, [b'{"model":"gpt-6.1-sol","usage":{"input_tokens":10,"output_tokens":2}}'], 0)
        self.post("/v1/responses", {"model": "gpt-6.1-sol", "prompt_cache_key": "conv-1"}, key="lq-a-codex")
        self.assertEqual(self.rows()[0].session_tag, "conv-1")

    def test_sessions_restored_from_ledger(self):
        self.post("/v1/messages", self.session_body("bbbb2222-0001")).get_data()
        self.rows()
        self.rt.gate.sessions.last.clear()
        from app.ledger import restore_counters
        restore_counters(self.app, self.rt.gate)
        self.assertLess(self.rt.gate.sessions.since("a/claude", "bbbb2222-0001", time.time()), 60)

    def test_error_inside_a_200_stream_is_classified(self):
        chunks = anthropic_stream()[:1] + sse_chunks(
            ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
        self.router.reply = lambda *a: (200, SSE, chunks, 0)
        r = self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True})
        self.assertEqual(r.status_code, 200)
        r.get_data()
        row = self.rows()[0]
        self.assertEqual((row.status, row.error_class), (200, "upstream_overloaded"))
        self.assertGreater(row.eusd, 0)                    # the input side was counted at message_start

    def test_account_auth_streak_alerts_and_recovers(self):
        body = b'{"type":"error","error":{"type":"authentication_error","message":"OAuth access token has been revoked"}}'
        self.router.reply = lambda *a: (401, JSON, [body], 0)
        send = lambda: self.post("/v1/messages", {"model": "claude-opus-5-5"}).get_data()
        send()
        kinds = lambda: [a["kind"] for a in self.rt.notifier.queue]
        self.assertNotIn("account_auth", kinds())             # one failure is not yet a streak
        self.router.reply = lambda *a: (400, JSON, [b'{"error":"bad request"}'], 0)
        send()                                                 # a client error neither breaks nor extends it
        self.router.reply = lambda *a: (401, JSON, [body], 0)
        send()
        self.assertIn("account_auth", kinds())
        e = self.rt.last_errors["a/claude"]
        self.assertEqual((e["class"], e["streak"], e["alerted"]), ("account_auth", 2, True))
        send()
        self.assertEqual(kinds().count("account_auth"), 1)     # once per streak
        self.router.reply = lambda *a: (200, SSE, anthropic_stream(), 0)
        self.post("/v1/messages", {"model": "claude-opus-5-5", "stream": True}).get_data()
        self.assertIn("account_auth_recovered", kinds())
        self.assertEqual(self.rt.last_errors["a/claude"]["streak"], 0)
        self.rt.ledger.flush()
        from app.models import GateEvent
        self.assertEqual([e.event for e in GateEvent.query.order_by(GateEvent.id)],
                         ["account_auth", "account_auth_recovered"])
        self.router.reply = lambda *a: (401, JSON, [body], 0)  # a new streak alerts again at once
        send()
        send()
        self.assertEqual(kinds().count("account_auth"), 2)

    def test_router_unreachable(self):
        self.router.close()
        up = self.rt.settings.upstreams["r1"]
        import socket
        with socket.socket() as sk:                         # a port nobody listens on
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        up.base_url = f"http://127.0.0.1:{port}"
        for _ in range(2):
            r = self.post("/v1/messages", {"model": "claude-opus-5-5"})
            self.assertEqual(r.status_code, 502)
        self.assertEqual(self.rows()[0].error_class, "router_unreachable")
        self.assertIn("router_unreachable", [a["kind"] for a in self.rt.notifier.queue])
        self.router = FakeRouter()                          # for tearDown

    def test_status_and_stats_m3_fields(self):
        self.post("/v1/messages", self.session_body("cccc3333-0001")).get_data()
        self.rows()
        st = self.proxy.get("/status", environ_overrides={"SERVER_PORT": "8091"}).get_json()
        pool = [p for p in st["pools"] if p["name"] == "a/claude"][0]
        self.assertEqual(pool["sessions"], {"aware": False, "warm": 1})
        self.assertEqual(pool["concurrency"]["peak_1h"], 1)
        self.assertEqual(pool["held_cold"], 0)
        stats = self.proxy.get("/stats?period=24h", environ_overrides={"SERVER_PORT": "8091"}).get_json()
        a = stats["pools"]["a/claude"]
        self.assertEqual((a["cold"]["count"], a["cold"]["eusd_share"]), (1, 1.0))
        self.assertEqual((a["concurrency_peak"], a["shadow_would_hold"]), (1, 0))
        page = self.proxy.get("/", environ_overrides={"SERVER_PORT": "8091"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("1 小时最多 1", page.get_data(as_text=True))


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

    def test_private_status_keeps_the_switch_page_public(self):
        s = load(BASE.replace("server: { admin_token: adm, fail_mode: open }",
                              "server: { admin_token: adm, fail_mode: open, private_status: true, "
                              "dashboards_url: 'http://example.com:3600/dashboards' }")
                 .replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.rt.settings = s  # same ports and upstreams; only the server options differ
        outside = {"SERVER_PORT": "8091", "REMOTE_ADDR": "203.0.113.5"}
        for path in ("/status", "/stats", "/events"):
            self.assertEqual(self.c.get(path, environ_overrides=outside).status_code, 401, path)
        self.assertEqual(self.c.get("/", environ_overrides=outside).status_code, 302)  # a browser: to the login page
        self.assertEqual(self.c.get("/", environ_overrides=outside, headers={"Authorization": "Bearer bad"}).status_code, 401)
        self.assertEqual(self.c.get("/status", environ_overrides=outside,
                                    headers={"Authorization": "Bearer adm"}).status_code, 200)
        self.assertEqual(self.c.get("/status", environ_overrides={"SERVER_PORT": "8091", "REMOTE_ADDR": "127.0.0.1"}).status_code, 200)
        for path in ("/healthz", "/dashboards"):
            self.assertEqual(self.c.get(path, environ_overrides=outside).status_code, 200, path)
        page = self.gate("/dashboard").get_data(as_text=True)
        self.assertIn("http://example.com:3600/dashboards", page)

    def test_switch_page_links_gates_under_the_configured_host(self):
        s = load(BASE.replace("server: { admin_token: adm, fail_mode: open }",
                              "server: { admin_token: adm, fail_mode: open, dashboard_gate_host: 203.0.113.9 }")
                 .replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.rt.settings = s
        self.admin("POST", "/admin/dashboards/r1/open", json={"for": "30m"})
        env = {"SERVER_PORT": "8091", "HTTP_HOST": "switch.example"}
        self.c.post("/dashboards", data={"action": "login", "token": "adm"}, environ_overrides=env)
        page = self.c.get("/dashboards", environ_overrides=env).get_data(as_text=True)
        self.assertIn("http://203.0.113.9:8101/dashboard", page)
        self.assertNotIn("switch.example:8101", page)

    def test_path_prefix_puts_links_and_assets_under_the_proxy_path(self):
        s = load(BASE.replace("server: { admin_token: adm, fail_mode: open }",
                              "server: { admin_token: adm, fail_mode: open, path_prefix: /lingqu, private_status: true }")
                 .replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.rt.settings = s
        local = {"SERVER_PORT": "8091", "REMOTE_ADDR": "127.0.0.1"}
        page = self.c.get("/", environ_overrides=local).get_data(as_text=True)
        self.assertIn('href="/lingqu/static/lingqu.css"', page)
        self.assertIn('href="/lingqu/"', page)
        self.assertIn('href="/lingqu/status"', page)
        css = self.c.get("/static/lingqu.css", environ_overrides=local)
        self.assertEqual(css.status_code, 200)
        css.close()
        r = self.c.get("/?period=7d", environ_overrides={"SERVER_PORT": "8091", "REMOTE_ADDR": "203.0.113.5"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/lingqu/console/login?next="), r.headers["Location"])
        self.assertIn("next=/lingqu/?period%3D7d", r.headers["Location"])
        self.assertEqual(self.c.get("/v1/models", environ_overrides=local).status_code, 404)
        login = self.c.post("/dashboards", data={"action": "login", "token": "adm"}, environ_overrides=local)
        self.assertEqual(login.headers["Location"], "/lingqu/dashboards")
        self.assertIn("Path=/lingqu", login.headers["Set-Cookie"])
        for bad in ("lingqu", "/lingqu/", "/a b", "//x"):
            _, errors, _ = read_settings(text=BASE.replace("server: { admin_token: adm, fail_mode: open }",
                                                           f"server: {{ admin_token: adm, path_prefix: '{bad}' }}"))
            self.assertTrue(any("path_prefix" in e for e in errors), bad)

    def test_navigation_only_with_links_and_overview_sends_browsers_to_login(self):
        env = {"SERVER_PORT": "8091", "REMOTE_ADDR": "127.0.0.1"}
        self.assertNotIn("lq-nav", self.c.get("/", environ_overrides=env).get_data(as_text=True))
        s = load(BASE.replace("server: { admin_token: adm, fail_mode: open }",
                              "server: { admin_token: adm, fail_mode: open, private_status: true, "
                              "links: [{name: 用量, url: /usage/}] }")
                 .replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url))
        self.rt.settings = s
        for path in ("/", "/dashboards", "/console/login"):
            page = self.c.get(path, environ_overrides=env).get_data(as_text=True)
            self.assertIn('href="/usage/"', page, path)
            self.assertIn('href="/console"', page, path)
            self.assertIn('href="/dashboards"', page, path)
        self.assertIn('aria-current="page">9router 仪表盘', self.c.get("/dashboards", environ_overrides=env).get_data(as_text=True))
        outside = {"SERVER_PORT": "8091", "REMOTE_ADDR": "203.0.113.5"}
        r = self.c.get("/?period=7d", environ_overrides=outside)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/console/login?next=", r.headers["Location"])
        self.assertEqual(self.c.get("/status", environ_overrides=outside).status_code, 401)
        _, errors, _ = read_settings(text=BASE.replace("server: { admin_token: adm, fail_mode: open }",
                                                       "server: { admin_token: adm, links: [{name: x, url: 'javascript:1'}] }"))
        self.assertTrue(any(e.startswith("server.links") for e in errors))

    def test_private_status_needs_an_admin_token(self):
        _, errors, _ = read_settings(text=BASE.replace("server: { admin_token: adm, fail_mode: open }",
                                                       "server: { fail_mode: open, private_status: true }"))
        self.assertIn("server.private_status needs server.admin_token", errors)

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
        self.assertEqual(page.count('class="card lq-pool"'), 2)
        self.assertEqual(page.count('data-pool-group="a"'), 1)
        self.assertIn('data-pool="a/claude"', page)
        self.assertIn('data-pool="a/codex"', page)
        self.assertIn('aria-label="Claude 流量"', page)
        self.assertIn('aria-label="Codex 流量"', page)
        self.assertIn('临时限额 $1.00', page)
        # Grouping the page must not merge the actual provider pools or their budgets.
        pools = self.c.get("/status", environ_overrides=env).get_json()["pools"]
        self.assertEqual(len(pools), 3)
        self.assertEqual(next(p for p in pools if p["name"] == "a/claude")["limit"], 1)
        self.assertIsNone(next(p for p in pools if p["name"] == "a/codex")["limit"])
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
