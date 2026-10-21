"""The gate: per-pool hold queue and release decisions (DESIGN §4.3, §4.5, §11.2).

Every request asks `admit()` before it is forwarded. Under every limit it passes at once.
Otherwise it is held in its pool's queue: a thread waits on the pool's Condition and re-checks
`decide()` whenever something changes (a departure, a request finishing, an override, a reload)
or a deadline it computed comes up. Holding sends no bytes to the client, so a request held
too long can still get a clean 503.

Over the money limit the queue head leaves one gap after the previous departure, and any held
request leaves when its own cap runs out. The cap is `hold_cap_cached` for everyone, unless the
pool is session-aware (M3): then a warm request's cap ends before its prompt cache expires, a
cold request's is `hold_cap_cold`, and the head is the request whose cap ends first. With equal
caps that order is plain FIFO.
"""
import collections
import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field

from .fifo import gap as fifo_gap, step_for
from .budget import Budgets
from .limits import Counts
from .phase import Phase
from .sessions import CACHE_MARGIN, COLD, NONE, SessionClock
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
    session: str = ""
    cache: str = ""             # warm | cold | none: the session's prompt cache at arrival
    since: float | None = None  # seconds since the session's previous request went upstream
    waiting_for: str = ""       # while held: money | concurrency | rpm
    shadow: str = ""            # the rule kind a shadow rule would have held it for

    user: str = ""
    user_ticket: "Ticket | None" = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lease_until: float = 0.0

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
        self.shadow_hits = collections.deque()  # (t, rule kind) of requests a shadow rule would hold
        self.brake_shown = "normal"


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
        self.sessions = SessionClock()
        self.budgets = Budgets(settings, self.counters)

    # ------------------------------------------------------------ configuration

    def state(self, pool):
        st = self.pools.get(pool)
        if st is None:
            st = self.pools[pool] = PoolState(self.lock)
        return st

    def reload(self, settings):
        with self.lock:
            self.settings = settings
            self.budgets.configure(settings)
            for name in [n for n in self.pools if settings.pool_config(n) is None and not self.pools[n].queue]:
                del self.pools[name]
            for st in self.pools.values():
                st.phase = None
                st.cond.notify_all()

    def set_override(self, pool, action, value, until):
        with self.lock:
            st = self.state(pool)
            st.override = None if action == "clear" else {"action": action, "value": value, "until": until}
            self.budgets.invalidate("override")
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
        p = self.settings.pool_config(pool)
        out = ["*"]
        for r in (p.rules if p else []):
            if r.kind in ("hourly_cap", "hourly_budget", "cycle_cap") and r.scope_model and r.applies(model) and r.scope_key not in out:
                out.append(r.scope_key)
        return out

    def charge(self, pool, model, amount, t=None, history=True, user=""):
        with self.lock:
            now = self.clock()
            charged_at = now if t is None else t
            targets = [pool] + (["@user/" + user] if user else [])
            with self.counters.lock:
                for target in targets:
                    self.counters.add(target, self.scopes(target, model), charged_at, amount, now)
                    if history:
                        self.counters.add_history(target, model, charged_at, amount, now)
                    self.state(target).cond.notify_all()
            return charged_at

    def update_quota(self, pool, connection, windows, source="api", persist=None):
        with self.lock:
            self.budgets.update(pool, connection, windows, self.clock(), source, persist)
            st = self.state(pool)
            self._observe(pool, st, self.clock())
            st.cond.notify_all()

    def _hard_reason(self, pool, model, now, shadow=False):
        p = self.settings.pool_config(pool)
        rules = [r for r in (p.rules if p else []) if r.kind in ("hourly_budget", "cycle_cap")
                 and r.shadow == shadow and r.applies(model)]
        if rules and self.budgets.emergency(pool, now, model) == "reject":
            return "emergency brake: upstream quota used >= 98%"
        for r in rules:
            if r.kind == "cycle_cap" and self.budgets.cycle(pool, r, now)["used_cycle"] >= r.cycle_limit - 1e-9:
                return f"cycle_cap {r.cycle_limit:g} reached"
            if r.kind == "hourly_budget" and self.budgets.value(pool, r, now)["s_rem"] == 0:
                return "hourly_budget: short-window allowance exhausted"
        return ""

    def _override(self, st, now):
        o = st.override
        if o and o.get("until") and now >= o["until"]:
            st.override = o = None
        return o

    def money(self, pool, model, now):
        """(ratio, rule, limit, spent) of the tightest enforcing hourly_cap; ratio 0 when none."""
        p = self.settings.pool_config(pool)
        st = self.state(pool)
        o = self._override(st, now)
        protect = p and any(r.kind == "hourly_budget" and not r.shadow and r.applies(model) for r in p.rules)
        if o and o["action"] == "lift" and (not protect or self.budgets.emergency(pool, now, model) == "normal"):
            return 0.0, None, None, self.counters.spent(pool, "*", now)
        best = (0.0, None, None, self.counters.spent(pool, "*", now))
        rules = [r for r in (p.rules if p else []) if r.kind in ("hourly_cap", "hourly_budget") and not r.shadow and r.applies(model)]
        if o and o["action"] == "limit" and not any(r.scope_model is None for r in rules):
            from ..settings import Rule
            rules.append(Rule(kind="hourly_cap", limit=o["value"]))
        for r in rules:
            limit = self.budgets.value(pool, r, now)["h_value"] if r.kind == "hourly_budget" else r.limit
            if o and o["action"] == "limit" and r.scope_model is None:
                limit = o["value"]
                if r.kind == "hourly_budget":
                    budget = self.budgets.value(pool, r, now)
                    if budget["brake"] != "normal":
                        limit = min(limit, r.h_min)
                    if budget["s_rem"] is not None:
                        limit = min(limit, budget["s_rem"])
            spent = self._spent(pool, r, now)
            ratio = spent / limit if limit else max(1.0, spent / 1e-12)
            if best[1] is None or ratio > best[0]:
                best = (ratio, r, limit, spent)
        return best

    def _spent(self, pool, rule, now):
        if rule.kind == "hourly_budget":
            return self.counters.total_since(pool, rule.scope_model, (int(now // BUCKET) - 59) * BUCKET, now)
        if rule.scope_model:
            return self.counters.total_since(pool, rule.scope_model, (int(now // BUCKET) - 59) * BUCKET, now)
        return self.counters.spent(pool, rule.scope_key, now)

    def _shadow_money(self, pool, model, now):
        p = self.settings.pool_config(pool)
        for r in (p.rules if p else []):
            if r.kind in ("hourly_cap", "hourly_budget") and r.shadow and r.applies(model):
                limit = self.budgets.value(pool, r, now)["h_value"] if r.kind == "hourly_budget" else r.limit
                if self._spent(pool, r, now) >= limit:
                    return r.kind
        return ""

    def _count_rules(self, pool, model):
        p = self.settings.pool_config(pool)
        return [r for r in (p.rules if p else []) if r.kind in ("concurrency", "rpm") and r.applies(model)]

    def _in_flight(self, st, rule):
        if not rule.scope_model:
            return st.counts.in_flight["*"]
        return sum(n for key, n in st.counts.in_flight.items()
                   if key.startswith("actual=") and rule.applies(key[7:]))

    def _rpm(self, st, rule, now):
        if not rule.scope_model:
            return st.counts.rpm("*", now)
        scopes = [key for key in st.counts.starts if key.startswith("actual=") and rule.applies(key[7:])]
        values = [st.counts.rpm(key, now) for key in scopes]
        return sum(n for n, _ in values), min((at for n, at in values if n), default=now)

    def _count_block(self, st, rules, now, t=None):
        """(enforcing rule that blocks or None, shadow rule that would block or None, retry at).
        With a ticket, requests held ahead of it for the same concurrency slot count as taken:
        a slot that frees goes to whoever has waited for it longest."""
        block = shadow = None
        retry = None
        ahead = None
        for r in rules:
            if r.kind == "concurrency":
                n = self._in_flight(st, r)
                if t is not None and not r.shadow:
                    if ahead is None:
                        ahead = self._waiting_ahead(st, t)
                    n += sum(1 for keys in ahead if r.scope_key in keys)
                over = n >= r.max
            else:
                n, free_at = self._rpm(st, r, now)
                over = n >= r.max
                if over:
                    retry = free_at if retry is None else min(retry, free_at)
            if over:
                if r.shadow:
                    shadow = shadow or r
                elif block is None:
                    block = r
        return block, shadow, retry

    def _waiting_ahead(self, st, t):
        """Concurrency scopes of the requests held before t that wait only for a slot."""
        out = []
        for u in st.queue:
            if u is t:
                break
            if u.waiting_for == "concurrency":
                out.append({r.scope_key for r in self._count_rules(u.pool, u.model) if r.kind == "concurrency"})
        return out

    # ------------------------------------------------------------ session-aware queue (DESIGN §4.5)

    def session_aware(self, pool):
        p = self.settings.pool_config(pool)
        return any(r.kind in ("hourly_cap", "hourly_budget") and r.session_aware and not r.shadow for r in (p.rules if p else []))

    def cap(self, t, aware):
        """Seconds t may be held before it goes whatever the ratio."""
        s = self.settings
        if not aware or t.cache in ("", NONE):
            return s.hold_cap_cached
        if t.cache == COLD:
            # It rebuilds the cache anyway: let it wait longer, but never turn it away for waiting.
            return min(s.hold_cap_cold, s.max_hold)
        return max(0.0, min(s.hold_cap_cached, s.cache_ttl - CACHE_MARGIN - (t.since or 0.0)))

    def order(self, t, aware):
        """Queue order: arrival; session-aware, the end of each request's own cap first."""
        return (t.arrival + self.cap(t, aware), t.seq) if aware else (0.0, t.seq)

    # ------------------------------------------------------------ decisions

    def decide(self, st, t, now, model):
        """("go" | "cap" | "reject" | "wait", wake_at, message) for ticket t at time now.
        Money first, then the count limits: a request the money gate lets go still needs a slot."""
        s = self.settings
        if self.closing:
            return "reject", None, "lingqu is shutting down"
        reason = self._hard_reason(t.pool, model, now)
        if reason:
            return "reject", None, reason
        waited = now - t.arrival
        verdict, wake, msg = self._money_decision(st, t, now, model, waited)
        if verdict == "wait":
            t.waiting_for = "money"
        if verdict in ("reject", "wait"):
            return verdict, wake, msg
        block, _, retry = self._count_block(st, self._count_rules(t.pool, model), now, t)
        if block is not None:
            if block.on_exceed == "reject":
                return "reject", None, f"{block.kind} limit {block.max} reached"
            if waited >= s.max_hold:
                return "reject", None, f"{block.kind} limit {block.max} reached; waited {waited:.0f} s"
            t.waiting_for = block.kind
            return "wait", min(x for x in (retry, t.arrival + s.max_hold) if x is not None), ""
        return verdict, None, ""

    def _money_decision(self, st, t, now, model, waited):
        s = self.settings
        ratio, rule, limit, spent = self.money(t.pool, model, now)
        t.ratio = ratio
        if ratio < 1 or rule is None:
            return "go", None, ""
        why = f"pool {t.pool} spent ${spent:.2f} of ${limit:g} in the last hour"
        if rule.on_exceed == "reject":
            return "reject", None, why
        aware = self.session_aware(t.pool)
        cap = self.cap(t, aware)
        if waited >= cap and cap <= s.max_hold:
            return "cap", None, ""
        if waited >= s.max_hold:
            return "reject", None, f"{why}; waited {waited:.0f} s"
        deadlines = [t.arrival + min(cap, s.max_hold), (int(now // BUCKET) + 1) * BUCKET]
        if rule.release == "phase":
            if st.phase is None:
                st.phase = Phase(s.phase)
            st.phase.tick(now, ratio)
            if st.phase.passing():
                return "go", None, ""
            return "wait", min(deadlines + [st.phase.until]), ""
        head = min(st.queue, key=lambda u: self.order(u, aware)) if st.queue else None
        if head is not None and head is not t:
            return "wait", min(deadlines), ""
        ready = (st.last_departure or 0) + fifo_gap(s.fifo, ratio)
        if now >= ready:
            return "go", None, ""
        return "wait", min(deadlines + [ready]), ""

    def admit(self, pool, model, gone=None, session="", user=""):
        """Block until the request may go; returns its Ticket (check `.rejected`).
        gone: optional callable, true once the client has hung up (polled while held).
        session: the request's session (Claude Code session id or prompt_cache_key), or ''."""
        now = self.clock()
        t = Ticket(pool=pool, model=model, arrival=now, seq=next(self.seq), session=session or "", user=user)
        if user and user in self.settings.users:
            t.user_ticket = Ticket(pool="@user/" + user, model=model, arrival=now, seq=t.seq, session=session or "")
        try:
            return self._admit(t, gone)
        except Exception as e:  # a bug in the gate must not take the traffic down (DESIGN §4.7)
            with self.lock:
                self.errors.append(self.clock())
                for u in self.tickets(t):
                    st = self.pools.get(u.pool)
                    if st and u in st.queue:
                        st.queue.remove(u)
                        st.cond.notify_all()
                if self.settings.fail_mode != "closed":
                    self.sessions.started(pool, t.session, self.clock())
            t.message = f"gate error: {e}"
            if self.settings.fail_mode == "closed":
                t.result = "rejected"
            else:
                t.result = "gate_error"
                t.count_scopes = []
            self.on_event(pool=pool, event="gate_error", detail={"error": repr(e)})
            return t

    def _prepare(self, ticket):
        pass

    def _queued(self, ticket, now):
        ticket.lease_until = now + self.settings.redis.get("lease", 90)

    def _started(self, ticket):
        pass

    def tickets(self, t):
        return [t, t.user_ticket] if t.user_ticket else [t]

    def _decision(self, t, now):
        decisions = [self.decide(self.state(u.pool), u, now, u.model) for u in self.tickets(t)]
        rejected = next((d for d in decisions if d[0] == "reject"), None)
        if rejected:
            return rejected
        waits = [d for d in decisions if d[0] == "wait"]
        if waits:
            return "wait", min((d[1] for d in waits if d[1] is not None), default=now + 1), ""
        return ("cap" if any(d[0] == "cap" for d in decisions) else "go"), None, ""

    def _admit(self, t, gone):
        queued = False
        initialized = False
        while True:
            # A shared lock releases between polls, so another process can finish/charge/admit.
            with self.lock:
                now = self.clock()
                members = self.tickets(t)
                if not initialized:
                    self._prepare(t)
                    for u in members:
                        u.cache, u.since = self.sessions.classify(u.pool, u.session, now, self.settings.cache_ttl)
                    initialized = True
                verdict, wake, msg = self._decision(t, now)
                if queued and any(u not in self.state(u.pool).queue for u in members):
                    verdict, msg = "reject", "held request lease expired"
                if t.cancelled or (gone is not None and gone()):
                    verdict, msg = "reject", "client went away while held"
                if verdict == "wait" and not queued:
                    if any(len(self.state(u.pool).queue) >= self.settings.max_queue for u in members):
                        verdict, msg = "reject", "pool or user hold queue is full"
                    else:
                        for u in members:
                            self._queued(u, now)
                            self.state(u.pool).queue.append(u)
                        queued = True
                if verdict != "wait":
                    for u in members:
                        st = self.state(u.pool)
                        if u in st.queue:
                            st.queue.remove(u)
                        u.held = now - u.arrival
                        u.waiting_for = ""
                        if verdict == "reject":
                            u.result, u.message = "rejected", msg
                        else:
                            u.result = "released_by_cap" if verdict == "cap" else ("held" if queued else "pass")
                            u.shadow = self._would_hold(st, u, now)
                            if u.shadow:
                                if u.result == "pass":
                                    u.result = "shadow_would_hold"
                                st.shadow_hits.append((now, u.shadow))
                            st.last_departure = now
                            self.sessions.started(u.pool, u.session, now)
                            u.count_scopes = sorted({r.scope_key for r in self._count_rules(u.pool, u.model)} | {"*", "actual=" + u.model})
                            st.counts.start(u.count_scopes, now)
                            self._started(u)
                        if queued:
                            st.waits.append((now, u.held))
                        while st.waits and st.waits[0][0] < now - WAITS_KEPT:
                            st.waits.popleft()
                        st.cond.notify_all()
                        self._observe(u.pool, st, now)
                    if t.user_ticket and t.user_ticket.shadow and t.result == "pass":
                        t.result, t.shadow = "shadow_would_hold", "user:" + t.user_ticket.shadow
                    if verdict == "reject":
                        self.on_event(pool=t.pool, event="rejected", ratio=t.ratio,
                                      detail={"message": msg, "held": round(t.held, 1), "user": t.user})
                    return t
                for u in members:
                    self._queued(u, now)
                    self._observe(u.pool, self.state(u.pool), now)
                # Condition uses the shared lock's release/restore hooks in Redis mode.
                self.state(t.pool).cond.wait(max(0.01, min(wake or now + 0.25, now + 0.25) - now))

    def _would_hold(self, st, t, now):
        """The kind of the shadow rule that would have held t, or ''."""
        _, shadow, _ = self._count_block(st, self._count_rules(t.pool, t.model), now)
        if shadow is not None:
            return shadow.kind
        if self._hard_reason(t.pool, t.model, now, shadow=True):
            return "cycle_or_quota"
        return self._shadow_money(t.pool, t.model, now)

    def finish(self, t):
        with self.lock:
            for u in self.tickets(t):
                st = self.state(u.pool)
                st.counts.finish(u.count_scopes)
                u.count_scopes = []
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
        p = self.settings.pool_config(pool)
        if p:
            brake = self.budgets.emergency(pool, now)
            if brake != st.brake_shown:
                st.brake_shown = brake
                enforcing = any(r.kind in ("hourly_budget", "cycle_cap") and not r.shadow for r in p.rules)
                self.on_event(pool=pool, event="quota_" + ("recovered" if brake == "normal" else brake),
                              detail={"message": f"upstream quota brake: {brake}",
                                      "mode": "enforce" if enforcing else "shadow"})
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
            for name in list(self.settings.pools) + ["@user/" + n for n in self.settings.users]:
                self.state(name)
                self.budgets.status(name, now)
            for name, st in list(self.pools.items()):
                st.counts.prune(now)
                self._observe(name, st, now)
                if st.queue:
                    st.cond.notify_all()

    def pool_status(self, pool, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            st = self.state(pool)
            ratio, rule, limit, spent = self.money(pool, "", now)
            o = self._override(st, now)
            hard_reason = self._hard_reason(pool, "", now)
            if hard_reason:
                state = "rejected"
            elif o and o["action"] == "lift" and self.budgets.emergency(pool, now) == "normal":
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
                "rules": self.rule_status(pool, st, now),
                "spent_1h": round(spent, 4), "limit": limit, "ratio": round(ratio, 4) if limit else None,
                "state": state, "step": step, "release": rule.release if rule else None,
                "next_departure": next_departure,
                "clear_at": self.counters.clear_at(pool, rule.scope_key, now, limit) if rule and limit else None,
                "held": len(st.queue), "held_cold": sum(1 for u in st.queue if u.cache == COLD),
                "in_flight": st.counts.in_flight["*"],
                "wait_p50_1h": round(waits[len(waits) // 2], 1) if waits else None,
                "wait_max_1h": round(waits[-1], 1) if waits else None,
                "override": o,
                "sessions": {"aware": self.session_aware(pool),
                             "warm": self.sessions.active(pool, now, self.settings.cache_ttl)},
                "concurrency": self._concurrency_status(pool, st, now),
                "budget": self.budgets.status(pool, now) or None,
                "cycles": [dict(self.budgets.cycle(pool, r, now), scope_key=r.scope_key,
                                limit=r.cycle_limit, mode="shadow" if r.shadow else "enforce")
                           for r in self.settings.pool_config(pool).rules if r.kind == "cycle_cap"]
                           if self.settings.pool_config(pool) else [],
                "brake": self.budgets.emergency(pool, now) if self.settings.pool_config(pool) else "normal",
                "reject_reason": hard_reason or None,
            }

    def rule_status(self, pool, st, now):
        p = self.settings.pool_config(pool)
        rows = []
        for r in (p.rules if p else []):
            row = {"kind": r.kind, "scope": r.scope_key, "mode": "shadow" if r.shadow else "enforce",
                   "on_exceed": r.on_exceed, "release": r.release}
            if r.kind in ("hourly_cap", "hourly_budget"):
                row.update(spent=self._spent(pool, r, now),
                           limit=self.budgets.value(pool, r, now)["h_value"] if r.kind == "hourly_budget" else r.limit)
            elif r.kind == "cycle_cap":
                row.update(self.budgets.cycle(pool, r, now), limit=r.cycle_limit)
            elif r.kind == "concurrency":
                row.update(in_flight=self._in_flight(st, r), max=r.max)
            else:
                n, retry = self._rpm(st, r, now)
                row.update(requests=n, retry_at=retry, max=r.max)
            rows.append(row)
        return rows

    def _concurrency_status(self, pool, st, now):
        """Pool-wide concurrency: what runs now, the hour's peak, the limits, and how many requests
        a shadow rule would have held in the last hour (by rule kind) -- what to look at before
        switching a shadow rule to enforce."""
        p = self.settings.pool_config(pool)
        rules = [r for r in (p.rules if p else []) if r.kind == "concurrency" and r.scope_model is None]
        while st.shadow_hits and st.shadow_hits[0][0] < now - 3600:
            st.shadow_hits.popleft()
        would = collections.Counter(kind for _, kind in st.shadow_hits)
        return {"in_flight": st.counts.in_flight["*"], "peak_1h": st.counts.peak("*", now),
                "max": min((r.max for r in rules if not r.shadow), default=None),
                "shadow_max": min((r.max for r in rules if r.shadow), default=None),
                "would_hold_1h": dict(would)}

    def held_total(self):
        with self.lock:
            return sum(len(st.queue) for name, st in self.pools.items() if not name.startswith("@user/"))

    def errors_1h(self):
        with self.lock:
            now = self.clock()
            while self.errors and self.errors[0] < now - 3600:
                self.errors.popleft()
            return len(self.errors)
