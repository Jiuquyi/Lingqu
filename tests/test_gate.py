"""The gate against a fake clock. Waiting threads are driven by advancing the clock and
notifying the pool's Condition, so nothing here sleeps for real beyond a few milliseconds."""
import threading
import time
import unittest

from app.gate.hold import Gate, Ticket
from app.gate.limits import Counts
from app.gate.sessions import SessionClock
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


AWARE = {"rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]":
         "rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo, session_aware: true }]"}


class SessionClockTestCase(unittest.TestCase):
    def test_classify(self):
        c = SessionClock()
        self.assertEqual(c.classify("p", "", 100, 300), ("none", None))
        self.assertEqual(c.classify("p", "s", 100, 300), ("cold", None))
        c.started("p", "s", 100)
        self.assertEqual(c.classify("p", "s", 160, 300), ("warm", 60))
        self.assertEqual(c.classify("p", "s", 401, 300)[0], "cold")
        self.assertEqual(c.classify("q", "s", 160, 300)[0], "cold")     # caches are per pool
        c.started("p", "s", 50)                                          # an older start never wins
        self.assertEqual(c.since("p", "s", 160), 60)
        self.assertEqual(c.active("p", 160, 300), 1)

    def test_bounded(self):
        c = SessionClock(keep=2)
        for i, s in enumerate("abc"):
            c.started("p", s, i)
        self.assertIsNone(c.since("p", "a", 10))
        self.assertEqual(c.since("p", "c", 10), 8)


class SessionAwareGateTestCase(unittest.TestCase):
    def start(self, h, session, model="claude-opus-5-5"):
        box = {}
        th = threading.Thread(target=lambda: box.update(t=h.gate.admit("a/claude", model, session=session)),
                              daemon=True)
        th.start()
        box["thread"] = th
        h.settle()
        return box

    def over_limit(self, h, spend=21):
        h.spend("a/claude", spend)
        h.gate.finish(h.gate.admit("a/claude", "m"))     # a departure: the gap counts from here

    def test_warm_goes_before_an_earlier_cold(self):
        h = GateHarness(**AWARE)
        h.gate.sessions.started("a/claude", "warm-s", h.clock() - 30)
        self.over_limit(h)                               # gap 15 s
        cold = self.start(h, "new-s")
        warm = self.start(h, "warm-s")
        h.advance(15.5)
        warm["thread"].join(1)
        self.assertIn("t", warm)
        self.assertNotIn("t", cold)
        self.assertEqual((warm["t"].cache, cold.get("t")), ("warm", None))
        h.advance(15.5)
        cold["thread"].join(1)
        self.assertEqual(cold["t"].result, "held")
        self.assertEqual(cold["t"].cache, "cold")

    def test_plain_fifo_keeps_arrival_order(self):
        h = GateHarness()
        h.gate.sessions.started("a/claude", "warm-s", h.clock() - 30)
        self.over_limit(h)
        cold = self.start(h, "new-s")
        warm = self.start(h, "warm-s")
        h.advance(15.5)
        cold["thread"].join(1)
        self.assertIn("t", cold)
        self.assertNotIn("t", warm)
        h.gate.shutdown()

    def test_warm_leaves_before_its_cache_expires(self):
        h = GateHarness(**AWARE)
        self.over_limit(h, spend=61)                     # gap 60 s
        h.gate.sessions.started("a/claude", "s", h.clock() - 270)
        box = self.start(h, "s")                         # cache expires in 30 s: cap 300 - 15 - 270 = 15 s
        h.advance(10)
        self.assertNotIn("t", box)
        h.advance(5.5)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "released_by_cap")
        self.assertAlmostEqual(box["t"].held, 15.5, places=1)

    def test_cold_waits_longer_and_is_never_turned_away(self):
        slow = {"defaults: { max_hold: 150s, max_queue: 8, hold_cap_cached: 120s }":
                "defaults: { max_hold: 150s, max_queue: 8, hold_cap_cached: 120s }\nfifo: [{ at: 1.0, gap: 200 }]"}
        for aware, held in ((False, 120), (True, 150)):  # cold cap: min(hold_cap_cold 300, max_hold 150)
            h = GateHarness(**(dict(slow, **AWARE) if aware else slow))
            self.over_limit(h)
            cold = self.start(h, "new-s")
            for _ in range(11):
                h.advance(10)
            self.assertNotIn("t", cold)
            for _ in range(5):
                h.advance(10)
            cold["thread"].join(1)
            self.assertEqual(cold["t"].result, "released_by_cap")
            self.assertAlmostEqual(cold["t"].held, held, delta=10)

    def test_session_recorded_on_departure_only(self):
        h = GateHarness(**{"on_exceed: hold, release: fifo": "on_exceed: reject"})
        h.gate.admit("a/claude", "m", session="s1")
        self.assertEqual(h.gate.sessions.since("a/claude", "s1", h.clock()), 0)
        h.spend("a/claude", 25)
        self.assertTrue(h.gate.admit("a/claude", "m", session="s2").rejected)
        self.assertIsNone(h.gate.sessions.since("a/claude", "s2", h.clock()))

    def test_status_shows_cold_held_and_sessions(self):
        h = GateHarness(**AWARE)
        self.over_limit(h)
        self.start(h, "new-s")
        st = h.gate.pool_status("a/claude")
        self.assertEqual((st["held"], st["held_cold"]), (1, 1))
        self.assertTrue(st["sessions"]["aware"])
        h.gate.shutdown()


class ConcurrencyTestCase(unittest.TestCase):
    CODEX = "keys: [lq-a-codex], rules: []"

    def test_freed_slot_goes_to_the_longest_waiting(self):
        h = GateHarness(**{self.CODEX: "keys: [lq-a-codex], rules: [{ kind: concurrency, max: 1 }]"})
        first = h.gate.admit("a/codex", "gpt")
        a = h.start(pool="a/codex", model="gpt")
        h.settle()
        b = h.start(pool="a/codex", model="gpt")
        h.settle()
        h.gate.finish(first)
        a["thread"].join(1)
        h.settle()
        self.assertIn("t", a)
        self.assertNotIn("t", b)
        h.gate.finish(a["t"])
        b["thread"].join(1)
        self.assertIn("t", b)

    def test_new_arrival_does_not_jump_a_waiting_request(self):
        h = GateHarness(**{self.CODEX: "keys: [lq-a-codex], rules: [{ kind: concurrency, max: 1 }]"})
        first = h.gate.admit("a/codex", "gpt")
        a = h.start(pool="a/codex", model="gpt")
        h.settle()
        with h.gate.lock:                               # the slot frees, but a has not woken up yet
            st = h.gate.state("a/codex")
            st.counts.finish(first.count_scopes)
            first.count_scopes = []
            t = h.gate._count_block(st, h.gate._count_rules("a/codex", "gpt"), h.clock(),
                                    Ticket(pool="a/codex", model="gpt", arrival=h.clock()))
            self.assertIsNotNone(t[0])                  # a newcomer sees the slot as taken by a
            st.cond.notify_all()
        a["thread"].join(1)
        self.assertIn("t", a)

    def test_shadow_counts_and_peak(self):
        h = GateHarness(**{self.CODEX: "keys: [lq-a-codex], rules: [{ kind: concurrency, max: 1, mode: shadow }]"})
        t1 = h.gate.admit("a/codex", "gpt")
        t2 = h.gate.admit("a/codex", "gpt")
        self.assertEqual((t1.result, t2.result, t2.shadow), ("pass", "shadow_would_hold", "concurrency"))
        h.gate.finish(t1)
        h.gate.finish(t2)
        c = h.gate.pool_status("a/codex")["concurrency"]
        self.assertEqual(c, {"in_flight": 0, "peak_1h": 2, "max": None, "shadow_max": 1,
                             "would_hold_1h": {"concurrency": 1}})
        h.clock.advance(3601)
        c = h.gate.pool_status("a/codex")["concurrency"]
        self.assertEqual((c["peak_1h"], c["would_hold_1h"]), (0, {}))

    def test_money_released_request_still_needs_a_slot(self):
        h = GateHarness(**{"rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]":
                           "rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }, "
                           "{ kind: concurrency, max: 1 }]"})
        first = h.gate.admit("a/claude", "m")           # under the limit: goes, holds the only slot
        h.spend("a/claude", 21)
        box = h.start()
        for _ in range(13):
            h.advance(10)                               # past the 120 s cap: money would let it go
        self.assertNotIn("t", box)
        h.gate.finish(first)
        box["thread"].join(1)
        self.assertEqual(box["t"].result, "released_by_cap")


class CountsTestCase(unittest.TestCase):
    def test_peak_slides(self):
        c = Counts()
        c.start(["*"], 0)
        c.start(["*"], 10)
        c.finish(["*"])
        c.finish(["*"])
        c.start(["*"], 3000)
        self.assertEqual(c.peak("*", 3000), 2)
        self.assertEqual(c.peak("*", 3611), 1)
