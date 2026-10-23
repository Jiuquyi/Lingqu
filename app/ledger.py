"""Batched ledger writer and rolling-window recovery (DESIGN §4.7, §9, §11.2).

The in-memory window is the authority; the database only records. A request never waits for a
write: entries go into bounded queues that a background thread flushes every second or every 100
rows. A full queue drops its oldest entry and counts it. Every entry keeps the time it was
queued, so the backlog age stays true while writes keep failing.

Ledger rows and meter events carry a request id and are inserted once: a batch retried after a
commit whose answer was lost does not write them twice.
"""
import collections
import importlib
import threading
import time

from . import db
from .models import BudgetSnapshot, CounterSnapshot, CycleSpendSnapshot, GateEvent, Ledger, MeterEvent, UpstreamQuota
from .gate.window import HISTORY_SPAN

MAX_PENDING = 10000
BATCH = 100
QUEUES = ("rows", "events", "observations", "meters")


def insert_once(model, rows, key):
    """Insert rows, skipping those whose unique key is already stored (SQLite, PostgreSQL)."""
    if not rows:
        return
    dialect = db.engine.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        db.session.bulk_insert_mappings(model, rows)
        return
    insert = importlib.import_module(f"sqlalchemy.dialects.{dialect}").insert
    # One statement per key set: column defaults apply only to keys a row leaves out.
    groups = collections.defaultdict(list)
    for row in rows:
        groups[tuple(sorted(row))].append(row)
    for group in groups.values():
        db.session.execute(insert(model.__table__).on_conflict_do_nothing(index_elements=key), group)


class LedgerWriter:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.Lock()
        self.queues = {k: collections.deque() for k in QUEUES}  # (queued at, entry)
        self.dropped_by = collections.Counter()
        self.writing_since = None   # queued time of the oldest entry in the write under way
        self.last_success = None
        self.failing_since = None   # first failure since the last successful write
        self.failures = 0           # failed writes in a row
        self.last_error = ""
        self.wake = threading.Event()

    @property
    def dropped(self):
        return sum(self.dropped_by.values())

    def _put(self, kind, entry):
        with self.lock:
            q = self.queues[kind]
            if len(q) >= MAX_PENDING:
                q.popleft()
                self.dropped_by[kind] += 1
            q.append((self.clock(), entry))
            if kind == "rows" and len(q) >= BATCH:
                self.wake.set()

    def add(self, row):
        self._put("rows", row)

    def add_event(self, row):
        self._put("events", row)

    def add_meter(self, row):
        self._put("meters", row)

    def add_observation(self, kind, row):
        self._put("observations", (kind, row))

    def add_quota(self, row):
        self.add_observation("quota", row)

    def add_budget(self, row):
        fields = ("pool", "scope_key", "ts", "trigger", "remaining_r", "weight_now", "weight_rem", "s_rem", "h_value")
        self.add_observation("budget", dict({k: row[k] for k in fields}, detail={k: v for k, v in row.items() if k not in fields}))

    def _oldest(self):
        times = [q[0][0] for q in self.queues.values() if q]
        if self.writing_since is not None:
            times.append(self.writing_since)
        return min(times, default=None)

    def lag(self):
        """Seconds the oldest unwritten entry has waited, a write under way included."""
        with self.lock:
            oldest = self._oldest()
            return round(self.clock() - oldest, 1) if oldest is not None else 0

    def metrics(self):
        with self.lock:
            now, oldest = self.clock(), self._oldest()
            return {"lag_sec": round(now - oldest, 1) if oldest is not None else 0,
                    "queued": {k: len(q) for k, q in self.queues.items()},
                    "dropped": dict(self.dropped_by), "dropped_total": sum(self.dropped_by.values()),
                    "last_success": self.last_success, "failures": self.failures,
                    "failing_sec": round(now - self.failing_since, 1) if self.failing_since is not None else 0,
                    "last_error": self.last_error or None}

    def flush(self):
        with self.lock:
            batch = {k: list(q) for k, q in self.queues.items()}
            for q in self.queues.values():
                q.clear()
            self.writing_since = self._oldest_of(batch)
        if self.writing_since is None:
            return 0
        entries = {k: [e for _, e in b] for k, b in batch.items()}
        try:
            with self.app.app_context():
                insert_once(Ledger, entries["rows"], ["request_id"])
                db.session.bulk_insert_mappings(GateEvent, entries["events"])
                db.session.bulk_insert_mappings(UpstreamQuota, [r for kind, r in entries["observations"] if kind == "quota"])
                db.session.bulk_insert_mappings(BudgetSnapshot, [r for kind, r in entries["observations"] if kind == "budget"])
                insert_once(MeterEvent, entries["meters"], ["request_id", "seq"])
                db.session.commit()
        except Exception as e:
            with self.app.app_context():
                db.session.rollback()
            with self.lock:  # keep them for the next try, oldest first, within the bound
                for kind, items in batch.items():
                    q = self.queues[kind]
                    q.extendleft(reversed(items))
                    while len(q) > MAX_PENDING:
                        q.popleft()
                        self.dropped_by[kind] += 1
                self.writing_since = None
                self.failures += 1
                if self.failing_since is None:
                    self.failing_since = self.clock()
                self.last_error = repr(e)[:300]
            raise
        with self.lock:
            self.writing_since = None
            self.failures, self.failing_since, self.last_success = 0, None, self.clock()
        return len(entries["rows"])

    @staticmethod
    def _oldest_of(batch):
        return min((b[0][0] for b in batch.values() if b), default=None)

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
        # Requests still running when the process stopped have no row; their input side does.
        for e in orphan_meters(since):
            if (e.ts > since if saved_at is not None else e.ts >= since) and e.amount:
                gate.charge(e.pool, e.model or "", e.amount, e.ts, history=False, user=e.user_ref or "")
                restored += 1
    restore_history(app, gate.counters, clock)
    return restored


def orphan_meters(since):
    """Meter events after since whose request never wrote its ledger row."""
    settled = db.session.query(Ledger.id).filter(Ledger.request_id == MeterEvent.request_id)
    return MeterEvent.query.filter(MeterEvent.ts >= since, ~settled.exists()).all()


def prune_meters(app, clock=time.time, keep=2 * 3600):
    """Drop meter events older than keep whose request has its ledger row; the row has it all.
    Orphans stay: they are the only record of what those requests used."""
    with app.app_context():
        settled = db.session.query(Ledger.id).filter(Ledger.request_id == MeterEvent.request_id)
        n = MeterEvent.query.filter(MeterEvent.ts < clock() - keep, settled.exists()).delete(synchronize_session=False)
        db.session.commit()
        return n


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
        for e in orphan_meters(now - HISTORY_SPAN):
            for pool in [e.pool] + (["@user/" + e.user_ref] if e.user_ref else []):
                if e.ts > saved.get((pool, e.model or ""), now - HISTORY_SPAN) and e.amount:
                    counters.add_history(pool, e.model or "", e.ts, e.amount, now)
