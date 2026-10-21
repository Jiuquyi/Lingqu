#!/usr/bin/env python3
"""Synthetic Claude Code traffic for `flask replay --source jsonl`, and a direct policy comparison.

The real replays in DESIGN §4.5 had one or two people queuing at a time, too few to tell whether
a session-aware queue helps. This makes more of the same kind of traffic, calibrated to what the
bypass throttle's logs showed (DESIGN §1.3, §4.5): two people send about 1700 requests a week,
a request that hits the cache costs about $0.17 at the median, a rebuild of a 600k-800k context
costs $3-4, and about one request in ten is cold. It is a model, not data: use it to see how a
policy behaves when more people share a pool, then confirm on the pool's own ledger.

    python scripts/replay_synth.py --users 4 --out /tmp/four.jsonl          # write a workload
    python scripts/replay_synth.py --users 4 --limit 30 --policy docs/replay/m3.yaml --seeds 5
"""
import argparse
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import replay as rp  # noqa: E402

# Claude Opus list prices, $ / 1M tokens.
P_IN, P_OUT, P_READ, P_WRITE = 5.0, 25.0, 0.5, 6.25
REBUILD = (P_WRITE - P_READ) / 1_000_000
T0 = 1_792_000_000 - 1_792_000_000 % 86400 - 8 * 3600  # a Monday 00:00 in UTC+8
BLOCKS = [(9.5, 12.0), (13.5, 18.5)]                   # working hours
EVENING = (20.0, 22.5)


def lognormal(rng, median, sigma, lo=0.0, hi=math.inf):
    return min(hi, max(lo, median * math.exp(rng.gauss(0, sigma))))


def user_requests(rng, user, days, intensity):
    """One person's requests over `days` days: tasks of serial requests inside working hours."""
    out = []
    session_n = 0
    for day in range(days):
        if day % 7 >= 5 and rng.random() > 0.25:      # most weekends off
            continue
        blocks = list(BLOCKS) + ([EVENING] if rng.random() < 0.3 else [])
        for b0, b1 in blocks:
            t = T0 + day * 86400 + (b0 + rng.uniform(0, 0.75)) * 3600
            end = T0 + day * 86400 + (b1 - rng.uniform(0, 0.5)) * 3600
            session_n += 1
            session, ctx = f"{user}-s{session_n}", rng.uniform(20e3, 40e3)
            last_start = None
            while t < end:
                if rng.random() < 0.05:               # /clear or a new terminal: a new, small session
                    session_n += 1
                    session, ctx = f"{user}-s{session_n}", rng.uniform(20e3, 40e3)
                    last_start = None
                for _ in range(1 + int(rng.expovariate(1 / (4 * intensity)))):
                    cold = last_start is None or t - last_start > rp.CACHE_TTL
                    new = lognormal(rng, 4000, 1.0, 200, 60e3)
                    out_tok = lognormal(rng, 350, 1.0, 20, 8000)
                    if cold:
                        c_in = (ctx + new) * P_WRITE / 1e6
                        read, write = 0, ctx + new
                    else:
                        c_in = ctx * P_READ / 1e6 + new * P_WRITE / 1e6
                        read, write = ctx, new
                    ttfb = 2 + rng.uniform(0, 3) + (ctx / 150e3 if cold else ctx / 600e3)
                    dur = ttfb + out_tok / rng.uniform(40, 80)
                    out.append({"t": round(t, 1), "session": session, "model": "claude-opus-5-5",
                                "cost": round(c_in + out_tok * P_OUT / 1e6, 5), "cost_input": round(c_in, 5),
                                "ttfb": round(ttfb, 1), "dur": round(dur, 1), "cache_read": int(read),
                                "cache_write": int(write)})
                    last_start = t
                    ctx += new + out_tok
                    if ctx > 800e3:                   # auto-compact: the next prefix is new and small
                        ctx = rng.uniform(50e3, 80e3)
                        last_start = None
                    t += dur + lognormal(rng, 6, 1.0, 1, 180)   # tools run, then the next request
                    if t >= end:
                        break
                t += lognormal(rng, 240, 1.2, 10, 3600) / intensity  # the person reads, thinks, types
    return out


def workload(users, days=7, seed=1, intensity=1.0):
    rng = random.Random(seed)
    rows = []
    for u in range(users):
        rows += user_requests(rng, f"u{u + 1}", days, intensity * rng.uniform(0.7, 1.3))
    rows.sort(key=lambda r: r["t"])
    return rows


def as_reqs(rows):
    return [rp.Req(t=r["t"], session=r["session"], model=r["model"], cost=r["cost"], cost_input=r["cost_input"],
                   ttfb=r["ttfb"], dur=r["dur"], cache_read=r["cache_read"], cache_write=r["cache_write"],
                   rebuild_per_token=REBUILD) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=2)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=1, help="Run this many workloads (seed, seed+1, ...) and average.")
    ap.add_argument("--intensity", type=float, default=1.0, help="Scale how busy each person is.")
    ap.add_argument("--out", help="Write the workload as jsonl here instead of comparing policies.")
    ap.add_argument("--limit", type=float, default=20.0)
    ap.add_argument("--policy", help="Policy YAML (one policy or a policies list) to compare with plain fifo.")
    a = ap.parse_args()

    if a.out:
        rows = workload(a.users, a.days, a.seed, a.intensity)
        with open(a.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"{len(rows)} requests, ${sum(r['cost'] for r in rows):.0f} -> {a.out}")
        return

    policies = [rp.Policy(name="no limit", release="none", limit=a.limit),
                rp.Policy(name=f"fifo (M2), ${a.limit:g}", release="fifo", limit=a.limit)]
    if a.policy:
        policies += rp.load_policies(a.policy, a.limit)
    runs = []
    for k in range(a.seeds):
        rows = workload(a.users, a.days, a.seed + k, a.intensity)
        reqs = as_reqs(rows)
        if k == 0:
            print(f"{a.users} users, {a.days} days: {len(reqs)} requests, ${sum(r.cost for r in reqs):.0f}, "
                  f"{len({r.session for r in reqs})} sessions (seed {a.seed})")
            rp.classify_table(rp.classify_check(reqs), print)
        runs.append([rp.simulate(reqs, p) for p in policies])
    if a.seeds == 1:
        rp.table(runs[0], print)
        return
    keys = ("peak", "penalty", "spend", "held", "wait_p50", "wait_p90", "wait_max", "warm_held", "warm_p50",
            "warm_max", "cold_held", "cold_p50", "cap_releases", "longest_stop", "errors", "delay_total")
    mean = []
    for i, p in enumerate(policies):
        m = {k: sum(r[i][k] for r in runs) / len(runs) for k in keys}
        mean.append({**m, "name": p.name, "delays": {}})
    print(f"mean of {a.seeds} workloads (seeds {a.seed}..{a.seed + a.seeds - 1}):")
    rp.table(mean, print)


if __name__ == "__main__":
    main()
