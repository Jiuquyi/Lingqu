"""Concurrency and RPM: limits on request counts, independent of money (DESIGN §4.3 ④)."""
import collections


class Counts:
    def __init__(self):
        self.in_flight = collections.Counter()  # scope key -> requests in flight
        self.starts = collections.defaultdict(collections.deque)  # scope key -> start times

    def start(self, scopes, now):
        for s in scopes:
            self.in_flight[s] += 1
            self.starts[s].append(now)

    def finish(self, scopes):
        for s in scopes:
            self.in_flight[s] = max(0, self.in_flight[s] - 1)

    def rpm(self, scope, now):
        q = self.starts[scope]
        while q and q[0] <= now - 60:
            q.popleft()
        return len(q), (q[0] + 60 if q else now)
