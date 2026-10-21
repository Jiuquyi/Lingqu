"""Concurrency and RPM: limits on request counts, independent of money (DESIGN §4.3 ④)."""
import collections

SPAN = 3600


class Counts:
    def __init__(self):
        self.in_flight = collections.Counter()  # scope key -> requests in flight
        self.starts = collections.defaultdict(collections.deque)  # scope key -> start times
        self.levels = collections.defaultdict(collections.deque)  # scope key -> (t, in flight after a start)

    def start(self, scopes, now):
        for s in scopes:
            self.in_flight[s] += 1
            self.starts[s].append(now)
            q = self.levels[s]
            # Keep only what can still be the peak: drop older levels not above this one.
            while q and q[-1][1] <= self.in_flight[s]:
                q.pop()
            q.append((now, self.in_flight[s]))

    def finish(self, scopes):
        for s in scopes:
            self.in_flight[s] = max(0, self.in_flight[s] - 1)

    def rpm(self, scope, now):
        q = self.starts[scope]
        while q and q[0] <= now - 60:
            q.popleft()
        return len(q), (q[0] + 60 if q else now)

    def prune(self, now):
        for scope in list(self.starts):
            self.rpm(scope, now)
            self.peak(scope, now)
            if not self.starts[scope] and not self.levels[scope] and not self.in_flight[scope]:
                del self.starts[scope]
                self.levels.pop(scope, None)
                self.in_flight.pop(scope, None)

    def peak(self, scope, now):
        """Most requests in flight at once in the last hour (at least the current count)."""
        q = self.levels[scope]
        while q and q[0][0] < now - SPAN:
            q.popleft()
        return max(q[0][1] if q else 0, self.in_flight[scope])
