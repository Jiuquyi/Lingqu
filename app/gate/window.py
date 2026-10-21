import threading

BUCKET = 60
SPAN = 3600
BUCKETS = SPAN // BUCKET


class Window:
    """Spend over the trailing hour in 60 one-minute buckets. No reset on the hour: the bucket
    for minute m counts until minute m + 60 starts."""

    def __init__(self):
        self.buckets = {}  # minute index -> eUSD

    def _prune(self, now):
        oldest = int(now // BUCKET) - BUCKETS + 1
        for m in [m for m in self.buckets if m < oldest]:
            del self.buckets[m]

    def add(self, t, amount, now=None):
        if amount:
            self.buckets[int(t // BUCKET)] = self.buckets.get(int(t // BUCKET), 0.0) + amount
            self._prune(now if now is not None else t)

    def total(self, now):
        self._prune(now)
        return sum(self.buckets.values())

    def clear_at(self, now, limit):
        """When the total drops below limit if nothing new comes in (None when it already is)."""
        total = self.total(now)
        if total < limit:
            return None
        for m in sorted(self.buckets):
            total -= self.buckets[m]
            if total < limit:
                return (m + BUCKETS) * BUCKET
        return None

    def dump(self):
        return dict(self.buckets)


class Counters:
    """Rolling windows per (pool, scope). Scope "*" is the whole pool; "model=<glob>" a model scope.
    In process memory; rebuilt from the ledger on start (CounterStore in DESIGN §2.3)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.windows = {}

    def add(self, pool, scopes, t, amount, now=None):
        with self.lock:
            for scope in scopes:
                self.windows.setdefault((pool, scope), Window()).add(t, amount, now)

    def spent(self, pool, scope, now):
        with self.lock:
            w = self.windows.get((pool, scope))
            return w.total(now) if w else 0.0

    def clear_at(self, pool, scope, now, limit):
        with self.lock:
            w = self.windows.get((pool, scope))
            return w.clear_at(now, limit) if w else None

    def reset(self, pool):
        with self.lock:
            for k in [k for k in self.windows if k[0] == pool]:
                del self.windows[k]
