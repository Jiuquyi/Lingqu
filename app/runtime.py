"""The runtime of a Lingqu worker with optional Redis admission coordination (DESIGN §11.2): settings, gate, prices,
ledger writer, alerts and the dashboard gate, plus the background threads that keep them."""
import logging
import threading
import time

import httpx

from . import settings as settings_mod
from .dashboard.exposure import Exposure
from .gate.hold import Gate
from .gate.window import Counters
from .ledger import LedgerWriter, restore_counters, save_counters
from .observe.notify import Notifier
from .pricing.sync import Pricing
from .quota import QuotaCollector
from .manager import Manager

log = logging.getLogger("lingqu")
VERSION = "0.8.0"
# Error classes that need a person, alerted when a pool sees them several times in a row.
ALERT_CLASSES = {"account_auth": "upstream account authorization failed; re-authorize it in 9router",
                 "router_unreachable": "cannot connect to 9router"}


class Runtime:
    def __init__(self, app, settings, clock=time.time):
        self.app, self.clock = app, clock
        self.settings = settings
        self.notifier = Notifier(settings.webhook, clock=clock)
        self.ledger = LedgerWriter(app, clock=clock)
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

    def upstream_result(self, pool, error_class, detail=""):
        """Called once per metered response: error_class None for a success (DESIGN §4.7, §6.4).

        last_errors[pool] keeps the latest error class, how often it came in the last 10 minutes
        (count) and how many responses in a row ended with it (streak; a success ends a streak,
        a client error neither extends nor ends one). account_auth and router_unreachable need a
        person: they alert as soon as a streak reaches notify.auth_streak, and again when the pool
        recovers."""
        if error_class == "client_error":
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
        try:
            n = 0 if self.settings.redis else restore_counters(self.app, self.gate, clock=self.clock)
            log.info("rolling windows and sessions restored (%d ledger rows replayed)", n)
        except Exception as e:
            log.warning("restoring the rolling windows failed: %s", e)
        self.exposure.load()
        try:
            if not self.settings.redis:
                self.quota.load()
        except Exception as e:
            log.warning("loading upstream quotas failed: %s", e)

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
        self.gate.tick()
        self.exposure.expire()

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
        now = self.clock()
        stale_threads = [n for n, t in self.heartbeats.items() if now - t > 180]
        shared = self.gate.health() if self.settings.redis else {"ok": True, "backend": "memory"}
        return {"ok": not stale_threads and self.ledger.lag() < 120 and shared["ok"], "coordination": shared,
                "threads": {n: round(now - t) for n, t in self.heartbeats.items()},
                "stale_threads": stale_threads, "ledger_lag_sec": self.ledger.lag(),
                "ledger_dropped": self.ledger.dropped, "pricing_stale": self.pricing.stale()}
