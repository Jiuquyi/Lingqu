"""Webhook alerts (DESIGN §6.4): generic JSON, the same kind of event at most once per 10 minutes.
Sent from a background thread; a failing webhook never affects anything else."""
import collections
import logging
import threading
import time

import httpx

DEDUP_SEC = 600
log = logging.getLogger("lingqu")


class Notifier:
    def __init__(self, webhook="", clock=time.time):
        self.webhook, self.clock = webhook, clock
        self.lock = threading.Lock()
        self.last = {}
        self.queue = collections.deque(maxlen=200)
        self.wake = threading.Event()

    def alert(self, kind, subject, message, **detail):
        """Queue an alert; kind + subject is the dedup key (e.g. "account_auth", "team-a/claude")."""
        now = self.clock()
        key = (kind, subject)
        with self.lock:
            if now - self.last.get(key, -DEDUP_SEC) < DEDUP_SEC:
                return False
            self.last[key] = now
            self.queue.append({"kind": kind, "subject": subject, "message": message, "ts": int(now),
                               "detail": detail})
        log.warning("alert %s %s: %s", kind, subject, message)
        self.wake.set()
        return True

    def forget(self, kind, subject):
        """Let the next alert of this kind and subject through at once."""
        with self.lock:
            self.last.pop((kind, subject), None)

    def run_once(self):
        self.wake.wait(5)
        self.wake.clear()
        while True:
            with self.lock:
                if not self.queue:
                    return
                item = self.queue.popleft()
            if not self.webhook:
                continue
            try:
                httpx.post(self.webhook, json={"source": "lingqu", **item}, timeout=10)
            except httpx.HTTPError as e:
                log.warning("webhook failed: %s", e)
