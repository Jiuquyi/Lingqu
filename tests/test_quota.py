import json
import unittest
from unittest.mock import patch

import httpx

from app import create_app, db
from app.gate.hold import Gate
from app.ledger import restore_counters, save_counters
from app.models import BudgetSnapshot, Ledger, UpstreamQuota
from app.pricing.ninerouter import Session
from app.quota import from_headers, normalize
from tests.fake_router import FakeRouter, anthropic_stream
from tests.helpers import BASE, FakeClock, load
from tests.test_budget import BUDGET, MODEL, OLD, POOL


class QuotaParsingTestCase(unittest.TestCase):
    def test_real_claude_and_codex_response_shapes(self):
        body = {"quotas": {"session (5h)": {"used": 91, "total": 100, "resetAt": "2026-10-21T12:00:00Z"},
                "weekly (7d)": {"remainingPercentage": 20}, "weekly opus (7d)": {"used": 5, "total": 10},
                "review_weekly": {"used": 100, "total": 100}}}
        q = normalize(body)
        self.assertEqual({w: v["used_pct"] for w, v in q.items()}, {"short": 91, "weekly": 80, "weekly_opus": 50})
        self.assertIsNotNone(q["short"]["reset_at"])
        self.assertEqual(normalize({"quotas": {"session": {"used": 0, "total": 100}}})["short"]["used_pct"], 0)

    def test_invalid_or_soft_failure_is_never_treated_as_zero(self):
        for body in ({"message": "Unable to fetch usage"}, {"quotas": {}}, [],
                     {"quotas": {"weekly": {"used": "nan", "total": 100}}},
                     {"quotas": {"weekly": {"used": 0, "total": 0}}},
                     {"quotas": {"weekly": {"used": 10, "total": 100, "resetAt": "broken"}}}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                normalize(body)

    def test_response_header_units(self):
        q = from_headers({"Anthropic-Ratelimit-Unified-5h-Utilization": "0.98",
                          "Anthropic-Ratelimit-Unified-5h-Reset": "1800000000"}, "claude")
        self.assertEqual(q["short"], {"used_pct": 98, "reset_at": 1800000000})
        self.assertEqual(from_headers({"x-codex-secondary-used-percent": "90"}, "codex")["weekly"]["used_pct"], 90)
        self.assertEqual(from_headers({"x-codex-primary-used-percent": "NaN"}, "codex"), {})

    def test_login_failure_and_network_failure_cool_down(self):
        clock = FakeClock()
        s = Session("http://router", "password", clock)
        with patch("app.pricing.ninerouter.httpx.post", side_effect=httpx.ConnectError("offline")) as post:
            with self.assertRaises(httpx.ConnectError):
                s.get("/api/providers")
            with self.assertRaisesRegex(RuntimeError, "waiting"):
                s.get("/api/providers")
            self.assertEqual(post.call_count, 1)


class QuotaIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()
        text = BASE.replace("http://r1:20128", self.router.url).replace(OLD, BUDGET).replace(
            "api_key: k1", "api_key: k1\n    quota: {password: secret, refresh: 3m, stale_after: 6m}")
        self.app = create_app("testing", settings=load(text))
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]
        self.client = self.app.test_client()
        self.percent = 50
        self.fail_usage = False
        self.fail_login = False
        self.extra_connections = []
        self.header_percent = None
        self.router.reply = self.reply

    def tearDown(self):
        for client in self.rt.clients.values():
            client.close()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.router.close()

    def reply(self, path, headers, body):
        hs = [("Content-Type", "application/json")]
        if path == "/api/auth/login":
            return (401 if self.fail_login else 200), hs + [("Set-Cookie", "auth_token=token; Path=/")], [b"{}"], 0
        if path == "/api/providers":
            connections = [{"id": "c1", "provider": "claude", "authType": "oauth", "isActive": True}] + self.extra_connections
            obj = {"connections": connections}
        elif path.startswith("/api/usage/"):
            if self.fail_usage:
                obj = {"message": "Unable to fetch usage"}
            else:
                obj = {"quotas": {"session (5h)": {"used": self.percent, "total": 100,
                                                 "resetAt": self.rt.clock() + 18000},
                                  "weekly (7d)": {"used": self.percent, "total": 100,
                                                  "resetAt": self.rt.clock() + 604800}}}
        elif path == "/v1/messages":
            extra = [] if self.header_percent is None else [("anthropic-ratelimit-unified-5h-utilization", str(self.header_percent))]
            return 200, [("Content-Type", "text/event-stream")] + extra, anthropic_stream(), 0
        else:
            obj = {}
        return 200, hs, [json.dumps(obj).encode()], 0

    def test_api_cookie_reused_snapshot_and_status(self):
        errors = self.rt.quota.sync()
        self.assertNotIn(POOL, errors)
        self.assertIn("a/codex", errors)  # no matching account must be visible
        self.rt.quota.sync()
        logins = [r for r in self.router.requests if r["path"] == "/api/auth/login"]
        self.assertEqual(len(logins), 1)
        self.rt.ledger.flush()
        self.assertEqual(UpstreamQuota.query.filter_by(pool=POOL).count(), 4)
        self.assertGreater(BudgetSnapshot.query.count(), 0)
        st = self.client.get("/status").get_json()["pools"][0]
        self.assertFalse(st["quota"]["stale"])
        self.assertIsNotNone(st["budget"])
        self.assertNotIn("secret", json.dumps(st))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_soft_failure_keeps_quota_and_brake_marks_stale(self):
        self.percent = 99
        self.rt.quota.sync()
        self.fail_usage = True
        self.rt.quota.sync()
        st = self.rt.quota.status(POOL, self.rt.clock())
        self.assertTrue(st["stale"])
        self.assertTrue(st["last_error"])
        self.assertTrue(self.rt.gate.admit(POOL, MODEL).rejected)

    def test_response_headers_stop_next_request_and_do_not_change_stream(self):
        self.rt.quota.sync()
        self.header_percent = .99
        self.rt.gate.set_override(POOL, "lift", None, self.rt.clock() + 600)
        response = self.client.post("/v1/messages", json={"model": MODEL, "stream": True}, headers={"x-api-key": "lq-a-claude"})
        self.assertEqual(response.data, b"".join(anthropic_stream()))
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/v1/messages", json={"model": MODEL}, headers={"x-api-key": "lq-a-claude"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(sum(r["path"] == "/v1/messages" for r in self.router.requests), 1)

    def test_multiple_accounts_require_explicit_binding(self):
        self.extra_connections = [{"id": "c2", "provider": "claude", "authType": "oauth"}]
        errors = self.rt.quota.sync()
        self.assertIn("set pool.connections", errors[POOL])
        self.rt.settings.pools[POOL].connections = ["c1", "c2"]
        self.assertNotIn(POOL, self.rt.quota.sync())
        before = list(self.rt.gate.budgets.quotas.values())
        self.rt.quota.headers(POOL, {"anthropic-ratelimit-unified-5h-utilization": ".99"})
        self.assertEqual(list(self.rt.gate.budgets.quotas.values()), before)

    def test_manual_sync_requires_admin(self):
        self.assertEqual(self.client.post("/admin/quota/sync").status_code, 401)
        self.assertEqual(self.client.post("/admin/quota/sync", headers={"Authorization": "Bearer adm"}).status_code, 502)

    def test_restart_keeps_brake_and_cycle_spend(self):
        clock = FakeClock()
        self.rt.clock = self.rt.gate.clock = clock
        self.rt.gate.charge(POOL, MODEL, 5, clock() - 7200)
        db.session.add(Ledger(ts=clock() - 7200, pool=POOL, model_req=MODEL, eusd=5, duration_ms=0))
        db.session.commit()
        save_counters(self.app, self.rt.gate.counters, clock)
        # Input belongs to the checkpoint; output arrives after it.
        db.session.add(Ledger(ts=clock() - 10, counted_at=clock() - 10, pool=POOL, model_req=MODEL,
                              eusd=5, eusd_input=2, duration_ms=20000))
        self.rt.gate.charge(POOL, MODEL, 2, clock() - 10)
        save_counters(self.app, self.rt.gate.counters, clock)
        clock.advance(10)
        db.session.commit()
        self.percent = 99
        self.rt.quota.sync()
        self.rt.ledger.flush()
        g = Gate(self.rt.settings, clock=clock)
        restore_counters(self.app, g, clock)
        self.assertAlmostEqual(g.counters.total_since(POOL, None, clock() - 604800, clock()), 10)
        self.assertAlmostEqual(g.counters.spent(POOL, "*", clock()), 5)
        self.rt.gate = g
        self.rt.quota.load()
        self.assertTrue(g.admit(POOL, MODEL).rejected)

    def test_periodic_sync_respects_refresh(self):
        self.rt.quota.tick()
        count = len(self.router.requests)
        self.rt.quota.tick()
        self.assertEqual(len(self.router.requests), count)

    def test_pricing_and_quota_share_session_across_source_change(self):
        existing = self.rt.quota.sessions["r1"]
        self.rt.settings.pricing.update(source="ninerouter", ninerouter={"upstream": "r1", "password": "secret"})
        self.rt.pricing.configure(self.rt.settings)
        self.rt.quota.configure()
        self.assertIs(self.rt.pricing.session, existing)

    def test_exact_accounting_time_is_used_at_checkpoint(self):
        clock = FakeClock()
        self.rt.clock = self.rt.gate.clock = clock
        self.rt.gate.charge(POOL, MODEL, 1)
        save_counters(self.app, self.rt.gate.counters, clock)
        # Arrival and rounded duration precede the checkpoint, but actual accounting follows it.
        db.session.add(Ledger(ts=clock() - 1, pool=POOL, model_req=MODEL, eusd=2,
                              duration_ms=999, accounted_at=clock() + .001))
        db.session.commit()
        clock.advance(1)
        g = Gate(self.rt.settings, clock=clock)
        restore_counters(self.app, g, clock)
        self.assertEqual(g.counters.total_since(POOL, None, clock() - 604800, clock()), 3)
