"""The gate: per-pool hold queue and release decisions (DESIGN §4.3, §4.5, §11.2).

Every request asks `admit()` before it is forwarded. Under every limit it passes at once.
Otherwise it is held in its pool's FIFO: a thread waits on the pool's Condition and re-checks
`decide()` whenever something changes (a departure, a request finishing, an override, a reload)
or a deadline it computed comes up. Holding sends no bytes to the client, so a request held
too long can still get a clean 503.
"""
import collections
import itertools
import threading
import time
from dataclasses import dataclass, field

from .fifo import gap as fifo_gap, step_for
from .limits import Counts
from .phase import Phase
from .window import BUCKET, Counters

WAITS_KEPT = 3600  # seconds of hold times kept for the p50 / max in /status


@dataclass
class Ticket:
    """One request's pass through the gate."""
    pool: str
    model: str
    arrival: float
    result: str = "pass"        # pass | held | released_by_cap | rejected | shadow_would_hold | gate_error
    held: float = 0.0
    ratio: float = 0.0
    message: str = ""
    count_scopes: list = field(default_factory=list)
    seq: int = 0
    cancelled: bool = False

    @property
    def rejected(self):
        return self.result == "rejected"


class PoolState:
    def __init__(self, lock):
        self.cond = threading.Condition(lock)
        self.queue = collections.deque()
        self.last_departure = None
        self.phase = None
        self.counts = Counts()
        self.override = None            # {"action": "lift" | "limit", "value": float, "until": ts}
        self.shown = ("open", None)     # last state reported as an event: (state, step `at`)
        self.waits = collections.deque()  # (t, seconds held) of requests that waited


class Gate:
    def __init__(self, settings, counters=None, clock=time.time, on_event=None):
        self.lock = threading.RLock()
        self.settings = settings
        self.counters = counters or Counters()
        self.clock = clock
        self.on_event = on_event or (lambda **kw: None)
        self.pools = {}
        self.closing = False
        self.errors = collections.deque()  # times of internal errors
        self.seq = itertools.count(1)

    # ------------------------------------------------------------ configuration

    def state(self, pool):
        st = self.pools.get(pool)
        if st is None:
            st = self.pools[pool] = PoolState(self.lock)
        return st

    def reload(self, settings):
        with self.lock:
            self.settings = settings
            for name in [n for n in self.pools if n not in settings.pools and not self.pools[n].queue]:
                del self.pools[name]
            for st in self.pools.values():
                st.phase = None
                st.cond.notify_all()

    def set_override(self, pool, action, value, until):
        with self.lock:
            st = self.state(pool)
            st.override = None if action == "clear" else {"action": action, "value": value, "until": until}
            st.cond.notify_all()
        self.on_event(pool=pool, event="override", detail={"action": action, "value": value, "until": until})

    def shutdown(self):
        """Stop admitting: every held request gets a 503 now; streams already running finish."""
        with self.lock:
            self.closing = True
            for st in self.pools.values():
                st.cond.notify_all()

    # ------------------------------------------------------------ measurement

    def scopes(self, pool, model):
        """Counter scopes a request charges: the whole pool plus each matching model-scoped cap."""
        p = self.settings.pools.get(pool)
        out = ["*"]
        for r in (p.rules if p else []):
            if r.kind == "hourly_cap" and r.scope_model and r.applies(model) and r.scope_key not in out:
                out.append(r.scope_key)
        return out

    def charge(self, pool, model, amount, t=None):
        now = self.clock()
        self.counters.add(pool, self.scopes(pool, model), now if t is None else t, amount, now)

    def _override(self, st, now):
        o = st.override
        if o and o.get("until") and now >= o["until"]:
            st.override = o = None
        return o

    def money(self, pool, model, now):
        """(ratio, rule, limit, spent) of the tightest enforcing hourly_cap; ratio 0 when none."""
        p = self.settings.pools.get(pool)
        st = self.state(pool)
        o = self._override(st, now)
        if o and o["action"] == "lift":
            return 0.0, None, None, self.counters.spent(pool, "*", now)
        best = (0.0, None, None, self.counters.spent(pool, "*", now))
        rules = [r for r in (p.rules if p else []) if r.kind == "hourly_cap" and not r.shadow and r.applies(model)]
        if o and o["action"] == "limit" and not any(r.scope_model is None for r in rules):
            from ..settings import Rule
            rules.append(Rule(kind="hourly_cap", limit=o["value"]))
        for r in rules:
            limit = o["value"] if (o and o["action"] == "limit" and r.scope_model is None) else r.limit
            spent = self.counters.spent(pool, r.scope_key, now)
            ratio = spent / limit if limit else 0.0
            if best[1] is None or ratio > best[0]:
                best = (ratio, r, limit, spent)
        return best

    def _shadow_money(self, pool, model, now):
        p = self.settings.pools.get(pool)
        for r in (p.rules if p else []):
            if r.kind == "hourly_cap" and r.shadow and r.applies(model) and r.limit:
                if self.counters.spent(pool, r.scope_key, now) >= r.limit:
                    return True
        return False

    def _count_rules(self, pool, model):
        p = self.settings.pools.get(pool)
        return [r for r in (p.rules if p else []) if r.kind in ("concurrency", "rpm") and r.applies(model)]

    def _count_block(self, st, rules, now):
        """(enforcing rule that blocks or None, shadow rule that would block or None, retry at)."""
        block = shadow = None
        retry = None
        for r in rules:
            if r.kind == "concurrency":
                over = st.counts.in_flight[r.scope_key] >= r.max
            else:
                n, free_at = st.counts.rpm(r.scope_key, now)
                over = n >= r.max
                if over:
                    retry = free_at if retry is None else min(retry, free_at)
            if over:
                if r.shadow:
                    shadow = shadow or r
                elif block is None:
                    block = r
        return block, shadow, retry

    # ------------------------------------------------------------ decisions

    def decide(self, st, t, now, model):
        """("go" | "cap" | "reject" | "wait", wake_at, message) for ticket t at time now."""
        s = self.settings
        if self.closing:
            return "reject", None, "lingqu is shutting down"
        waited = now - t.arrival
        rules = self._count_rules(t.pool, model)
        block, _, retry = self._count_block(st, rules, now)
        if block is not None:
            if block.on_exceed == "reject":
                return "reject", None, f"{block.kind} limit {block.max} reached"
            if waited >= s.max_hold:
                return "reject", None, f"{block.kind} limit {block.max} reached; waited {waited:.0f} s"
            return "wait", min(x for x in (retry, t.arrival + s.max_hold) if x is not None), ""

        ratio, rule, limit, spent = self.money(t.pool, model, now)
        t.ratio = ratio
        if ratio < 1 or rule is None:
            return "go", None, ""
        why = f"pool {t.pool} spent ${spent:.2f} of ${limit:g} in the last hour"
        if rule.on_exceed == "reject":
            return "reject", None, why
        if waited >= s.hold_cap_cached and s.hold_cap_cached <= s.max_hold:
            return "cap", None, ""
        if waited >= s.max_hold:
            return "reject", None, f"{why}; waited {waited:.0f} s"
        deadlines = [t.arrival + min(s.hold_cap_cached, s.max_hold), (int(now // BUCKET) + 1) * BUCKET]
        if rule.release == "phase":
            if st.phase is None:
                st.phase = Phase(s.phase)
            st.phase.tick(now, ratio)
            if st.phase.passing():
                return "go", None, ""
            return "wait", min(deadlines + [st.phase.until]), ""
        head = st.queue[0] if st.queue else None
        if head is not None and head is not t:
            return "wait", min(deadlines), ""
        ready = (st.last_departure or 0) + fifo_gap(s.fifo, ratio)
        if now >= ready:
            return "go", None, ""
        return "wait", min(deadlines + [ready]), ""

    def admit(self, pool, model, gone=None):
        """Block until the request may go; returns its Ticket (check `.rejected`).
        gone: optional callable, true once the client has hung up (polled while held)."""
        now = self.clock()
        t = Ticket(pool=pool, model=model, arrival=now, seq=next(self.seq))
        try:
            return self._admit(t, gone)
        except Exception as e:  # a bug in the gate must not take the traffic down (DESIGN §4.7)
            with self.lock:
                self.errors.append(self.clock())
                st = self.pools.get(pool)
                if st and t in st.queue:
                    st.queue.remove(t)
                    st.cond.notify_all()
            t.message = f"gate error: {e}"
            if self.settings.fail_mode == "closed":
                t.result = "rejected"
            else:
                t.result = "gate_error"
                t.count_scopes = []
            self.on_event(pool=pool, event="gate_error", detail={"error": repr(e)})
            return t

    def _admit(self, t, gone):
        with self.lock:
            st = self.state(t.pool)
            s = self.settings
            now = self.clock()
            verdict, wake, msg = self.decide(st, t, now, t.model)
            queued_seqs = ()
            if verdict == "wait":
                queued_seqs = (t.seq,)
                if len(st.queue) >= s.max_queue:
                    verdict, msg = "reject", f"pool {t.pool}: {len(st.queue)} requests already held"
                else:
                    st.queue.append(t)
                    self._observe(t.pool, st, now)
                    while True:
                        verdict, wake, msg = self.decide(st, t, now, t.model)
                        if verdict != "wait" or t.cancelled:
                            break
                        st.cond.wait(max(0.01, min(wake, now + 2) - now) if wake else 2)
                        now = self.clock()
                        if gone is not None and gone():
                            t.cancelled = True
                    st.queue.remove(t)
                    st.cond.notify_all()
                    if t.cancelled:
                        verdict, msg = "reject", "client went away while held"
            now = self.clock()
            t.held = now - t.arrival
            if verdict == "reject":
                t.result, t.message = "rejected", msg
                self.on_event(pool=t.pool, event="rejected", ratio=t.ratio, detail={"message": msg, "held": round(t.held, 1)})
            else:
                queued = t.seq in queued_seqs
                t.result = "released_by_cap" if verdict == "cap" else ("held" if queued else "pass")
                if t.result == "pass" and self._would_hold(st, t, now):
                    t.result = "shadow_would_hold"
                st.last_departure = now
                t.count_scopes = sorted({r.scope_key for r in self._count_rules(t.pool, t.model)} | {"*"})
                st.counts.start(t.count_scopes, now)
                st.cond.notify_all()
            if queued_seqs:
                st.waits.append((now, t.held))
                while st.waits and st.waits[0][0] < now - WAITS_KEPT:
                    st.waits.popleft()
            self._observe(t.pool, st, now)
            return t

    def _would_hold(self, st, t, now):
        _, shadow, _ = self._count_block(st, self._count_rules(t.pool, t.model), now)
        return shadow is not None or self._shadow_money(t.pool, t.model, now)

    def finish(self, t):
        """The request's response ended (or failed): free its concurrency slot."""
        if not t.count_scopes:
            return
        with self.lock:
            st = self.state(t.pool)
            st.counts.finish(t.count_scopes)
            t.count_scopes = []
            st.cond.notify_all()

    def cancel(self, t):
        with self.lock:
            t.cancelled = True
            st = self.pools.get(t.pool)
            if st:
                st.cond.notify_all()

    # ------------------------------------------------------------ observation

    def _observe(self, pool, st, now):
        """Emit a gate event when the pool enters, changes step in or leaves throttling."""
        ratio, rule, limit, spent = self.money(pool, "", now)
        if st.override and st.override["action"] == "lift":
            shown = ("override", None)
        elif rule is not None and ratio >= 1:
            steps = self.settings.phase if rule.release == "phase" else self.settings.fifo
            step = step_for(steps, ratio)
            shown = ("throttled", step[0] if step else None)
        else:
            shown = ("open", None)
        if shown == st.shown:
            return
        before, st.shown = st.shown, shown
        if shown[0] == "throttled" and before[0] != "throttled":
            event = "throttle_start"
        elif shown[0] == "throttled":
            event = "step_change"
        elif before[0] == "throttled":
            event = "throttle_end"
        else:
            return
        self.on_event(pool=pool, event=event, ratio=ratio, spent=spent, limit=limit,
                      step={"at": shown[1]} if shown[1] else None)

    def tick(self):
        """Called periodically: report throttles that ended with no traffic, wake sleepers."""
        with self.lock:
            now = self.clock()
            for name, st in list(self.pools.items()):
                self._observe(name, st, now)
                if st.queue:
                    st.cond.notify_all()

    def pool_status(self, pool, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            st = self.state(pool)
            ratio, rule, limit, spent = self.money(pool, "", now)
            o = self._override(st, now)
            if o and o["action"] == "lift":
                state = "override"
            elif rule is not None and ratio >= 1:
                state = "queuing"
            else:
                state = "open"
            step = None
            next_departure = None
            if rule is not None and ratio >= 1:
                if rule.release == "phase":
                    s = step_for(self.settings.phase, ratio)
                    step = {"at": s[0], "off": s[1], "on": s[2]} if s else None
                    if st.phase:
                        step = dict(step or {}, phase=st.phase.phase, until=round(st.phase.until))
                else:
                    s = step_for(self.settings.fifo, ratio)
                    step = {"at": s[0], "gap": s[1]} if s else None
                    next_departure = round((st.last_departure or now) + (s[1] if s else 0))
            waits = sorted(w for t, w in st.waits if t >= now - 3600)
            return {
                "spent_1h": round(spent, 4), "limit": limit, "ratio": round(ratio, 4) if limit else None,
                "state": state, "step": step, "release": rule.release if rule else None,
                "next_departure": next_departure,
                "clear_at": self.counters.clear_at(pool, rule.scope_key, now, limit) if rule and limit else None,
                "held": len(st.queue), "in_flight": st.counts.in_flight["*"],
                "wait_p50_1h": round(waits[len(waits) // 2], 1) if waits else None,
                "wait_max_1h": round(waits[-1], 1) if waits else None,
                "override": o,
            }

    def held_total(self):
        with self.lock:
            return sum(len(st.queue) for st in self.pools.values())

    def errors_1h(self):
        with self.lock:
            now = self.clock()
            while self.errors and self.errors[0] < now - 3600:
                self.errors.popleft()
            return len(self.errors)
