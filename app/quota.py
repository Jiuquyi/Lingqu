"""9router quota adapter: dashboard reads and optional upstream response headers only."""
import math
import threading
from datetime import datetime, timezone
from urllib.parse import quote

from .pricing.ninerouter import Session


def finite(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a quota number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("quota number must be finite")
    return number


def timestamp(value):
    if value is None or value == "":
        return None
    try:
        number = finite(value)
        return number / 1000 if number > 1e12 else number
    except (ValueError, TypeError):
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()


WINDOW_NAMES = {"short": "short", "session": "short", "session (5h)": "short", "five_hour": "short",
                "weekly": "weekly", "weekly (7d)": "weekly", "seven_day": "weekly",
                "weekly_opus": "weekly_opus", "weekly opus (7d)": "weekly_opus", "seven_day_opus": "weekly_opus",
                "weekly_sonnet": "weekly_sonnet", "weekly sonnet (7d)": "weekly_sonnet", "seven_day_sonnet": "weekly_sonnet"}


def normalize(body):
    if not isinstance(body, dict):
        raise ValueError("usage response must be an object")
    data = body.get("quotas", body)
    if not isinstance(data, dict):
        raise ValueError("usage quotas must be an object")
    out = {}
    for name, raw in data.items():
        window = WINDOW_NAMES.get(name)
        if window is None or not isinstance(raw, dict) or raw.get("unlimited"):
            continue
        try:
            if "used_pct" in raw or "utilization" in raw:
                pct = finite(raw.get("used_pct", raw.get("utilization")))
            elif "used" in raw and "total" in raw:
                total = finite(raw["total"])
                if total <= 0:
                    raise ValueError("quota total must be positive")
                pct = finite(raw["used"]) / total * 100
            elif "remainingPercentage" in raw:
                pct = 100 - finite(raw["remainingPercentage"])
            else:
                continue
            if pct < 0:
                raise ValueError("quota used percentage must be nonnegative")
            out[window] = {"used_pct": min(100.0, pct),
                           "reset_at": timestamp(raw.get("resetAt", raw.get("reset_at", raw.get("resets_at"))))}
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(f"invalid {name} quota: {e}") from e
    if not out:
        raise ValueError("usage response contains no supported quota windows")
    return out


def from_headers(headers, kind):
    h = {k.lower(): v for k, v in headers.items()}
    out = {}
    pairs = [("short", "x-codex-primary", "used-percent", "reset"),
             ("weekly", "x-codex-secondary", "used-percent", "reset")] if kind == "codex" else [
             ("short", "anthropic-ratelimit-unified-5h", "utilization", "reset"),
             ("weekly", "anthropic-ratelimit-unified-7d", "utilization", "reset")]
    for window, prefix, used, reset in pairs:
        try:
            pct = finite(h[prefix + "-" + used]) * (100 if kind == "claude" else 1)
            if not 0 <= pct <= 100:
                continue
            out[window] = {"used_pct": pct, "reset_at": timestamp(h.get(prefix + "-" + reset))}
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return out


class QuotaCollector:
    def __init__(self, runtime):
        self.rt = runtime
        self.sessions = {}
        self.attempts = {}
        self.errors = {}
        self.connections = {}
        self.lock = threading.Lock()  # serialize manual and background syncs, never held on the data path
        self.configure()

    def configure(self):
        sessions = {}
        for name, u in self.rt.settings.upstreams.items():
            if not u.quota:
                continue
            password = u.quota["password"]
            existing = self.sessions.get(name)
            pricing = self.rt.pricing.session
            sessions[name] = next((s for s in (existing, pricing) if s and s.base_url == u.base_url and
                                   s.password == password), None) or Session(u.base_url, password, self.rt.clock)
            if pricing and pricing.base_url == u.base_url and pricing.password == password:
                self.rt.pricing.session = sessions[name]
        self.sessions = sessions
        self.attempts.clear()
        self.connections.clear()
        self.errors.clear()

    def tick(self):
        for name, u in list(self.rt.settings.upstreams.items()):
            if u.quota and (name not in self.attempts or self.rt.clock() - self.attempts[name] >= u.quota["refresh"]):
                self.sync(name)

    def _failed(self, pool, error):
        self.errors[pool] = str(error)
        with self.rt.gate.lock:
            for (name, _, _), q in self.rt.gate.budgets.quotas.items():
                if name == pool:
                    q["stale"], q["error"] = True, str(error)
            self.rt.gate.budgets.invalidate("quota_stale")
        self.rt.notifier.alert("quota_stale", pool, f"pool {pool}: quota collection failed: {error}")

    def sync(self, upstream=None):
        with self.lock:
            errors = {}
            for name, session in list(self.sessions.items()):
                if upstream is not None and upstream != name:
                    continue
                self.attempts[name] = self.rt.clock()
                pools = [p for p in self.rt.settings.pools.values() if p.upstream == name]
                try:
                    body = session.get("/api/providers")
                    connections = body.get("connections") if isinstance(body, dict) else None
                    if not isinstance(connections, list):
                        raise ValueError("providers response needs connections[]")
                except Exception as e:
                    for p in pools:
                        self._failed(p.name, e)
                        errors[p.name] = str(e)
                    continue
                fetched = {}
                for p in pools:
                    try:
                        aliases = {"claude": {"claude", "cc", "claude-code"}, "codex": {"codex", "cx"}}[p.kind]
                        candidates = [c for c in connections if isinstance(c, dict) and c.get("provider") in aliases
                                      and c.get("isActive", True) and c.get("authType", "oauth") == "oauth"]
                        chosen = [c for c in candidates if c.get("id") in p.connections] if p.connections else candidates
                        if not chosen or (p.connections and {c.get("id") for c in chosen} != set(p.connections)):
                            raise ValueError("no matching active OAuth connections")
                        if not p.connections and len(chosen) != 1:
                            raise ValueError("multiple OAuth connections: set pool.connections explicitly")
                        self.connections[p.name] = [c["id"] for c in chosen]
                        with self.rt.gate.lock:
                            old_keys = [k for k in self.rt.gate.budgets.quotas if k[0] == p.name and
                                        k[1] not in self.connections[p.name]]
                            for k in old_keys:
                                del self.rt.gate.budgets.quotas[k]
                            if old_keys:
                                self.rt.gate.budgets.invalidate("connections")
                        failed = []
                        for c in chosen:
                            cid = c["id"]
                            try:
                                if cid not in fetched:
                                    fetched[cid] = normalize(session.get("/api/usage/" + quote(cid, safe="")))
                                self.rt.gate.update_quota(p.name, cid, fetched[cid], persist=self.rt.ledger.add_quota)
                            except Exception as e:
                                failed.append(str(e))
                        if failed:
                            raise ValueError("; ".join(failed))
                        self.errors.pop(p.name, None)
                    except Exception as e:
                        self._failed(p.name, e)
                        errors[p.name] = str(e)
            return errors

    def headers(self, pool, headers):
        p = self.rt.settings.pools.get(pool)
        if not p:
            return
        ids = p.connections or self.connections.get(pool, [])
        # Headers have no account ID: only a single bound connection can safely accept them.
        if len(ids) != 1:
            return
        windows = from_headers(headers, p.kind)
        if windows:
            self.rt.gate.update_quota(pool, ids[0], windows, source="headers", persist=self.rt.ledger.add_quota)

    def status(self, pool, now):
        with self.rt.gate.lock:
            windows = self.rt.gate.budgets.windows(pool, now)
            calibration = [dict(v) for (p, _, _), v in self.rt.gate.budgets.calibration.items() if p == pool]
        enabled = bool(self.rt.settings.upstreams[self.rt.settings.pools[pool].upstream].quota)
        if not enabled and not windows:
            return None
        return {"windows": windows, "stale": not windows or any(q["stale"] for q in windows),
                "last_error": self.errors.get(pool), "connections": self.connections.get(pool, []),
                "calibration": calibration}

    def load(self):
        from .models import UpstreamQuota
        with self.rt.app.app_context():
            rows = UpstreamQuota.query.order_by(UpstreamQuota.ts, UpstreamQuota.id).all()
            with self.rt.gate.lock:
                for row in rows:
                    if row.pool not in self.rt.settings.pools:
                        continue
                    self.rt.gate.budgets.update(row.pool, row.connection,
                        {row.window: {"used_pct": row.used_pct, "reset_at": row.reset_at}}, row.ts, row.source)
                    self.rt.gate.budgets.quotas[(row.pool, row.connection, row.window)]["stale"] = True
