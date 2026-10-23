"""Budget reservations, in shadow (DESIGN §4.3): what the money gate would see if requests still
waiting for their input usage were already counted.

The gate only sees charges that arrived. Between a request's departure and its message_start
its input is invisible, so requests released together can all pass under the limit and then
overshoot it together. A reservation estimates a departing request's input from recent requests
of the same pool and cache state (warm / cold / none) and holds it until the request's input
charge settles it. The gate records the requests that observed + reserved spend would have held.
Nothing is enforced: /status shows the reserved amount, those would-hold counts and how far the
estimates were off, which is what to look at before any reservation enforces.
"""
import collections

SAMPLES = 200         # recent input costs kept per (pool, cache state)
MIN_SAMPLES = 10      # fewer: fall back to the pool's samples, then to no estimate
RESERVE_TTL = 600     # a reservation no charge settled is dropped after this
KEPT = 3600           # seconds of errors and would-hold events kept for /status


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


class Reservations:
    def __init__(self):
        self.samples = collections.defaultdict(lambda: collections.deque(maxlen=SAMPLES))
        self.held = collections.defaultdict(dict)        # pool -> {request id: [amount, reserved at]}
        self.errors = collections.defaultdict(collections.deque)      # pool -> (t, estimate, actual)
        self.would_hold = collections.defaultdict(collections.deque)  # pool -> t

    def observe(self, pool, cache, amount):
        """The input cost of a request that settled with upstream usage."""
        if amount and amount > 0:
            self.samples[pool, cache or "none"].append(amount)
            self.samples[pool, "*"].append(amount)

    def estimate(self, pool, cache, q):
        for key in ((pool, cache or "none"), (pool, "*")):
            if len(self.samples.get(key, ())) >= MIN_SAMPLES:
                return quantile(self.samples[key], q)
        return None

    def reserved(self, pool, now):
        held = self.held.get(pool)
        if not held:
            return 0.0
        for rid in [rid for rid, (_, t) in held.items() if t < now - RESERVE_TTL]:
            del held[rid]
        return sum(a for a, _ in held.values())

    def reserve(self, pool, rid, amount, now):
        if amount:
            self.held[pool][rid] = [amount, now]

    def settle(self, pool, rid, actual, now):
        """The request's input charge arrived: the reservation goes, its error is kept."""
        held = self.held.get(pool)
        entry = held.pop(rid, None) if held else None
        if entry is not None:
            errors = self.errors[pool]
            errors.append((now, entry[0], actual))
            while errors and errors[0][0] < now - KEPT:
                errors.popleft()

    def release(self, pool, rid):
        """The request ended without an input charge (an error, a hang-up before message_start)."""
        held = self.held.get(pool)
        if held:
            held.pop(rid, None)

    def held_back(self, pool, now):
        q = self.would_hold[pool]
        q.append(now)
        while q and q[0] < now - KEPT:
            q.popleft()

    def status(self, pool, now, q):
        errors = [(e, a) for t, e, a in self.errors.get(pool, ()) if t >= now - KEPT]
        diffs = [e - a for e, a in errors]
        would = [t for t in self.would_hold.get(pool, ()) if t >= now - KEPT]
        return {"mode": "shadow", "quantile": q, "reserved": round(self.reserved(pool, now), 4),
                "in_flight": len(self.held.get(pool, {})), "would_hold_1h": len(would),
                "estimate": {c: (round(v, 4) if v is not None else None)
                             for c in ("warm", "cold", "none") for v in [self.estimate(pool, c, q)]},
                "error_1h": {"n": len(errors),
                             "bias": round(sum(diffs) / len(diffs), 4) if diffs else None,
                             "mae": round(sum(abs(d) for d in diffs) / len(diffs), 4) if diffs else None,
                             "p90_abs": round(quantile([abs(d) for d in diffs], .9), 4) if diffs else None,
                             "under_share": round(sum(1 for d in diffs if d < 0) / len(diffs), 3) if diffs else None}}

    def dump(self):
        return {pool: {rid: list(v) for rid, v in held.items()} for pool, held in self.held.items() if held}

    def load(self, data):
        self.held = collections.defaultdict(dict, {pool: {rid: list(v) for rid, v in held.items()}
                                                   for pool, held in (data or {}).items()})
