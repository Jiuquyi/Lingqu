"""Batched ledger writer and rolling-window recovery (DESIGN §4.7, §9, §11.2).

The in-memory window is the authority; the database only records. A request never waits for a
write: rows go into a bounded queue that a background thread flushes every second or every 100
rows. When the queue is full the oldest row is dropped and counted.
"""
import collections
import threading
import time

from . import db
from .models import BudgetSnapshot, CounterSnapshot, CycleSpendSnapshot, GateEvent, Ledger, UpstreamQuota
from .gate.window import HISTORY_SPAN

MAX_PENDING = 10000
BATCH = 100


class LedgerWriter:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.Lock()
        self.rows = collections.deque()
        self.events = collections.deque()
        self.observations = collections.deque()
        self.dropped = 0
        self.oldest_pending = None
        self.wake = threading.Event()

    def add(self, row):
        with self.lock:
            if len(self.rows) >= MAX_PENDING:
                self.rows.popleft()
                self.dropped += 1
            if self.oldest_pending is None:
                self.oldest_pending = self.clock()
            self.rows.append(row)
            if len(self.rows) >= BATCH:
                self.wake.set()

    def add_event(self, row):
        with self.lock:
            if len(self.events) >= MAX_PENDING:
                self.events.popleft()
            self.events.append(row)
            if self.oldest_pending is None:
                self.oldest_pending = self.clock()

    def lag(self):
        with self.lock:
            return round(self.clock() - self.oldest_pending, 1) if self.oldest_pending is not None else 0

    def add_observation(self, kind, row):
        with self.lock:
            if len(self.observations) >= MAX_PENDING:
                self.observations.popleft()
                self.dropped += 1
            self.observations.append((kind, row))
            if self.oldest_pending is None:
                self.oldest_pending = self.clock()

    def add_quota(self, row):
        self.add_observation("quota", row)

    def add_budget(self, row):
        fields = ("pool", "scope_key", "ts", "trigger", "remaining_r", "weight_now", "weight_rem", "s_rem", "h_value")
        self.add_observation("budget", dict({k: row[k] for k in fields}, detail={k: v for k, v in row.items() if k not in fields}))

    def flush(self):
        with self.lock:
            rows, self.rows = list(self.rows), collections.deque()
            events, self.events = list(self.events), collections.deque()
            observations, self.observations = list(self.observations), collections.deque()
            self.oldest_pending = None
        if not rows and not events and not observations:
            return 0
        try:
            with self.app.app_context():
                db.session.bulk_insert_mappings(Ledger, rows)
                db.session.bulk_insert_mappings(GateEvent, events)
                db.session.bulk_insert_mappings(UpstreamQuota, [row for kind, row in observations if kind == "quota"])
                db.session.bulk_insert_mappings(BudgetSnapshot, [row for kind, row in observations if kind == "budget"])
                db.session.commit()
        except Exception:
            with self.app.app_context():
                db.session.rollback()
            with self.lock:  # keep them for the next try, oldest first, within the bound
                self.rows.extendleft(reversed(rows[-MAX_PENDING:]))
                self.events.extendleft(reversed(events[-MAX_PENDING:]))
                self.observations.extendleft(reversed(observations[-MAX_PENDING:]))
                while len(self.observations) > MAX_PENDING:
                    self.observations.popleft()
                    self.dropped += 1
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
    with counters.lock:
        now = clock()
        snap = {k: w.dump() for k, w in counters.windows.items()}
        for h in counters.history.values():
            h.prune(now)
        history = {k: dict(h.amounts) for k, h in counters.history.items()}
    with app.app_context():
        CounterSnapshot.query.delete()
        CycleSpendSnapshot.query.delete()
        for (pool, scope), buckets in snap.items():
            db.session.add(CounterSnapshot(pool=pool, scope_key=scope, saved_at=now,
                                           buckets={str(k): v for k, v in buckets.items()}))
        for (pool, model), charges in history.items():
            db.session.add(CycleSpendSnapshot(pool=pool, model=model, saved_at=now,
                                               charges={str(k): v for k, v in charges.items()}))
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
            for r in sorted(rows, key=lambda row: row.ts + (row.held_ms or 0) / 1000):
                if r.gate_result != "rejected":
                    if r.session_tag:
                        gate.sessions.started(r.pool, r.session_tag, r.ts + (r.held_ms or 0) / 1000)
                    departure = r.ts + (r.held_ms or 0) / 1000
                    if departure > now - 60:
                        for pool in [r.pool] + (["@user/" + r.user_ref] if r.user_ref else []):
                            scopes = sorted({x.scope_key for x in gate._count_rules(pool, r.model_req or r.model_actual)} |
                                            {"*", "actual=" + (r.model_req or r.model_actual or "")})
                            counts = gate.state(pool).counts
                            counts.start(scopes, departure)
                            counts.finish(scopes)
                    if r.user_ref:
                        gate.sessions.started("@user/" + r.user_ref, r.session_tag, r.ts + (r.held_ms or 0) / 1000)
        for r in rows:
            model = r.model_req or r.model_actual
            first = r.eusd_input or 0
            start = r.counted_at or r.ts
            end = r.accounted_at or (r.ts + ((r.held_ms or 0) + (r.duration_ms or 0)) / 1000)
            if (start > since if saved_at is not None else start >= since) and first:
                gate.charge(r.pool, model, first, start, history=False, user=r.user_ref or "")
            if (end > since if saved_at is not None else end >= since) and (r.eusd or 0) - first > 0:
                gate.charge(r.pool, model, (r.eusd or 0) - first, end, history=False, user=r.user_ref or "")
            restored += 1
    restore_history(app, gate.counters, clock)
    return restored


def restore_history(app, counters, clock=time.time):
    now = clock()
    with counters.lock:
        counters.history.clear()
    with app.app_context():
        snaps = CycleSpendSnapshot.query.all()
        saved = {(s.pool, s.model): s.saved_at for s in snaps}
        for s in snaps:
            for ts, amount in s.charges.items():
                counters.add_history(s.pool, s.model, float(ts), amount, now)
        # Include newly added models that have no snapshot, and requests crossing a checkpoint.
        rows = Ledger.query.filter(Ledger.ts >= now - HISTORY_SPAN - 3600).yield_per(1000)
        for r in rows:
            model = r.model_req or r.model_actual or ""
            targets = [r.pool] + (["@user/" + r.user_ref] if r.user_ref else [])
            first = r.eusd_input or 0
            start = r.counted_at or r.ts
            end = r.accounted_at or (r.ts + ((r.held_ms or 0) + (r.duration_ms or 0)) / 1000)
            for pool in targets:
                cutoff = saved.get((pool, model), now - HISTORY_SPAN)
                if start > cutoff and first:
                    counters.add_history(pool, model, start, first, now)
                if end > cutoff and (r.eusd or 0) > first:
                    counters.add_history(pool, model, end, r.eusd - first, now)
