"""Redis-backed Gate using fenced, short transactions around the existing decision engine.

One namespaced JSON document is intentional: admission across a pool and a user is atomic.
No network forwarding or sleeping happens inside a transaction. Intended for the small
subscription gateways described in DESIGN, rather than a high-throughput Redis cluster.
"""
import collections
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict

from .hold import Gate, Ticket
from .phase import Phase
from .window import SpendHistory, Window

log = logging.getLogger("lingqu")


class SharedStateError(RuntimeError):
    """The transaction did not take effect."""


class CommitUnconfirmed(SharedStateError):
    """The commit was sent but its answer was lost: it may or may not have taken effect."""


def signature(settings):
    data = asdict(settings)
    data.pop("path", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


SAMPLES_KEPT = 2000  # transactions kept for the lock and state-size figures in /healthz


class SharedLock:
    """RLock/Condition interface with Redis fencing, including full release while waiting."""
    COMMIT = """
    if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
    redis.call('set', KEYS[2], ARGV[2])
    redis.call('set', KEYS[3], ARGV[3])
    return 1
    """

    def __init__(self, gate):
        self.gate = gate
        self.local = threading.RLock()
        self.context = threading.local()
        self.samples = collections.deque(maxlen=SAMPLES_KEPT)  # (t, wait s, hold s, state bytes)

    def metrics(self):
        """Lock wait and hold times and the state size: what decides whether Redis needs the
        hot / history split (DESIGN §11.2) before more instances are added."""
        samples = list(self.samples)
        now = time.time()
        waits, holds = [x[1] for x in samples], [x[2] for x in samples]
        ms = lambda v: round(v * 1000, 2) if v is not None else None
        return {"transactions": len(samples), "per_sec_60s": round(sum(1 for x in samples if x[0] > now - 60) / 60, 2),
                "wait_ms": {q: ms(percentile(waits, p)) for q, p in (("p50", .5), ("p95", .95), ("p99", .99))},
                "hold_ms": {q: ms(percentile(holds, p)) for q, p in (("p50", .5), ("p95", .95), ("p99", .99))},
                "state_bytes": samples[-1][3] if samples else None}

    def acquire(self, blocking=True, timeout=-1):
        if not self.local.acquire(blocking, timeout):
            return False
        depth = getattr(self.context, "depth", 0)
        if depth == 0:
            lock = self.gate.redis.lock(self.gate.prefix + ':lock', timeout=15, blocking_timeout=5)
            try:
                started = time.time()
                if not lock.acquire(blocking=blocking):
                    raise SharedStateError("shared gate is busy")
                self.context.remote = lock
                self.context.timing = (started, time.time())
                raw = self.gate.redis.get(self.gate.prefix + ':state')
                self.context.size = len(raw or b"")
                if raw:
                    data = json.loads(raw)
                    if data.get("schema") != 1:
                        raise SharedStateError("unsupported shared state schema")
                    if data['signature'] != self.gate.signature() and not getattr(self.context, 'reloading', False):
                        raise SharedStateError("cluster configuration differs; reload every instance")
                    self.gate.decode(data)
                elif self.gate.epoch is not None and not getattr(self.context, 'restoring', False):
                    # This instance has seen the state, so Redis lost it: an empty one would start
                    # every window and budget afresh. Refuse until one instance rebuilds it.
                    raise SharedStateError("shared state lost; waiting for it to be rebuilt from the ledger")
                self.gate.reap()
            except Exception:
                try:
                    if lock.owned():
                        lock.release()
                finally:
                    self.local.release()
                raise
        self.context.depth = depth + 1
        return True

    def release(self):
        depth = self.context.depth - 1
        self.context.depth = depth
        try:
            if depth == 0:
                lock = self.context.remote
                payload = json.dumps(self.gate.encode(), separators=(',', ':'), allow_nan=False)
                try:
                    committed = self.gate.redis.eval(self.COMMIT, 3, self.gate.prefix + ':lock',
                                                     self.gate.prefix + ':state', self.gate.prefix + ':signature',
                                                     lock.local.token, payload, self.gate.signature())
                except Exception as e:
                    try:  # best effort: the others need not wait for the lock to expire
                        lock.release()
                    except Exception:
                        pass
                    raise CommitUnconfirmed("shared gate commit unconfirmed") from e
                if not committed:
                    raise SharedStateError("shared gate transaction lease expired")
                started, acquired = self.context.timing
                self.samples.append((acquired, acquired - started, time.time() - acquired, len(payload)))
                try:
                    lock.release()
                except Exception as e:  # committed already; the lock expires on its own
                    log.warning("releasing the shared gate lock failed: %s", e)
        finally:
            self.local.release()

    def _is_owned(self):
        return bool(getattr(self.context, 'depth', 0))

    def _release_save(self):
        depth = self.context.depth
        for _ in range(depth):
            self.release()
        return depth

    def _acquire_restore(self, depth):
        for _ in range(depth):
            self.acquire()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class RedisGate(Gate):
    def __init__(self, settings, clock=time.time, on_event=None, client=None):
        super().__init__(settings, clock=clock, on_event=on_event)
        from redis import Redis
        self.redis = client if client is not None else Redis.from_url(settings.redis['url'],
            socket_timeout=settings.redis['socket_timeout'], socket_connect_timeout=settings.redis['socket_timeout'])
        self.prefix = '{' + settings.redis['namespace'] + '}:gate'
        self.sequence = 0
        self.active = {}
        self.registry = {}  # identity must survive a Condition releasing and reacquiring its lock
        self.epoch = None   # id of the shared state this instance has seen; set when it is built
        self._signature = (None, None)
        self.lock = SharedLock(self)

    def signature(self):
        """The settings fingerprint, computed once per settings object."""
        if self._signature[0] is not self.settings:
            self._signature = (self.settings, signature(self.settings))
        return self._signature[1]

    def state_missing(self):
        return not self.redis.exists(self.prefix + ':state')

    def bootstrap(self, restore):
        """Make sure the shared state exists, building it from the database when it does not: on
        a cluster's first start, or after Redis lost it. One instance builds it under a Redis
        lock; the others stay not ready until it is there. restore(gate) replays the database
        into this gate inside the building transaction. Returns "present", "built" or "rebuilt"
        (the state was lost)."""
        if not self.state_missing():
            with self.lock:  # learn the epoch, so a later loss is told apart from a first start
                pass
            return "present"
        lost = self.epoch is not None
        guard = self.redis.lock(self.prefix + ':restore', timeout=600, blocking_timeout=0)
        if not guard.acquire(blocking=False):
            raise SharedStateError("another instance is rebuilding the shared state")
        try:
            if not self.state_missing():
                return "present"
            self.lock.context.restoring = True
            try:
                with self.lock:
                    self.clear()
                    self.epoch = uuid.uuid4().hex
                    restore(self)
            finally:
                self.lock.context.restoring = False
            return "rebuilt" if lost else "built"
        finally:
            try:
                guard.release()
            except Exception as e:
                log.warning("releasing the shared-state rebuild lock failed: %s", e)

    def clear(self):
        """Forget the local copy of the shared state (PoolState objects stay: threads hold them)."""
        for st in self.pools.values():
            st.queue.clear()
            st.counts = type(st.counts)()
            st.last_departure, st.phase, st.override = None, None, None
            st.waits.clear()
            st.shadow_hits.clear()
        self.counters.windows.clear()
        self.counters.history.clear()
        self.sessions.last.clear()
        self.budgets.quotas, self.budgets.calibration, self.budgets.cache = {}, {}, {}
        self.active, self.applied = {}, collections.OrderedDict()
        self.reservations.load({})

    def _prepare(self, ticket):
        self.sequence += 1
        for u in self.tickets(ticket):
            u.seq = self.sequence

    def _queued(self, ticket, now):
        super()._queued(ticket, now)
        self.registry[ticket.id] = ticket

    def _started(self, ticket):
        self.registry[ticket.id] = ticket
        self.active[ticket.id] = {'pool': ticket.pool, 'scopes': list(ticket.count_scopes),
                                  'until': self.clock() + self.settings.redis['lease']}

    def reap(self):
        now = self.clock()
        for tid, data in list(self.active.items()):
            if data['until'] <= now:
                self.state(data['pool']).counts.finish(data['scopes'])
                del self.active[tid]
        for st in self.pools.values():
            for ticket in list(st.queue):
                if ticket.lease_until <= now:
                    st.queue.remove(ticket)

    def renew(self):
        with self.lock:
            until = self.clock() + self.settings.redis['lease']
            for tid, ticket in self.registry.items():
                if tid in self.active and ticket.count_scopes:
                    self.active[tid]['until'] = until

    def finish(self, ticket):
        with self.lock:
            for u in self.tickets(ticket):
                active = self.active.pop(u.id, None)
                if active:
                    self.state(u.pool).counts.finish(active['scopes'])
                u.count_scopes = []
                self.registry.pop(u.id, None)

    def admit(self, pool, model, gone=None, session='', user=''):
        try:
            ticket = super().admit(pool, model, gone, session, user)
            if ticket.result == 'gate_error':
                ticket.result, ticket.message = 'rejected', 'shared admission unavailable'
            if ticket.rejected:
                for u in self.tickets(ticket):
                    self.registry.pop(u.id, None)
            return ticket
        except Exception:
            # Never create an independent in-process gate during an outage or a config mismatch.
            return Ticket(pool, model, self.clock(), result='rejected',
                          message='shared admission unavailable', user=user)

    def reload(self, settings):
        previous = self.settings
        self._signature = (None, None)   # the same object may come back changed
        self.lock.context.reloading = True
        try:
            super().reload(settings)
        except Exception:
            self.settings = previous
            self._signature = (None, None)
            self.budgets.configure(previous)
            raise
        finally:
            self.lock.context.reloading = False

    def encode(self):
        now = self.clock()
        states = {}
        for name, st in self.pools.items():
            st.counts.prune(now)
            states[name] = {
                'queue': [self.ticket_data(t) for t in st.queue],
                'last_departure': st.last_departure, 'override': st.override,
                'shown': st.shown, 'brake_shown': st.brake_shown,
                'phase': {k: getattr(st.phase, k) for k in ('phase', 'until', 'step')} if st.phase else None,
                'waits': [v for v in st.waits if v[0] >= now - 3600],
                'shadow_hits': [v for v in st.shadow_hits if v[0] >= now - 3600],
                'in_flight': dict(st.counts.in_flight),
                'starts': {k: [t for t in v if t > now - 60] for k, v in st.counts.starts.items()},
                'levels': {k: [v for v in q if v[0] >= now - 3600] for k, q in st.counts.levels.items()},
            }
        for h in self.counters.history.values():
            h.prune(now)
        for w in self.counters.windows.values():
            w.total(now)
        b = self.budgets
        return {'schema': 1, 'signature': self.signature(), 'epoch': self.epoch, 'states': states,
            'windows': [[*k, w.dump()] for k, w in self.counters.windows.items()],
            'history': [[*k, h.amounts] for k, h in self.counters.history.items()],
            'sessions': [[*k, t] for k, t in self.sessions.last.items()],
            'quotas': [[*k, v] for k, v in b.quotas.items()],
            'calibration': [[*k, v] for k, v in b.calibration.items()],
            'revision': b.revision, 'budget_cache': [[*k, v] for k, v in b.cache.items()],
            'active': self.active, 'sequence': self.sequence,
            'applied': [[k, *v] for k, v in self.applied.items()],
            'reservations': self.reservations.dump()}

    @staticmethod
    def ticket_data(ticket):
        return {k: v for k, v in vars(ticket).items() if k != 'user_ticket'}

    def decode(self, data):
        # Keep PoolState and local Ticket identities: waiting threads retain references to them.
        self.sequence = data["sequence"]
        self.epoch = data.get("epoch") or self.epoch
        for name, raw in data['states'].items():
            st = self.state(name)
            st.queue.clear()
            for raw_ticket in raw['queue']:
                tid = raw_ticket['id']
                t = self.registry.get(tid)
                if t is None:
                    t = Ticket(**raw_ticket)
                else:
                    for k, v in raw_ticket.items():
                        setattr(t, k, v)
                st.queue.append(t)
            for key in ('last_departure', 'override', 'brake_shown'):
                setattr(st, key, raw[key])
            st.shown = tuple(raw['shown'])
            st.waits = collections.deque(map(tuple, raw['waits']))
            st.shadow_hits = collections.deque(map(tuple, raw['shadow_hits']))
            st.phase = None
            if raw['phase']:
                st.phase = Phase(self.settings.phase)
                for k, v in raw['phase'].items():
                    setattr(st.phase, k, v)
            st.counts.in_flight = collections.Counter(raw['in_flight'])
            st.counts.starts = collections.defaultdict(collections.deque,
                {k: collections.deque(v) for k, v in raw['starts'].items()})
            st.counts.levels = collections.defaultdict(collections.deque,
                {k: collections.deque(map(tuple, v)) for k, v in raw['levels'].items()})
        self.counters.windows.clear()
        for pool, scope, values in data['windows']:
            w = Window()
            w.buckets = {int(k): v for k, v in values.items()}
            self.counters.windows[pool, scope] = w
        self.counters.history.clear()
        for pool, model, values in data['history']:
            h = SpendHistory()
            h.amounts = {float(k): v for k, v in values.items()}
            h.dirty = True
            self.counters.history[pool, model] = h
        self.sessions.last = collections.OrderedDict(((p, s), t) for p, s, t in data['sessions'])
        b = self.budgets
        b.quotas = {(p, c, w): v for p, c, w, v in data['quotas']}
        b.calibration = {(p, c, w): v for p, c, w, v in data['calibration']}
        b.revision = data['revision']
        b.cache = {}
        for p, scope, (sig, row) in data['budget_cache']:
            sig[4] = tuple(tuple(v) for v in sig[4])
            b.cache[p, scope] = (tuple(sig), row)
        self.active = data['active']
        self.applied = collections.OrderedDict((k, (taken, at)) for k, taken, at in data.get('applied', []))
        self.reservations.load(data.get('reservations'))

    def health(self):
        """Redis reachable, the shared state there and built from this instance's config; no
        transaction, so a health check neither waits for the gate lock nor rewrites the state."""
        try:
            self.redis.ping()
            missing = self.state_missing()
            sig = self.redis.get(self.prefix + ':signature')
        except Exception:
            return {'ok': False, 'backend': 'redis', 'error': 'shared admission unavailable'}
        out = {'ok': True, 'backend': 'redis', 'epoch': self.epoch, 'lock': self.lock.metrics()}
        if missing:
            out.update(ok=False, error='shared state missing; it is rebuilt from the ledger')
        elif sig is not None and sig.decode() != self.signature():
            out.update(ok=False, error='cluster configuration differs; reload every instance')
        return out
