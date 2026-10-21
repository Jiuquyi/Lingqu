"""The gate against a fake clock. Waiting threads are driven by advancing the clock and
notifying the pool's Condition, so nothing here sleeps for real beyond a few milliseconds."""
import threading
import time
import unittest

from app.gate.hold import Gate
from app.gate.window import Window
from tests.helpers import FakeClock, load


class WindowTestCase(unittest.TestCase):
    def test_rolls_by_minute(self):
        w = Window()
        w.add(0, 5.0)
        w.add(1800, 10.0)
        self.assertEqual(w.total(1800), 15.0)
        self.assertEqual(w.total(3599), 15.0)
        self.assertEqual(w.total(3600), 10.0)
        self.assertEqual(w.total(1800 + 3600), 0.0)

    def test_clear_at(self):
        w = Window()
        w.add(0, 15.0)
        w.add(600, 10.0)
        self.assertEqual(w.clear_at(700, 20), 3600)
        self.assertIsNone(w.clear_at(700, 30))


class GateHarness:
    def __init__(self, text=None, **replace):
        self.clock = FakeClock()
        self.events = []
        s = load(**replace) if text is None else load(text)
        self.gate = Gate(s, clock=self.clock, on_event=lambda **kw: self.events.append(kw))

    def spend(self, pool, amount):
        self.gate.charge(pool, "", amount)

    def start(self, pool="a/claude", model="claude-opus-5-5"):
        """admit() in a thread; returns a dict that gets the ticket."""
        box = {}

        def run():
            box["t"] = self.gate.admit(pool, model)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        box["thread"] = th
        return box

    def settle(self):
        time.sleep(0.03)

    def advance(self, dt, pool="a/claude"):
        self.clock.advance(dt)
        with self.gate.lock:
            self.gate.state(pool).cond.notify_all()
        self.settle()


class GateTestCase(unittest.TestCase):
    def test_open_under_limit(self):
        h = GateHarness()
        h.spend("a/claude", 10)
        t = h.gate.admit("a/claude", "claude-opus-5-5")
        self.assertEqual(t.result, "pass")
        self.assertAlmostEqual(t.ratio, 0.5)

    def test_unlimited_pool(self):
        h = GateHarness()
        h.spend("a/codex", 1000)
        self.assertEqual(h.gate.admit("a/codex", "gpt").result, "pass")

    def test_fifo_gap_over_limit(self):
        h = GateHarness()
        h.spend("a/claude", 21)                     # ratio 1.05 -> gap 15 s
        first = h.gate.admit("a/claude", "m")      # nobody departed yet: goes at once
        self.assertEqual(first.result, "pass")
        h.gate.finish(first)
        box = h.start()
        h.settle()
        self.assertNotIn("t", box)                  # held: 15 s gap since the first departure
        h.advance(10)
        self.assertNotIn("t", box)
        h.advance(5.5)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "held")
        self.assertAlmostEqual(box["t"].held, 15.5, places=1)
        self.assertTrue(any(e["event"] == "throttle_start" for e in h.events))

    def test_gap_grows_with_ratio(self):
        h = GateHarness()
        h.spend("a/claude", 41)                     # ratio 2.05 -> gap 45 s
        h.gate.finish(h.gate.admit("a/claude", "m"))
        box = h.start()
        h.advance(40)
        self.assertNotIn("t", box)
        h.advance(6)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "held")

    def test_fifo_order(self):
        h = GateHarness()
        h.spend("a/claude", 21)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        a = h.start()
        h.settle()
        b = h.start()
        h.settle()
        h.advance(15.5)
        a["thread"].join(1)
        self.assertIn("t", a)
        self.assertNotIn("t", b)                    # b waits another gap after a left
        h.advance(15.5)
        b["thread"].join(1)
        self.assertIn("t", b)

    def test_head_timeout_lets_through(self):
        h = GateHarness()
        h.spend("a/claude", 61)                     # ratio 3.05 -> gap 60 s
        h.gate.finish(h.gate.admit("a/claude", "m"))
        a = h.start()
        h.settle()
        b = h.start()                               # second in line: would wait 120 s
        h.settle()
        h.advance(61)
        a["thread"].join(1)
        self.assertNotIn("t", b)
        h.advance(60)                               # b has waited 121 s: hold_cap_cached
        b["thread"].join(1)
        self.assertEqual(b["t"].result, "released_by_cap")

    def test_max_hold_rejects_when_cap_is_longer(self):
        h = GateHarness(**{"hold_cap_cached: 120s": "hold_cap_cached: 200s"})
        h.spend("a/claude", 61)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        h.start()                                   # first in line, leaves after one 60 s gap
        h.settle()
        b = h.start()
        h.settle()
        for _ in range(4):
            h.advance(40)
        b["thread"].join(1)
        self.assertTrue(b["t"].rejected)
        self.assertIn("waited", b["t"].message)

    def test_max_queue(self):
        h = GateHarness(**{"max_queue: 8": "max_queue: 1"})
        h.spend("a/claude", 21)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        h.start()
        h.settle()
        t = h.gate.admit("a/claude", "m")
        self.assertTrue(t.rejected)
        h.gate.shutdown()

    def test_reject_rule(self):
        h = GateHarness(**{"on_exceed: hold, release: fifo": "on_exceed: reject"})
        h.spend("a/claude", 21)
        self.assertTrue(h.gate.admit("a/claude", "m").rejected)

    def test_recovery_releases_queue(self):
        h = GateHarness()
        h.spend("a/claude", 21)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        box = h.start()
        h.settle()
        h.gate.counters.reset("a/claude")           # spend dropped below the limit
        h.advance(0.1)
        box["thread"].join(1)
        self.assertIn("t", box)
        h.gate.tick()
        self.assertTrue(any(e["event"] == "throttle_end" for e in h.events))

    def test_override_lift_and_limit(self):
        h = GateHarness()
        h.spend("a/claude", 25)
        h.gate.set_override("a/claude", "lift", None, h.clock() + 600)
        self.assertEqual(h.gate.admit("a/claude", "m").result, "pass")
        self.assertEqual(h.gate.pool_status("a/claude")["state"], "override")
        h.gate.set_override("a/claude", "limit", 30, h.clock() + 600)
        self.assertAlmostEqual(h.gate.pool_status("a/claude")["ratio"], 25 / 30, places=3)
        h.clock.advance(601)
        self.assertAlmostEqual(h.gate.pool_status("a/claude")["ratio"], 25 / 20, places=3)

    def test_phase_release(self):
        h = GateHarness(**{"release: fifo": "release: phase"})
        h.spend("a/claude", 21)                     # step 1.0: off 45 s, on 60 s
        box = h.start()
        h.settle()
        self.assertNotIn("t", box)
        h.advance(46)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "held")
        self.assertEqual(h.gate.admit("a/claude", "m").result, "pass")  # inside the on phase

    def test_shutdown_rejects_held(self):
        h = GateHarness()
        h.spend("a/claude", 21)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        box = h.start()
        h.settle()
        h.gate.shutdown()
        box["thread"].join(1)
        self.assertTrue(box["t"].rejected)

    def test_client_gone_leaves_queue(self):
        h = GateHarness()
        h.spend("a/claude", 21)
        h.gate.finish(h.gate.admit("a/claude", "m"))
        gone = threading.Event()
        box = {}
        th = threading.Thread(target=lambda: box.update(t=h.gate.admit("a/claude", "m", gone=gone.is_set)),
                              daemon=True)
        th.start()
        h.settle()
        gone.set()
        h.advance(1)
        th.join(1)
        self.assertTrue(box["t"].rejected)
        self.assertEqual(h.gate.pool_status("a/claude")["held"], 0)

    def test_concurrency(self):
        h = GateHarness(**{"keys: [lq-a-codex], rules: []": "keys: [lq-a-codex], rules: [{ kind: concurrency, max: 1 }]"})
        first = h.gate.admit("a/codex", "gpt")
        box = h.start(pool="a/codex", model="gpt")
        h.settle()
        self.assertNotIn("t", box)
        h.gate.finish(first)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "held")

    def test_shadow_concurrency(self):
        h = GateHarness(**{"keys: [lq-a-codex], rules: []":
                           "keys: [lq-a-codex], rules: [{ kind: concurrency, max: 1, mode: shadow }]"})
        h.gate.admit("a/codex", "gpt")
        t = h.gate.admit("a/codex", "gpt")
        self.assertEqual(t.result, "shadow_would_hold")

    def test_model_scope(self):
        h = GateHarness(**{"rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]":
                           "rules: [{ kind: hourly_cap, limit: 20, on_exceed: reject }, "
                           "{ kind: hourly_cap, limit: 5, on_exceed: reject, scope: { model: 'claude-opus-*' } }]"})
        h.gate.charge("a/claude", "claude-opus-5-5", 6)
        self.assertTrue(h.gate.admit("a/claude", "claude-opus-5-5").rejected)
        self.assertEqual(h.gate.admit("a/claude", "claude-sonnet-5").result, "pass")

    def test_gate_error_fails_open(self):
        h = GateHarness()

        def boom(*a, **kw):
            raise RuntimeError("bug")
        h.gate.decide = boom
        t = h.gate.admit("a/claude", "m")
        self.assertEqual(t.result, "gate_error")
        self.assertFalse(t.rejected)
        self.assertEqual(h.gate.errors_1h(), 1)
