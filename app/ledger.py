"""Batched ledger writer and rolling-window recovery (DESIGN §4.7, §9, §11.2).

The in-memory window is the authority; the database only records. A request never waits for a
write: rows go into a bounded queue that a background thread flushes every second or every 100
rows. When the queue is full the oldest row is dropped and counted.
"""
import collections
import threading
import time

from . import db
from .models import CounterSnapshot, GateEvent, Ledger

MAX_PENDING = 10000
BATCH = 100


class LedgerWriter:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.Lock()
        self.rows = collections.deque()
        self.events = collections.deque()
        self.dropped = 0
        self.oldest_pending = None
        self.wake = threading.Event()

    def add(self, row):
        with self.lock:
            if len(self.rows) >= MAX_PENDING:
                self.rows.popleft()
                self.dropped += 1
            if not self.rows:
                self.oldest_pending = self.clock()
            self.rows.append(row)
            if len(self.rows) >= BATCH:
                self.wake.set()

    def add_event(self, row):
        with self.lock:
            if len(self.events) >= MAX_PENDING:
                self.events.popleft()
            self.events.append(row)

    def lag(self):
        with self.lock:
            return round(self.clock() - self.oldest_pending, 1) if self.rows and self.oldest_pending else 0

    def flush(self):
        with self.lock:
            rows, self.rows = list(self.rows), collections.deque()
            events, self.events = list(self.events), collections.deque()
            self.oldest_pending = None
        if not rows and not events:
            return 0
        try:
            with self.app.app_context():
                db.session.bulk_insert_mappings(Ledger, rows)
                db.session.bulk_insert_mappings(GateEvent, events)
                db.session.commit()
        except Exception:
            with self.app.app_context():
                db.session.rollback()
            with self.lock:  # keep them for the next try, oldest first, within the bound
                self.rows.extendleft(reversed(rows[-MAX_PENDING:]))
                self.events.extendleft(reversed(events[-MAX_PENDING:]))
                self.oldest_pending = self.oldest_pending or self.clock()
                while len(self.rows) > MAX_PENDING:
                    self.rows.popleft()
                    self.dropped += 1
            raise
        return len(rows)

    def run_once(self):
        self.wake.wait(1.0)
        self.wake.clear()
        self.flush()


def save_counters(app, counters, clock=time.time):
    now = clock()
    with counters.lock:
        snap = {k: w.dump() for k, w in counters.windows.items()}
    with app.app_context():
        CounterSnapshot.query.delete()
        for (pool, scope), buckets in snap.items():
            db.session.add(CounterSnapshot(pool=pool, scope_key=scope, saved_at=now,
                                           buckets={str(k): v for k, v in buckets.items()}))
        db.session.commit()


def restore_counters(app, gate, clock=time.time):
    """Rebuild the windows: last counter snapshot, then ledger rows after it (DESIGN §9).
    Rows are charged at the time they were counted, by the scopes of the current settings.
    Also tells the gate when each session last went upstream, so a session that was warm before
    the restart is not taken for a cold one."""
    now = clock()
    since = now - 3600
    restored = 0
    with app.app_context():
        snaps = CounterSnapshot.query.all()
        saved_at = max((s.saved_at for s in snaps), default=None)
        if saved_at is not None and saved_at > since:
            for s in snaps:
                for minute, amount in (s.buckets or {}).items():
                    gate.counters.add(s.pool, [s.scope_key], int(minute) * 60, amount, now)
            since = saved_at
        # A request is charged twice: its input when counted (message_start), the rest at its end.
        # since - 3600 is at least an hour back: far enough for every cache that can still be warm.
        rows = Ledger.query.filter(Ledger.ts >= since - 3600).all()
        with gate.lock:
            for r in rows:
                if r.session_tag and r.gate_result != "rejected":
                    gate.sessions.started(r.pool, r.session_tag, r.ts + (r.held_ms or 0) / 1000)
        for r in rows:
            model = r.model_actual or r.model_req
            first = r.eusd_input or 0
            start = r.counted_at or r.ts
            end = r.ts + ((r.held_ms or 0) + (r.duration_ms or 0)) / 1000
            if start >= since and first:
                gate.charge(r.pool, model, first, start)
            if end >= since and (r.eusd or 0) - first > 0:
                gate.charge(r.pool, model, (r.eusd or 0) - first, end)
            restored += 1
    return restored
