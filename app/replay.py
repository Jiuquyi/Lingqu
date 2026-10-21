"""`flask replay` (DESIGN §7): run past requests through other policies and compare.

Model:
- Each session (or user when there is no session) sends serially: when one of its requests is
  held, its later requests move back by the same amount. Costs are never dropped, only moved.
- A held request waits as the policy says. A rejected one (503) is retried the way Claude Code
  does it: try n waits 0.5 s x 2^(n-1), at most 32 s, plus 0-25 % jitter; after 10 tries it is
  an error and the user sends it again 30 s later.
- Cache expiry: a request that starts more than 5 minutes after its session's previous start,
  when originally it did not, pays its cache reads again at (cache write - cache read) price.
- The gate sees a request's input cost at its first byte and the rest when it ends.
- Session-aware FIFO (DESIGN §4.5), as the gate does it: a request whose session started nothing
  in the last `cache_ttl` is cold; it rebuilds the cache however long it waits, so it may wait up
  to min(`hold_cap_cold`, `max_hold`) instead of `hold_cap_cached`. A warm one leaves at the
  latest CACHE_MARGIN before its cache expires. The queue is served earliest deadline first
  (arrival + the request's own cap), which is plain FIFO when every cap is the same.
- An optional concurrency cap: a request starts only while fewer than `concurrency` run.

Limits: the cache model looks only at start gaps; without session ids all of a user's sessions
are one; sub-agent caches are not modelled.
"""
import collections
import heapq
import json
import random
import statistics
import time
from dataclasses import dataclass, field

import yaml

from .gate.fifo import gap as fifo_gap
from .gate.phase import Phase
from .gate.sessions import CACHE_MARGIN
from .settings import DEFAULT_FIFO, DEFAULT_PHASE, parse_duration

CACHE_TTL = 300
RETRY_TRIES = 10
RESEND_SEC = 30
TAIL_SEC = 6 * 3600


@dataclass
class Req:
    t: float                    # original start
    session: str
    model: str
    cost: float                 # eUSD
    cost_input: float           # part visible at the first byte
    ttfb: float                 # seconds to the first byte
    dur: float                  # seconds start to end
    cache_read: int = 0
    rebuild_per_token: float = 0.0  # extra eUSD per cache-read token when the cache expired
    cache_write: int = 0            # tokens written to the cache (classify_check only)


@dataclass
class Policy:
    name: str
    release: str = "none"       # none | fifo | phase
    on_exceed: str = "hold"     # hold | reject
    limit: float = 20.0
    fifo: list = field(default_factory=lambda: list(DEFAULT_FIFO))
    phase: list = field(default_factory=lambda: list(DEFAULT_PHASE))
    hold_cap_cached: float = 120.0
    hold_cap_cold: float = 300.0
    max_hold: float = 150.0
    max_queue: int = 8
    session_aware: bool = False  # cold requests wait up to hold_cap_cold, warm ones go first
    warm_deadline: bool = True  # session-aware: a warm request leaves before its cache expires
    cold_cap: bool = True       # session-aware: a cold request may wait up to hold_cap_cold
    cache_ttl: float = CACHE_TTL
    concurrency: int = 0        # requests in flight at most; 0: no cap
    visible: str = "start"      # start: counted in the data path; log: counted after the request ended


def policy_from(name, raw, default_limit):
    raw = raw or {}
    p = Policy(name=name, limit=float(raw.get("limit", default_limit)))
    p.release = raw.get("release", "fifo")
    p.on_exceed = raw.get("on_exceed", "hold")
    p.visible = raw.get("visible", "start")
    if raw.get("fifo"):
        p.fifo = [(float(s["at"]), parse_duration(s["gap"])) for s in raw["fifo"]]
    if raw.get("phase"):
        p.phase = [(float(s["at"]), parse_duration(s["off"]), parse_duration(s["on"])) for s in raw["phase"]]
    for k in ("hold_cap_cached", "hold_cap_cold", "max_hold", "cache_ttl"):
        if raw.get(k) is not None:
            setattr(p, k, parse_duration(raw[k]))
    for k in ("max_queue", "concurrency"):
        if raw.get(k) is not None:
            setattr(p, k, int(raw[k]))
    p.session_aware = bool(raw.get("session_aware", False))
    p.warm_deadline = bool(raw.get("warm_deadline", True))
    p.cold_cap = bool(raw.get("cold_cap", True))
    if raw.get("scale"):
        p.fifo = [(a, g * float(raw["scale"])) for a, g in p.fifo]
    return p


def load_policies(path, default_limit):
    """A policy file: one policy (a mapping) or several ({"policies": [{name: ..., ...}]})."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    items = raw.get("policies") if isinstance(raw, dict) and "policies" in raw else [raw]
    return [policy_from(it.get("name") or f"policy {i + 1}", it, default_limit) for i, it in enumerate(items)]


def current_policy(settings, pool):
    """The pool's enforcing pool-wide hourly_cap (and pool-wide concurrency) as configured now."""
    p = settings.pools[pool]
    conc = [r.max for r in p.rules if r.kind == "concurrency" and not r.shadow and r.scope_model is None]
    for r in p.rules:
        if r.kind == "hourly_cap" and not r.shadow and r.scope_model is None:
            name = f"current ({r.release}{' + sessions' if r.session_aware else ''}, ${r.limit:g})"
            return Policy(name=name, release=r.release, on_exceed=r.on_exceed,
                          limit=r.limit, fifo=settings.fifo, phase=settings.phase,
                          hold_cap_cached=settings.hold_cap_cached, hold_cap_cold=settings.hold_cap_cold,
                          max_hold=settings.max_hold, max_queue=settings.max_queue,
                          session_aware=r.session_aware, cache_ttl=settings.cache_ttl,
                          concurrency=min(conc) if conc else 0)
    return None


# ------------------------------------------------------------ sources

def rebuild_rate(price):
    return max(0.0, price["cache_write_5m"] - price["cache_read"]) / 1_000_000


def from_ledger(pool, start, end, price_of):
    from .models import Ledger
    rows = Ledger.query.filter(Ledger.pool == pool, Ledger.ts >= start, Ledger.ts < end,
                               Ledger.status < 400).order_by(Ledger.ts).all()
    out = []
    for r in rows:
        model = r.model_actual or r.model_req or ""
        held = (r.held_ms or 0) / 1000
        out.append(Req(t=r.ts + held, session=r.session_tag or r.key_ref or "-", model=model,
                       cost=r.eusd or 0, cost_input=r.eusd_input or 0, ttfb=(r.ttfb_ms or 0) / 1000,
                       dur=max(1.0, (r.duration_ms or 0) / 1000), cache_read=r.cache_read_tokens or 0,
                       cache_write=(r.cache_write_5m_tokens or 0) + (r.cache_write_1h_tokens or 0),
                       rebuild_per_token=rebuild_rate(price_of(model))))
    return out


def from_newapi(base_url, token, user_id, start, end, group, model_filter, price_of):
    """Consumption logs (type 2) through New API's admin log API with an access token."""
    import httpx
    headers = {"Authorization": f"Bearer {token}", "New-Api-User": str(user_id)}
    base = base_url.rstrip("/")
    st = httpx.get(base + "/api/status", timeout=15).json().get("data") or {}
    per_usd = float(st.get("quota_per_unit") or 500000)
    items, page = [], 1
    while True:
        q = {"type": 2, "start_timestamp": int(start), "end_timestamp": int(end), "p": page, "page_size": 100}
        if group:
            q["group"] = group
        r = httpx.get(base + "/api/log/", params=q, headers=headers, timeout=60)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"New API /api/log/: {body.get('message')}")
        data = body["data"]
        items += data.get("items") or []
        if len(items) >= data.get("total", 0) or not data.get("items"):
            break
        page += 1
    out = []
    for it in items:
        model = it.get("model_name") or ""
        if (group and it.get("group") != group) or not model_filter(model):
            continue
        try:
            other = json.loads(it.get("other") or "{}")
        except ValueError:
            other = {}
        dur = max(1, it.get("use_time") or 1)
        cost_all = (it.get("quota") or 0) / per_usd
        price = price_of(model)
        out_cost = (it.get("completion_tokens") or 0) * price["output"] / 1_000_000
        # created_at is when New API wrote the log: the request's end.
        out.append(Req(t=it["created_at"] - dur, session=it.get("username") or "-", model=model, cost=cost_all,
                       cost_input=max(0.0, cost_all - out_cost), ttfb=(other.get("frt") or 0) / 1000, dur=dur,
                       cache_read=other.get("cache_tokens") or 0, cache_write=other.get("cache_creation_tokens") or 0,
                       rebuild_per_token=rebuild_rate(price)))
    out.sort(key=lambda r: r.t)
    return out


def from_jsonl(path, price_of):
    """One JSON object per line: t, session, model, cost, [cost_input, ttfb, dur, cache_read, cache_write]."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            model = o.get("model", "")
            out.append(Req(t=float(o["t"]), session=str(o.get("session", "-")), model=model, cost=float(o["cost"]),
                           cost_input=float(o.get("cost_input", o["cost"])), ttfb=float(o.get("ttfb", 0)),
                           dur=max(1.0, float(o.get("dur", 1))), cache_read=int(o.get("cache_read", 0)),
                           cache_write=int(o.get("cache_write", 0)), rebuild_per_token=rebuild_rate(price_of(model))))
    out.sort(key=lambda r: r.t)
    return out


# ------------------------------------------------------------ simulation

class Window:
    """Spend in the trailing hour, from when each cost becomes visible."""

    def __init__(self):
        self.pending = []           # (visible at, cost), kept sorted lazily
        self.items = collections.deque()
        self.total = 0.0

    def add(self, at, amount):
        heapq.heappush(self.pending, (at, amount))

    def tick(self, t):
        while self.pending and self.pending[0][0] <= t:
            at, c = heapq.heappop(self.pending)
            self.items.append((at, c))
            self.total += c
        while self.items and self.items[0][0] <= t - 3600:
            self.total -= self.items.popleft()[1]


class Sim:
    def __init__(self, policy, rng):
        self.p, self.rng = policy, rng
        self.queue = []             # held entries
        self.last_departure = None
        self.phase = Phase(policy.phase)
        self.errors = 0
        self.ends = []              # end times of the requests in flight (heap)
        self.cap_releases = 0       # held requests let through by their cap, not by the gap

    def backoff(self, e, t):
        e["tries"] += 1
        if e["tries"] > RETRY_TRIES:
            self.errors += 1
            e["tries"], e["retry_at"] = 0, t + RESEND_SEC
        else:
            e["retry_at"] = t + min(32, 0.5 * 2 ** (e["tries"] - 1)) * (1 + 0.25 * self.rng.random())

    def cap(self, e):
        """How long a request may be held before it goes whatever the ratio."""
        p = self.p
        if not p.session_aware:
            return p.hold_cap_cached
        if e["cold"]:
            # As the gate: a request that rebuilds anyway is let through at max_hold, never turned away.
            return min(p.hold_cap_cold, p.max_hold) if p.cold_cap else p.hold_cap_cached
        if p.warm_deadline:
            return max(0.0, min(p.hold_cap_cached, p.cache_ttl - CACHE_MARGIN - e["since"]))
        return p.hold_cap_cached

    def order(self, e):
        """Queue order: arrival for plain FIFO; the request's own deadline when session-aware."""
        return e["held_since"] + (e["cap"] if self.p.session_aware else 0)

    def started(self, t, dur):
        heapq.heappush(self.ends, t + dur)

    def free(self, t):
        """Concurrency slots free now (a large number without a cap)."""
        if not self.p.concurrency:
            return 1 << 30
        while self.ends and self.ends[0] <= t:
            heapq.heappop(self.ends)
        return self.p.concurrency - len(self.ends)

    def open_now(self, t, ratio):
        """Would a request arriving now go straight through? Held requests keep their place."""
        p = self.p
        if self.free(t) - len(self.queue) <= 0:
            return False
        if p.release == "none" or ratio < 1:
            return True
        if p.release == "phase":
            self.phase.tick(t, ratio)
            return self.phase.passing()
        return not self.queue and (self.last_departure is None or t >= self.last_departure + fifo_gap(p.fifo, ratio))

    def arrive(self, t, ratio, e):
        """True: starts now. False: held or waiting to retry."""
        if self.open_now(t, ratio):
            self.last_departure = t
            return True
        if self.p.on_exceed == "reject" or len(self.queue) >= self.p.max_queue:
            e["retry_at"] = None
            self.backoff(e, t)
            e["retrying"] = True
            return False
        e["held_since"] = t
        e["cap"] = self.cap(e)
        self.queue.append(e)
        return False

    def tick(self, t, ratio):
        """Entries that start now."""
        p = self.p
        out = []
        if p.release == "phase":
            self.phase.tick(t, ratio)
        free = self.free(t)
        keep = []
        for e in sorted(self.queue, key=self.order):
            waited = t - e["held_since"]
            money = (p.release == "none" or ratio < 1 or (p.release == "phase" and self.phase.passing())
                     or waited >= e["cap"])
            if money and free > 0:
                out.append(e)
                free -= 1
                if p.release == "fifo" and ratio >= 1:
                    self.cap_releases += 1
            elif waited >= p.max_hold:
                e["retrying"] = True
                self.backoff(e, t)
            else:
                keep.append(e)
        self.queue = keep
        if p.release == "fifo" and keep and ratio >= 1 and free > 0:
            if self.last_departure is None or t >= self.last_departure + fifo_gap(p.fifo, ratio):
                out.append(self.queue.pop(0))
        if out:
            self.last_departure = t
        return out


def pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def simulate(reqs, policy, seed=1):
    by_session = collections.defaultdict(list)
    for r in reqs:
        by_session[r.session].append(r)
    if not reqs:
        return None
    sim = Sim(policy, random.Random(seed))
    win = Window()
    t0 = int(min(r.t for r in reqs))
    t1 = int(max(r.t for r in reqs)) + TAIL_SEC
    st = {s: {"i": 0, "delay": 0.0, "last_start": None, "last_orig": None,
              "arrival": rs[0].t, "entry": None} for s, rs in by_session.items()}
    started, waits = [], {"warm": [], "cold": []}
    penalty, delays = 0.0, collections.Counter()
    stop_runs, run_start = [], None

    def is_cold(s):
        return s["last_start"] is None or s["arrival"] - s["last_start"] > policy.cache_ttl

    def start(sess, now):
        nonlocal penalty
        s = st[sess]
        r = by_session[sess][s["i"]]
        wait = max(0.0, now - s["arrival"])
        cold = is_cold(s)
        c = r.cost
        if (s["last_start"] is not None and now - s["last_start"] > policy.cache_ttl
                and r.t - s["last_orig"] <= policy.cache_ttl):
            extra = r.cache_read * r.rebuild_per_token
            penalty += extra
            c += extra
        if policy.visible == "log":
            win.add(now + r.dur + 10, c)
        else:
            win.add(now + r.ttfb, r.cost_input + (c - r.cost))
            win.add(now + r.dur, c - r.cost_input - (c - r.cost))
        sim.started(now, r.dur)
        started.append((now, c))
        if wait > 0.5:
            waits["cold" if cold else "warm"].append(wait)
        delays[sess] += wait
        s["delay"] += wait
        s["last_start"], s["last_orig"] = now, r.t
        s["i"] += 1
        s["entry"] = None
        rs = by_session[sess]
        s["arrival"] = rs[s["i"]].t + s["delay"] if s["i"] < len(rs) else None

    t = t0
    while t < t1:
        win.tick(t)
        ratio = win.total / policy.limit if policy.limit else 0.0
        for e in sim.tick(t, ratio):
            start(e["session"], t)
        for sess, s in st.items():
            e = s["entry"]
            if e is not None:
                if e.get("retrying") and e["retry_at"] is not None and e["retry_at"] <= t:
                    if sim.open_now(t, ratio):
                        sim.last_departure = t
                        start(sess, t)
                    else:
                        sim.backoff(e, t)
                continue
            if s["arrival"] is not None and s["arrival"] < t + 1:
                e = s["entry"] = {"session": sess, "tries": 0, "retrying": False, "cold": is_cold(s),
                                  "since": max(t, s["arrival"]) - s["last_start"] if s["last_start"] else None}
                if sim.arrive(max(t, s["arrival"]), ratio, e):
                    start(sess, s["arrival"])
        waiting = any(s["entry"] is not None for s in st.values())
        if waiting and run_start is None:
            run_start = t
        elif not waiting and run_start is not None:
            stop_runs.append(t - run_start)
            run_start = None
        if not waiting and all(s["arrival"] is None for s in st.values()):
            break
        # Jump over idle stretches: nothing waits and the next arrival is later. Not for phase:
        # its off/on cycle moves on with time, requests or not.
        if not waiting and not sim.queue and policy.release != "phase":
            nxt = min((s["arrival"] for s in st.values() if s["arrival"] is not None), default=t1)
            t = max(t + 1, int(nxt) - 1) if nxt > t + 2 else t + 1
            continue
        t += 1

    started.sort()
    peak, j, acc = 0.0, 0, 0.0
    for tt, c in started:
        acc += c
        while started[j][0] <= tt - 3600:
            acc -= started[j][1]
            j += 1
        peak = max(peak, acc)
    every = waits["warm"] + waits["cold"]
    return {"name": policy.name, "requests": len(started), "spend": sum(c for _, c in started), "peak": peak,
            "penalty": penalty, "longest_stop": max(stop_runs, default=0), "held": len(every),
            "wait_p50": statistics.median(every) if every else 0, "wait_p90": pct(every, 0.9),
            "wait_max": max(every, default=0),
            "warm_held": len(waits["warm"]), "warm_p50": statistics.median(waits["warm"]) if waits["warm"] else 0,
            "warm_max": max(waits["warm"], default=0),
            "cold_held": len(waits["cold"]), "cold_p50": statistics.median(waits["cold"]) if waits["cold"] else 0,
            "cap_releases": sim.cap_releases, "delay_total": sum(delays.values()), "errors": sim.errors,
            "delays": dict(delays)}


def classify_check(reqs, ttl=CACHE_TTL):
    """How well "nothing from this session in the last ttl" predicts a cache rebuild, judged by
    the tokens each request actually read and wrote. Only requests with token data count."""
    last = {}
    n = collections.Counter()
    cost = collections.Counter()
    for r in sorted(reqs, key=lambda r: r.t):
        prev = last.get(r.session)
        last[r.session] = r.t
        if r.cache_read + r.cache_write == 0 or r.session in ("", "-"):
            continue
        predicted = prev is None or r.t - prev > ttl
        actual = r.cache_write > r.cache_read
        key = ("cold" if predicted else "warm") + "/" + ("rebuilt" if actual else "hit")
        n[key] += 1
        cost[key] += r.cost
    return {"counts": dict(n), "cost": {k: round(v, 2) for k, v in cost.items()}}


def table(results, echo):
    echo(f"{'policy':<34} {'peak 60m':>9} {'cache exp.':>10} {'spend':>8} {'held':>5} {'p50 wait':>8} "
         f"{'p90 wait':>8} {'max wait':>8} {'warm p50/max':>13} {'cold p50':>8} {'cap rel.':>8} "
         f"{'longest stop':>12} {'errors':>6} {'delay h':>7}  per-session delay (min)")
    for r in results:
        d = "  ".join(f"{s[:12]} {v / 60:.0f}" for s, v in sorted(r["delays"].items(), key=lambda kv: -kv[1])[:6])
        echo(f"{r['name'][:34]:<34} ${r['peak']:>8.2f} ${r['penalty']:>9.2f} ${r['spend']:>7.1f} {r['held']:>5} "
             f"{r['wait_p50']:>7.0f}s {r['wait_p90']:>7.0f}s {r['wait_max']:>7.0f}s "
             f"{r['warm_p50']:>5.0f}s/{r['warm_max']:>5.0f}s {r['cold_p50']:>7.0f}s {r['cap_releases']:>8.0f} "
             f"{r['longest_stop']:>11.0f}s "
             f"{r['errors']:>6} {r['delay_total'] / 3600:>7.1f}  {d}")


def classify_table(check, echo):
    c, m = check["counts"], check["cost"]
    total = sum(c.values())
    if not total:
        echo("cold/warm check: no requests with session ids and cache token counts")
        return
    echo(f"cold/warm check ({total} requests with a session and token counts; predicted / actual):")
    for k in ("warm/hit", "warm/rebuilt", "cold/rebuilt", "cold/hit"):
        echo(f"  {k:<13} {c.get(k, 0):>6}  ${m.get(k, 0):>9.2f}")


def parse_day(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d"))
