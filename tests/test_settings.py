import os
import unittest

from app import settings as settings_mod
from tests.helpers import BASE, load


class SettingsTestCase(unittest.TestCase):
    def test_base_loads(self):
        s = load()
        self.assertEqual(set(s.pools), {"a/claude", "a/codex", "b/claude"})
        self.assertEqual(s.keys["lq-a-claude"].pool, "a/claude")
        self.assertEqual(s.dashboard_ports(), [8101, 8102])
        self.assertEqual(s.fifo[0], (1.0, 15.0))
        self.assertEqual(s.upstreams["r1"].dashboard.openers, [("team-a", "op1")])

    def test_env_expansion_and_missing(self):
        os.environ["LQ_TEST_KEY"] = "secret"
        s, errors, _ = settings_mod.load(text=BASE.replace("api_key: k1", 'api_key: "${LQ_TEST_KEY}"'))
        self.assertEqual(errors, [])
        self.assertEqual(s.upstreams["r1"].api_key, "secret")
        _, errors, _ = settings_mod.load(text=BASE.replace("api_key: k1", 'api_key: "${LQ_NOT_SET_X}"'))
        self.assertTrue(any("LQ_NOT_SET_X" in e for e in errors))

    def test_duplicate_key_rejected(self):
        _, errors, _ = settings_mod.load(text=BASE.replace("keys: [lq-a-codex]", "keys: [lq-a-claude]"))
        self.assertTrue(any("already bound" in e for e in errors))

    def test_fifo_must_start_at_one_and_increase(self):
        bad = BASE + "fifo:\n  - { at: 1.2, gap: 15 }\n"
        _, errors, _ = settings_mod.load(text=bad)
        self.assertTrue(any("at = 1.0" in e for e in errors))
        bad = BASE + "fifo:\n  - { at: 1.0, gap: 15 }\n  - { at: 1.0, gap: 20 }\n"
        _, errors, _ = settings_mod.load(text=bad)
        self.assertTrue(any("increase" in e for e in errors))

    def test_long_gap_warns(self):
        _, errors, warnings = settings_mod.load(text=BASE + "fifo:\n  - { at: 1.0, gap: 40 }\n")
        self.assertEqual(errors, [])
        self.assertTrue(any("1.5 x" in w for w in warnings))

    def test_port_clash(self):
        _, errors, _ = settings_mod.load(text=BASE.replace("port: 8102", "port: 8101"))
        self.assertTrue(any("already used" in e for e in errors))
        _, errors, _ = settings_mod.load(text=BASE.replace("port: 8102", "port: 8091"))
        self.assertTrue(any("clashes" in e for e in errors))

    def test_unknown_upstream_and_rule(self):
        _, errors, _ = settings_mod.load(text=BASE.replace("upstream: r2", "upstream: nope"))
        self.assertTrue(any("nope" in e for e in errors))
        _, errors, _ = settings_mod.load(text=BASE.replace("kind: hourly_cap", "kind: weird"))
        self.assertTrue(any("weird" in e for e in errors))

    def test_dynamic_budget_needs_cycle_limit(self):
        _, errors, warnings = settings_mod.load(text=BASE.replace(
            "keys: [lq-a-codex], rules: []", "keys: [lq-a-codex], rules: [{ kind: hourly_budget }]"))
        self.assertTrue(any("cycle_limit" in e for e in errors))
        self.assertFalse(any("ignored" in w for w in warnings))

    def test_model_keys(self):
        text = BASE.replace("keys: [lq-a-codex], rules: []",
                            "keys: [], match: { models: ['gpt-*'] }, rules: []") + \
            "keys:\n  - { key: lq-shared, upstream_pools: [a/codex] }\n"
        s, errors, _ = settings_mod.load(text=text)
        self.assertEqual(errors, [])
        self.assertEqual(s.keys["lq-shared"].pools, ["a/codex"])

    def test_durations(self):
        self.assertEqual(settings_mod.parse_duration("10m"), 600)
        self.assertEqual(settings_mod.parse_duration("4h"), 14400)
        self.assertEqual(settings_mod.parse_duration(15), 15)
        with self.assertRaises(ValueError):
            settings_mod.parse_duration("soon")

    def test_session_aware(self):
        s, errors, _ = settings_mod.load(text=BASE.replace("release: fifo }", "release: fifo, session_aware: true }"))
        self.assertEqual(errors, [])
        self.assertTrue(s.pools["a/claude"].rules[0].session_aware)
        self.assertEqual((s.cache_ttl, s.hold_cap_cold), (300, 300))
        _, errors, _ = settings_mod.load(text=BASE.replace("release: fifo }", "release: phase, session_aware: true }"))
        self.assertTrue(any("session_aware applies only" in e for e in errors))
        _, errors, _ = settings_mod.load(text=BASE.replace(
            "rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]",
            "rules: [{ kind: hourly_cap, limit: 20, session_aware: true }, "
            "{ kind: hourly_cap, limit: 5, scope: { model: 'claude-opus-*' } }]"))
        self.assertTrue(any("all of its enforcing hourly_cap" in e for e in errors))
        _, errors, _ = settings_mod.load(text=BASE.replace("hold_cap_cached: 120s }", "hold_cap_cached: 120s, cache_ttl: 10s }"))
        self.assertTrue(any("cache_ttl" in e for e in errors))
        _, errors, warnings = settings_mod.load(text=BASE.replace("hold_cap_cached: 120s }",
                                                                  "hold_cap_cached: 120s, hold_cap_cold: 60s }"))
        self.assertEqual(errors, [])
        self.assertTrue(any("hold_cap_cold is shorter" in w for w in warnings))

    def test_auth_streak(self):
        self.assertEqual(load().auth_streak, 2)
        s, errors, _ = settings_mod.load(text=BASE + "notify: { auth_streak: 3 }\n")
        self.assertEqual((errors, s.auth_streak), ([], 3))
