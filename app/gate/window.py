import threading
import fnmatch
from bisect import bisect_left, bisect_right

HISTORY_SPAN = 32 * 86400


class SpendHistory:
    """Exact charge times, prefix sums for O(log n) cycle and short-window reads.

    Each model has its own series so a newly added model scope works on reload, too.
    Out-of-order recovery charges invalidate the index, rebuilt once on the next read.
    """
    def __init__(self):
        self.amounts = {}
        self.times = []
        self.sums = [0.0]
        self.dirty = False

    def add(self, t, amount):
        self.amounts[t] = self.amounts.get(t, 0.0) + amount
        if not self.dirty and (not self.times or t > self.times[-1]):
            self.times.append(t)
            self.sums.append(self.sums[-1] + amount)
        else:
            self.dirty = True

    def prune(self, now):
        old = [t for t in self.amounts if t < now - HISTORY_SPAN]
        if old:
            for t in old:
                del self.amounts[t]
            self.dirty = True

    def total(self, since, now, after=False):
        if self.dirty:
            self.times = sorted(self.amounts)
            self.sums = [0.0]
            for t in self.times:
                self.sums.append(self.sums[-1] + self.amounts[t])
            self.dirty = False
        start = (bisect_right if after else bisect_left)(self.times, since)
        end = bisect_right(self.times, now)
        return max(0.0, self.sums[end] - self.sums[start])

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
        self.lock = threading.RLock()
        self.windows = {}
        self.history = {}  # (pool, actual model) -> SpendHistory
        self.pruned_hour = None

    def add_history(self, pool, model, t, amount, now):
        if not amount or t < now - HISTORY_SPAN:
            return
        with self.lock:
            self.history.setdefault((pool, model or ""), SpendHistory()).add(t, amount)
            if int(now // 3600) != self.pruned_hour:
                for history in self.history.values():
                    history.prune(now)
                self.pruned_hour = int(now // 3600)

    def total_since(self, pool, model_pattern, since, now, after=False):
        with self.lock:
            return sum(h.total(since, now, after) for (p, m), h in self.history.items()
                       if p == pool and (model_pattern is None or fnmatch.fnmatchcase(m, model_pattern)))

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
