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
