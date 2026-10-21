import random
import unittest

from app import replay as rp


def burst(n=120, every=30, cost=1.5, sessions=("s1", "s2")):
    """Two sessions sending a $1.5 request every 30 s each: $360/h, far over a $20 limit."""
    reqs = []
    for i in range(n):
        for j, s in enumerate(sessions):
            reqs.append(rp.Req(t=1_800_000_000 + i * every + j, session=s, model="m", cost=cost,
                               cost_input=cost * 0.9, ttfb=1, dur=10, cache_read=500_000,
                               rebuild_per_token=5.75e-6))
    return reqs


class ReplayTestCase(unittest.TestCase):
    def test_no_limit_keeps_everything(self):
        reqs = burst()
        r = rp.simulate(reqs, rp.Policy(name="none", release="none"))
        self.assertEqual(r["requests"], len(reqs))
        self.assertAlmostEqual(r["spend"], sum(x.cost for x in reqs))
        self.assertEqual(r["held"], 0)

    def test_fifo_lowers_the_peak_without_errors(self):
        reqs = burst()
        free = rp.simulate(reqs, rp.Policy(name="none", release="none"))
        fifo = rp.simulate(reqs, rp.Policy(name="fifo", release="fifo", limit=20))
        self.assertLess(fifo["peak"], free["peak"])
        self.assertEqual(fifo["errors"], 0)
        self.assertEqual(fifo["requests"], len(reqs))
        self.assertLessEqual(fifo["wait_max"], 121)

    def test_long_stops_expire_the_cache(self):
        reqs = burst()
        long_gaps = rp.Policy(name="slow", release="fifo", limit=20, hold_cap_cached=400, max_hold=500,
                              fifo=[(1.0, 200)])
        r = rp.simulate(reqs, long_gaps)
        self.assertGreater(r["penalty"], 0)

    def test_reject_retries_like_claude_code(self):
        sim = rp.Sim(rp.Policy(name="x", on_exceed="reject"), random.Random(1))
        e = {"tries": 0}
        for n in range(1, 11):
            sim.backoff(e, 0)
            self.assertLessEqual(e["retry_at"], min(32, 0.5 * 2 ** (n - 1)) * 1.25)
        sim.backoff(e, 0)
        self.assertEqual(sim.errors, 1)
        self.assertEqual(e["retry_at"], rp.RESEND_SEC)

    def test_policy_file_shapes(self):
        p = rp.policy_from("x", {"release": "fifo", "limit": 15, "scale": 2}, 20)
        self.assertEqual(p.limit, 15)
        self.assertEqual(p.fifo[0], (1.0, 30.0))


def session_traffic(sessions=4, n=60):
    """Sessions that each send a warm $0.3 request every 40 s, and every 10th request after a
    4.5-minute pause: still warm, but with little of its cache life left."""
    reqs = []
    for k in range(sessions):
        t = 1_800_000_000 + k * 7
        for i in range(n):
            t += 270 if i % 10 == 9 else 40
            reqs.append(rp.Req(t=t, session=f"s{k}", model="m", cost=0.3, cost_input=0.28, ttfb=1, dur=8,
                               cache_read=600_000, cache_write=2_000, rebuild_per_token=5.75e-6))
    return sorted(reqs, key=lambda r: r.t)


class SessionAwareReplayTestCase(unittest.TestCase):
    def test_warm_deadline_avoids_cache_expiry(self):
        reqs = session_traffic()
        fifo = rp.simulate(reqs, rp.Policy(name="fifo", release="fifo", limit=10))
        aware = rp.simulate(reqs, rp.Policy(name="aware", release="fifo", limit=10, session_aware=True))
        self.assertGreater(fifo["penalty"], 0)
        self.assertEqual(aware["penalty"], 0)
        self.assertLessEqual(aware["peak"], fifo["peak"])
        self.assertEqual(aware["errors"], 0)

    def test_cold_requests_wait_longer_not_fail(self):
        reqs = burst()                                  # the first request of each session is cold
        p = rp.Policy(name="aware", release="fifo", limit=20, session_aware=True, hold_cap_cold=150,
                      fifo=[(1.0, 200)])
        r = rp.simulate(reqs, p)
        self.assertEqual(r["errors"], 0)
        self.assertLessEqual(r["warm_max"], 121)

    def test_concurrency_cap(self):
        reqs = [rp.Req(t=1_800_000_000 + i, session=f"s{i}", model="m", cost=0.1, cost_input=0.1, ttfb=1, dur=100)
                for i in range(5)]
        r = rp.simulate(reqs, rp.Policy(name="c2", release="none", concurrency=2))
        self.assertEqual(r["requests"], 5)
        self.assertGreaterEqual(r["wait_max"], 190)     # the fifth starts after two rounds of 100 s
        free = rp.simulate(reqs, rp.Policy(name="free", release="none"))
        self.assertEqual(free["held"], 0)

    def test_classify_check(self):
        reqs = [rp.Req(t=0, session="a", model="m", cost=3, cost_input=3, ttfb=1, dur=5, cache_read=0, cache_write=600_000),
                rp.Req(t=60, session="a", model="m", cost=0.3, cost_input=0.3, ttfb=1, dur=5, cache_read=600_000, cache_write=1000),
                rp.Req(t=700, session="a", model="m", cost=3, cost_input=3, ttfb=1, dur=5, cache_read=0, cache_write=601_000),
                rp.Req(t=760, session="a", model="m", cost=3, cost_input=3, ttfb=1, dur=5, cache_read=0, cache_write=602_000)]
        c = rp.classify_check(reqs)["counts"]
        self.assertEqual(c, {"cold/rebuilt": 2, "warm/hit": 1, "warm/rebuilt": 1})

    def test_policy_file_session_keys(self):
        p = rp.policy_from("x", {"session_aware": True, "hold_cap_cold": "150s", "concurrency": 3, "cache_ttl": "5m"}, 20)
        self.assertEqual((p.session_aware, p.hold_cap_cold, p.concurrency, p.cache_ttl), (True, 150, 3, 300))
