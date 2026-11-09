"""The runtime of a Lingqu worker with optional Redis admission coordination (DESIGN §11.2): settings, gate, prices,
ledger writer, alerts and the dashboard gate, plus the background threads that keep them."""
import collections
import logging
import threading
import time

import httpx

from . import settings as settings_mod
from .capacity import Capacity
from .dashboard.exposure import Exposure
from .gate.hold import APPLIED_KEPT, Gate
from .gate.shared import CommitUnconfirmed
from .gate.window import Counters
from .ledger import LedgerWriter, prune_meters, restore_counters, save_counters
from .observe.notify import Notifier
from .pricing.sync import Pricing
from .quota import QuotaCollector
from .manager import Manager
from .usage.service import Usage

log = logging.getLogger("lingqu")
VERSION = "0.9.4"
# Error classes that need a person, alerted when a pool sees them several times in a row.
ALERT_CLASSES = {"account_auth": "upstream account authorization failed; re-authorize it in 9router",
                 "router_unreachable": "cannot connect to 9router"}
# Error classes that say nothing about the upstream: they neither extend nor end a streak.
CLIENT_CLASSES = ("client_error", "client_closed")
MAX_UNSETTLED = 10000   # charges waiting for the gate
QUALITY_KEPT = 3600     # seconds of settled requests kept for the usage-quality figures


class Runtime:
    def __init__(self, app, settings, clock=time.time):
        self.app, self.clock = app, clock
        self.settings = settings
        self.notifier = Notifier(settings.webhook, clock=clock)
        self.ledger = LedgerWriter(app, clock=clock)
        self.capacity = Capacity(settings, clock=clock)
        if settings.redis:
            from .gate.shared import RedisGate
            self.gate = RedisGate(settings, clock=clock, on_event=self._gate_event)
        else:
            self.gate = Gate(settings, Counters(), clock=clock, on_event=self._gate_event)
        self.pricing = Pricing(app, settings, clock=clock)
        self.pricing.on_unpriced = self._unpriced
        self.gate.budgets.on_snapshot = self.ledger.add_budget
        self.quota = QuotaCollector(self)
        self.manager = Manager(self)
        self._share_sessions()
        self.exposure = Exposure(app, settings, clock=clock, on_change=self._exposure_change)
        self.clients = {}
        self.client_lock = threading.Lock()
        self.heartbeats = {}
        self.threads = []
        self.stop = threading.Event()
        self.last_errors = {}       # pool -> {"class", "at", "count"}
        self.started_at = clock()
        self.metering_lock = threading.Lock()
        self.unsettled = collections.deque()   # charges the gate did not take, oldest first
        self.charges = collections.Counter()   # failed | recovered | lost
        self.quality = collections.deque()     # (t, pool, protocol, usage_source, eusd, default price)
        self.recovery = None                   # set by recover(): None in tests and the CLI
        self.capacity_event_at = 0.0
        self.pruned_at = clock()
        self.usage = Usage(self)

    def _share_sessions(self):
        sessions = [self.pricing.session] + list(self.quota.sessions.values()) + [
            adapter.session for adapter in self.manager.adapters.values()]
        for session in sessions:
            if session is not None:
                session.shared = self.settings.redis or None

    # ------------------------------------------------------------ upstream clients

    def client(self, upstream):
        """One pooled httpx client per 9router (connect 10 s, idle 600 s between chunks)."""
        with self.client_lock:
            c = self.clients.get(upstream)
            if c is None:
                c = self.clients[upstream] = httpx.Client(
                    timeout=httpx.Timeout(connect=10, read=600, write=600, pool=10),
                    limits=httpx.Limits(max_connections=256, max_keepalive_connections=32),
                    follow_redirects=False, trust_env=False)
            return c

    # ------------------------------------------------------------ events

    def _gate_event(self, pool, event, ratio=None, spent=None, limit=None, step=None, detail=None):
        now = self.clock()
        self.ledger.add_event({"ts": now, "pool": pool, "event": event, "ratio": ratio, "spent_1h": spent,
                               "limit_value": limit, "step": step, "detail": detail})
        if event in ("throttle_start", "throttle_end", "step_change"):
            log.info("pool %s: %s ratio=%.2f spent=$%.2f limit=$%s step=%s", pool, event, ratio or 0,
                     spent or 0, limit, step)
        if event in ("throttle_start", "throttle_end") and self.settings.notify_throttle:
            self.notifier.alert(event, pool, f"pool {pool}: {event.replace('_', ' ')}, "
                                             f"${spent or 0:.2f} of ${limit} in the last hour")
        elif event == "rejected":
            self.notifier.alert("hold_timeout", pool, f"pool {pool}: request rejected: {(detail or {}).get('message')}")
        elif event == "gate_error":
            self.notifier.alert("gate_error", pool, f"gate error in pool {pool}: {(detail or {}).get('error')}")
        elif event in ("quota_minimum", "quota_reject", "quota_recovered"):
            self.notifier.alert(event, pool, f"pool {pool}: {(detail or {}).get('message')} "
                                           f"({(detail or {}).get('mode')})")

    def _unpriced(self, model, kind):
        self.notifier.alert("unpriced_model", model, f"no price for model {model}; using the {kind} default price")

    def _exposure_change(self, upstream, action, state, until, actor, source):
        self.ledger.add_event({"ts": self.clock(), "pool": None, "event": f"dashboard_{action}",
                               "detail": {"upstream": upstream, "state": state, "until": until, "actor": actor,
                                          "source": source}})
        if state == "persistent" and action == "open":
            self.notifier.alert("dashboard_persistent", upstream,
                                f"9router dashboard of {upstream} kept open by {actor}")

    # ------------------------------------------------------------ metering

    def charge_failed(self, event, error):
        """A metered charge the gate did not take: kept and retried under the same key. A commit
        whose answer was lost may have taken effect, so it is retried only while the gate still
        remembers its key; after that it is counted as lost (the ledger row still has it)."""
        lost = None
        with self.metering_lock:
            self.charges["failed"] += 1
            if len(self.unsettled) >= MAX_UNSETTLED:
                lost = self.unsettled.popleft()
            self.unsettled.append(dict(event, uncertain=isinstance(error, CommitUnconfirmed)))
        log.warning("pool %s: charge %s:%s of $%.4f not taken by the gate: %s", event["pool"],
                    event["request_id"], event["seq"], event["amount"], error)
        self.notifier.alert("charge_failed", event["pool"], f"pool {event['pool']}: metering could not reach the gate "
                                                           f"(charges are kept and retried): {error}")
        if lost:
            self._charge_lost(lost, "too many charges waiting")

    def _charge_lost(self, event, why):
        with self.metering_lock:
            self.charges["lost"] += 1
        self.ledger.add_event({"ts": self.clock(), "pool": event["pool"], "event": "charge_lost",
                               "detail": {"message": f"${event['amount']:.4f} not counted in the window: {why}",
                                          "request_id": event["request_id"], "seq": event["seq"]}})
        self.notifier.alert("charge_lost", event["pool"], f"pool {event['pool']}: ${event['amount']:.4f} of a "
                                                          f"request was not counted in the window: {why}")

    def retry_charges(self):
        """Give the gate the charges it did not take, oldest first; stop at the first failure."""
        with self.metering_lock:
            items, self.unsettled = list(self.unsettled), collections.deque()
        for i, ev in enumerate(items):
            if ev["uncertain"] and self.clock() - ev["ts"] > APPLIED_KEPT - 60:
                self._charge_lost(ev, "its commit went unconfirmed and can no longer be told apart from a retry")
                continue
            try:
                self.gate.charge(ev["pool"], ev["model"], ev["amount"], t=ev["ts"], user=ev["user_ref"] or "",
                                 key=f"{ev['request_id']}:{ev['seq']}")
            except Exception as e:
                ev["uncertain"] = ev["uncertain"] or isinstance(e, CommitUnconfirmed)
                with self.metering_lock:
                    self.unsettled.extendleft(reversed(items[i:]))
                return
            with self.metering_lock:
                self.charges["recovered"] += 1

    def metered(self, row):
        """A settled metered response: usage-quality figures and the reservation estimates."""
        if row.get("usage_source") == "upstream" and row.get("eusd_input"):
            with self.gate.lock:
                self.gate.reservations.observe(row["pool"], row.get("cache_state"), row["eusd_input"])
        now = self.clock()
        with self.metering_lock:
            self.quality.append((now, row["pool"], row.get("protocol"), row.get("usage_source"), row.get("eusd") or 0.0,
                                 str(row.get("price_ref") or "").startswith("default:")))
            while self.quality and self.quality[0][0] < now - QUALITY_KEPT:
                self.quality.popleft()

    def metering_status(self):
        """Pending charges, and per pool / protocol where the last hour's usage came from."""
        now = self.clock()
        with self.metering_lock:
            pending = list(self.unsettled)
            rows = [q for q in self.quality if q[0] >= now - QUALITY_KEPT]
            charges = dict(self.charges)
        by = {}
        for _, pool, protocol, source, eusd, default in rows:
            b = by.setdefault(f"{pool} {protocol}", {"pool": pool, "protocol": protocol, "requests": 0, "eusd": 0.0,
                                                    "sources": collections.Counter(), "eusd_by_source": collections.Counter(),
                                                    "default_price": 0, "default_price_eusd": 0.0})
            b["requests"] += 1
            b["eusd"] += eusd
            b["sources"][source] += 1
            b["eusd_by_source"][source] += eusd
            if default:
                b["default_price"] += 1
                b["default_price_eusd"] += eusd
        quality = [dict(b, eusd=round(b["eusd"], 4), sources=dict(b["sources"]),
                        eusd_by_source={k: round(v, 4) for k, v in b["eusd_by_source"].items()},
                        default_price_eusd=round(b["default_price_eusd"], 4)) for _, b in sorted(by.items())]
        return {"pending": len(pending), "pending_eusd": round(sum(e["amount"] for e in pending), 4),
                "pending_oldest_sec": round(now - pending[0]["ts"], 1) if pending else 0,
                "charges": charges, "quality_1h": quality}

    def capacity_rejected(self, reason):
        """A proxy request turned away by the process-wide limits; one event a minute at most."""
        now = self.clock()
        if now - self.capacity_event_at >= 60:
            self.capacity_event_at = now
            self.ledger.add_event({"ts": now, "pool": None, "event": "capacity_reject",
                                   "detail": {"message": reason, **self.capacity.status()}})
            self.notifier.alert("capacity_reject", "capacity", f"lingqu is full, requests get a 503: {reason}")

    def upstream_result(self, pool, error_class, detail=""):
        """Called once per metered response: error_class None for a success (DESIGN §4.7, §6.4).

        last_errors[pool] keeps the latest error class, how often it came in the last 10 minutes
        (count) and how many responses in a row ended with it (streak; a success ends a streak,
        a client error neither extends nor ends one). account_auth and router_unreachable need a
        person: they alert as soon as a streak reaches notify.auth_streak, and again when the pool
        recovers."""
        if error_class in CLIENT_CLASSES:
            return
        now = self.clock()
        e = self.last_errors.get(pool)
        if error_class is None:
            if e and e.get("streak"):
                if e.get("alerted"):
                    self._streak_end(pool, e, now)
                e["streak"], e["alerted"] = 0, False
            return
        if e and e["class"] == error_class:
            e["count"] = e["count"] + 1 if now - e["at"] < 600 else 1
            e["streak"] = e.get("streak", 0) + 1
            e["at"], e["detail"] = now, (detail or "")[:300]
        else:
            if e and e.get("alerted"):
                self._streak_end(pool, e, now)
            e = self.last_errors[pool] = {"class": error_class, "at": now, "count": 1, "streak": 1,
                                          "since": now, "detail": (detail or "")[:300], "alerted": False}
        if error_class in ALERT_CLASSES and not e["alerted"] and e["streak"] >= self.settings.auth_streak:
            e["alerted"] = True
            what = ALERT_CLASSES[error_class]
            self.notifier.alert(error_class, pool, f"pool {pool}: {what} ({e['streak']} in a row): {e['detail']}",
                                streak=e["streak"])
            self.ledger.add_event({"ts": now, "pool": pool, "event": error_class,
                                   "detail": {"message": f"{what}: {e['detail']}", "streak": e["streak"]}})

    def _streak_end(self, pool, e, now):
        kind = e["class"]
        self.notifier.forget(kind, pool)        # a new failure alerts again at once
        self.notifier.alert(f"{kind}_recovered", pool,
                            f"pool {pool}: {kind} cleared after {e['streak']} failures in {now - e['since']:.0f} s")
        self.ledger.add_event({"ts": now, "pool": pool, "event": f"{kind}_recovered",
                               "detail": {"message": f"{kind} cleared after {e['streak']} failures",
                                          "streak": e["streak"]}})

    # ------------------------------------------------------------ reload

    def reload(self, path=None):
        """Validate, then swap in a new config; the old one stays on any error (DESIGN §8.3)."""
        new, errors, warnings = settings_mod.load(path or self.settings.path)
        if not errors and new.redis != self.settings.redis:
            errors.append("redis settings require a coordinated restart")
        if not errors and new.usage != self.settings.usage:
            errors.append("usage settings require a restart")
        if not errors and (new.proxy_port != self.settings.proxy_port or new.status_port != self.settings.status_port or
                           new.dashboard_ports() != self.settings.dashboard_ports() or new.dsn != self.settings.dsn):
            errors.append("database and listening port changes require a restart")
        if errors:
            return False, errors, warnings
        with self.quota.lock:
            try:
                self.gate.reload(new)
            except Exception:
                return False, ["shared state unavailable: configuration was not reloaded"], warnings
            self.settings = new
            self.pricing.configure(new)
            self.quota.configure()
            self.manager.configure()
            self._share_sessions()
        self.exposure.configure(new)
        self.capacity.configure(new)
        self.notifier.webhook = new.webhook
        with self.client_lock:
            self.clients = {k: v for k, v in self.clients.items() if k in new.upstreams}
        log.info("config reloaded from %s", new.path)
        return True, [], warnings

    # ------------------------------------------------------------ background threads

    def restore(self):
        try:
            self.pricing.load_snapshot()
        except Exception as e:
            log.warning("loading the price snapshot failed: %s", e)
        self.recover()
        self.exposure.load()

    def recover(self):
        """Rebuild the windows, sessions, spend history and quotas from the database. Until it
        succeeds the gate turns metered requests away and /healthz says not ready: admitting with
        empty windows would undercount the last hour. Retried by the gate thread."""
        self.gate.ready = False
        started = self.clock()
        try:
            if self.settings.redis:
                how = self.gate.bootstrap(self._replay)
            else:
                self._replay(self.gate)
                how = "restored"
        except Exception as e:
            first = self.recovery is None or self.recovery["state"] == "ok"
            self.recovery = {"state": "failed", "error": repr(e)[:300], "at": self.clock(),
                             "attempts": (self.recovery or {}).get("attempts", 0) + 1}
            log.warning("restoring the windows failed (not ready, retried): %s", e)
            if first:
                self.notifier.alert("recovery_failed", "recovery", f"restoring the windows failed; metered requests "
                                                                   f"get a 503 until it succeeds: {e}")
            return False
        if how == "rebuilt":
            self.notifier.alert("shared_state_lost", "redis", "Redis lost the shared state; rebuilt it from the ledger")
        self.recovery = {"state": "ok", "how": how, "at": self.clock(), "seconds": round(self.clock() - started, 2)}
        self.gate.ready = True
        log.info("windows %s in %.2f s", how, self.recovery["seconds"])
        return True

    def _replay(self, gate):
        if self.settings.redis:
            # The gate rebuilds from scratch in its building transaction.
            restore_counters(self.app, gate, clock=self.clock)
        else:
            # A retry after a failed attempt must not add what that attempt already did.
            fresh = Gate(self.settings, Counters(), clock=self.clock)
            restore_counters(self.app, fresh, clock=self.clock)
            with self.gate.lock:
                self.gate.counters.windows, self.gate.counters.history = fresh.counters.windows, fresh.counters.history
                self.gate.sessions = fresh.sessions
                for name, st in fresh.pools.items():
                    self.gate.state(name).counts = st.counts
        self.quota.load()
        self._load_estimates()

    def _load_estimates(self):
        """Seed the reservation estimates with the last day's settled requests."""
        from .models import Ledger
        with self.app.app_context():
            rows = (Ledger.query.with_entities(Ledger.pool, Ledger.cache_state, Ledger.eusd_input)
                    .filter(Ledger.ts >= self.clock() - 86400, Ledger.usage_source == "upstream", Ledger.eusd_input > 0)
                    .order_by(Ledger.ts.desc()).limit(5000).all())
        with self.gate.lock:
            for pool, cache, amount in reversed(rows):
                self.gate.reservations.observe(pool, cache, amount)

    def _loop(self, name, every, fn):
        def run():
            while not self.stop.is_set():
                self.heartbeats[name] = self.clock()
                try:
                    fn()
                except Exception as e:
                    log.warning("%s: %s", name, e)
                    if name == "ledger" and self.ledger.lag() > 60:
                        self.notifier.alert("ledger_backlog", "ledger", f"ledger writes are failing: {e}")
                if every:
                    self.stop.wait(every)
        t = threading.Thread(target=run, name=f"lingqu-{name}", daemon=True)
        t.start()
        self.threads.append(t)

    def _price_tick(self):
        if self.pricing.due():
            try:
                self.pricing.sync()
            except Exception:
                pass
        if self.pricing.stale():
            self.notifier.alert("pricing_stale", "pricing", f"prices not synced for over 24 h: {self.pricing.last_error}")

    def _gate_tick(self):
        if self.recovery and (self.recovery["state"] != "ok" or (self.settings.redis and self.gate.state_missing())):
            self.recover()
        self.gate.tick()
        self.exposure.expire()
        if self.unsettled:
            self.retry_charges()
        if self.clock() - self.pruned_at >= 3600:
            self.pruned_at = self.clock()
            prune_meters(self.app, clock=self.clock)

    def start(self):
        """Start the background threads (gunicorn worker only, never in tests or the CLI)."""
        self.restore()
        self._loop("ledger", 0, self.ledger.run_once)
        self._loop("pricing", 30, self._price_tick)
        if self.settings.redis:
            self._loop("leases", 10, self.gate.renew)
        else:
            self._loop("counters", 60, lambda: save_counters(self.app, self.gate.counters, clock=self.clock))
        self._loop("gate", 15, self._gate_tick)
        self._loop("quota", 15, self.quota.tick)
        self._loop("notify", 0, self.notifier.run_once)
        self._loop("manager", 15, self.manager.tick)
        if self.settings.usage:
            self._loop("usage", 5, self.usage.tick)

    def shutdown(self):
        """SIGTERM: held requests get a 503 at once; streams already running carry on."""
        self.gate.shutdown()

    def exit(self):
        """Process exit, after the last stream: write what is still queued."""
        self.stop.set()
        try:
            self.ledger.flush()
            if not self.settings.redis:
                save_counters(self.app, self.gate.counters, clock=self.clock)
        except Exception as e:
            log.warning("flush on exit failed: %s", e)

    def health(self):
        """Readiness: background threads alive, ledger written, windows recovered, shared state up."""
        now = self.clock()
        stale_threads = [n for n, t in self.heartbeats.items() if now - t > 180]
        shared = self.gate.health() if self.settings.redis else {"ok": True, "backend": "memory"}
        ledger = self.ledger.metrics()
        recovered = self.recovery is None or self.recovery["state"] == "ok"
        return {"ok": not stale_threads and ledger["lag_sec"] < 120 and shared["ok"] and recovered,
                "recovery": self.recovery, "coordination": shared,
                "threads": {n: round(now - t) for n, t in self.heartbeats.items()},
                "stale_threads": stale_threads, "ledger_lag_sec": ledger["lag_sec"],
                "ledger_dropped": ledger["dropped_total"], "ledger": ledger, "capacity": self.capacity.status(),
                "pricing_stale": self.pricing.stale()}
