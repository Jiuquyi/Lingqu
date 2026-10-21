"""The single in-process state of a Lingqu instance (DESIGN §11.2): settings, gate, prices,
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

log = logging.getLogger("lingqu")
VERSION = "0.5.0"


class Runtime:
    def __init__(self, app, settings, clock=time.time):
        self.app, self.clock = app, clock
        self.settings = settings
        self.notifier = Notifier(settings.webhook, clock=clock)
        self.ledger = LedgerWriter(app, clock=clock)
        self.gate = Gate(settings, Counters(), clock=clock, on_event=self._gate_event)
        self.pricing = Pricing(app, settings, clock=clock)
        self.pricing.on_unpriced = self._unpriced
        self.exposure = Exposure(app, settings, clock=clock, on_change=self._exposure_change)
        self.clients = {}
        self.client_lock = threading.Lock()
        self.heartbeats = {}
        self.threads = []
        self.stop = threading.Event()
        self.last_errors = {}       # pool -> {"class", "at", "count"}
        self.started_at = clock()

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

    def _unpriced(self, model, kind):
        self.notifier.alert("unpriced_model", model, f"no price for model {model}; using the {kind} default price")

    def _exposure_change(self, upstream, action, state, until, actor, source):
        self.ledger.add_event({"ts": self.clock(), "pool": None, "event": f"dashboard_{action}",
                               "detail": {"upstream": upstream, "state": state, "until": until, "actor": actor,
                                          "source": source}})
        if state == "persistent" and action == "open":
            self.notifier.alert("dashboard_persistent", upstream,
                                f"9router dashboard of {upstream} kept open by {actor}")

    def upstream_error(self, pool, error_class):
        if not error_class or error_class == "client_error":
            return
        now = self.clock()
        e = self.last_errors.get(pool)
        if e and e["class"] == error_class and now - e["at"] < 600:
            e["count"] += 1
            e["at"] = now
        else:
            e = self.last_errors[pool] = {"class": error_class, "at": now, "count": 1}
        if error_class == "account_auth":
            self.notifier.alert("account_auth", pool, f"pool {pool}: upstream account authorization failed "
                                                      f"({e['count']} in a row); needs a person to re-authorize")

    # ------------------------------------------------------------ reload

    def reload(self, path=None):
        """Validate, then swap in a new config; the old one stays on any error (DESIGN §8.3)."""
        new, errors, warnings = settings_mod.load(path or self.settings.path)
        if errors:
            return False, errors, warnings
        self.settings = new
        self.gate.reload(new)
        self.pricing.configure(new)
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
            n = restore_counters(self.app, self.gate, clock=self.clock)
            log.info("rolling windows restored (%d ledger rows replayed)", n)
        except Exception as e:
            log.warning("restoring the rolling windows failed: %s", e)
        self.exposure.load()

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
        self._loop("counters", 60, lambda: save_counters(self.app, self.gate.counters, clock=self.clock))
        self._loop("gate", 15, self._gate_tick)
        self._loop("notify", 0, self.notifier.run_once)

    def shutdown(self):
        """SIGTERM: held requests get a 503 at once; streams already running carry on."""
        self.gate.shutdown()

    def exit(self):
        """Process exit, after the last stream: write what is still queued."""
        self.stop.set()
        try:
            self.ledger.flush()
            save_counters(self.app, self.gate.counters, clock=self.clock)
        except Exception as e:
            log.warning("flush on exit failed: %s", e)

    def health(self):
        now = self.clock()
        stale_threads = [n for n, t in self.heartbeats.items() if now - t > 180]
        return {"ok": not stale_threads and self.ledger.lag() < 120,
                "threads": {n: round(now - t) for n, t in self.heartbeats.items()},
                "stale_threads": stale_threads, "ledger_lag_sec": self.ledger.lag(),
                "ledger_dropped": self.ledger.dropped, "pricing_stale": self.pricing.stale()}
