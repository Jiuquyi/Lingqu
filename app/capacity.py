"""Process-wide admission in front of the pool gates (DESIGN §11.2).

Every proxy request, held ones included, takes a worker thread and keeps its body in memory
until its response closes (httpx keeps the request content). Per-pool max_queue does not bound
either across pools. A request over the active limit or the in-flight body bytes gets a 503 at
once, before its body is read, so the worker keeps threads for the status port (health checks,
admin) and its memory below the container limit.
"""
import collections
import os
import threading
import time

RESERVED_THREADS = 8  # gunicorn threads kept for the status and dashboard ports
REJECTS_KEPT = 3600


def worker_threads():
    return int(os.environ.get("LINGQU_THREADS", "64"))


def default_max_active():
    return max(1, worker_threads() - RESERVED_THREADS)


class Capacity:
    def __init__(self, settings, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.active = 0
        self.body = 0
        self.peak = collections.deque()      # (t, active) when a new hour's peak may start
        self.rejects = collections.deque()   # (t, reason)
        self.configure(settings)

    def configure(self, settings):
        self.max_active = settings.max_active or default_max_active()
        self.max_body = int(settings.max_body_in_flight_mb * 1024 * 1024)

    def enter(self, body_bytes):
        """A Slot, or (None, reason) when the process is full. A single body larger than the
        whole allowance still gets in when nothing else holds a body (MAX_CONTENT_LENGTH caps it)."""
        with self.lock:
            now = self.clock()
            reason = None
            if self.active >= self.max_active:
                reason = f"{self.active} requests in progress (limit {self.max_active})"
            elif body_bytes and self.body and self.body + body_bytes > self.max_body:
                reason = f"request bodies in memory would exceed {self.max_body // (1024 * 1024)} MiB"
            if reason:
                self.rejects.append((now, "active" if reason.endswith(")") else "body"))
                self._trim(now)
                return None, reason
            self.active += 1
            self.body += body_bytes
            self.peak.append((now, self.active))
            self._trim(now)
            return Slot(self, body_bytes), ""

    def _trim(self, now):
        while self.rejects and self.rejects[0][0] < now - REJECTS_KEPT:
            self.rejects.popleft()
        while self.peak and self.peak[0][0] < now - 3600:
            self.peak.popleft()

    def status(self):
        with self.lock:
            now = self.clock()
            self._trim(now)
            return {"active": self.active, "max_active": self.max_active, "peak_active_1h": max(
                        [n for _, n in self.peak] + [self.active]),
                    "body_mb": round(self.body / 1048576, 1), "max_body_mb": round(self.max_body / 1048576, 1),
                    "rejected_1h": dict(collections.Counter(r for _, r in self.rejects))}


class Slot:
    def __init__(self, capacity, body_bytes):
        self.capacity, self.body_bytes = capacity, body_bytes
        self.closed = False

    def grow(self, body_bytes):
        """The body turned out bigger than announced (chunked upload): account the rest."""
        extra = max(0, body_bytes - self.body_bytes)
        with self.capacity.lock:
            self.capacity.body += extra
        self.body_bytes += extra

    def close(self):
        with self.capacity.lock:
            if self.closed:
                return
            self.closed = True
            self.capacity.active -= 1
            self.capacity.body -= self.body_bytes
