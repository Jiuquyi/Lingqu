"""Calendar budgets and upstream safety limits. All calls run under Gate.lock; no I/O here."""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def cycle_bounds(rule, now, reset_at=None):
    period = rule.cycle_period
    if rule.anchor == "upstream_reset" and reset_at is not None:
        anchor = reset_at
    elif isinstance(rule.anchor, (int, float)):
        anchor = rule.anchor
    else:
        text = "mon 00:00" if rule.anchor == "upstream_reset" else rule.anchor
        day, hm = text.split()
        hour, minute = map(int, hm.split(":"))
        local = datetime.fromtimestamp(now, ZoneInfo(rule.timezone))
        anchor = (local - timedelta(days=(local.weekday() -
                  ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day)) % 7)).replace(
                      hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    start = anchor + math.floor((now - anchor) / period) * period
    return start, start + period


def weights_remaining(rule, weights, now, end):
    zone = ZoneInfo(rule.timezone)
    local = datetime.fromtimestamp(now, zone)
    t = local.replace(minute=0, second=0, microsecond=0).timestamp()
    current = weights[local.weekday() * 24 + local.hour]
    total = 0.0
    while t < end:
        hour = datetime.fromtimestamp(t, zone)
        total += weights[hour.weekday() * 24 + hour.hour] * min(3600, end - t) / 3600
        t += 3600
    return current, total


# Routers recompute reset times on every read, so one cycle's reset_at drifts by up to a second.
RESET_JITTER = 120
CALIBRATION_HISTORY = 48


def same_cycle(a, b):
    return a == b if a is None or b is None else abs(a - b) < RESET_JITTER


class Budgets:
    def __init__(self, settings, counters, on_snapshot=None):
        self.settings, self.counters = settings, counters
        self.on_snapshot = on_snapshot or (lambda row: None)
        self.quotas = {}  # (pool, connection, window) -> normalized observation
        self.cache = {}
        self.revision = {}
        self.calibration = {}  # (account, window) -> the current cycle's eUSD per percent, and past cycles
        self.trigger = "startup"

    def configure(self, settings, trigger="reload"):
        self.quotas = {key: q for key, q in self.quotas.items() if key[0] in settings.pools and
                       key[0] in self.settings.pools and
                       settings.upstreams[settings.pools[key[0]].upstream].base_url ==
                       self.settings.upstreams[self.settings.pools[key[0]].upstream].base_url and
                       settings.pools[key[0]].connections == self.settings.pools[key[0]].connections}
        self.settings = settings
        self.invalidate(trigger)

    def invalidate(self, trigger):
        self.cache.clear()
        self.trigger = trigger

    def update(self, pool, connection, windows, now, source="api", persist=None):
        changed = False
        for window, data in windows.items():
            key = (pool, connection, window)
            old = self.quotas.get(key)
            self.calibrate(pool, window, data, now)
            baseline = old.get("budget_pct", old["used_pct"]) if old else None
            row = dict(data, pool=pool, connection=connection, window=window, ts=now, source=source,
                       stale=False, error=None)
            self.quotas[key] = row
            meaningful = (old is None or abs(baseline - row["used_pct"]) >= 1 or
                        not same_cycle(old.get("reset_at"), row.get("reset_at")) or old.get("stale", False) or
                        self.level(old["used_pct"]) != self.level(row["used_pct"]))
            row["budget_pct"] = row["used_pct"] if meaningful else baseline
            changed |= meaningful
            if persist:
                persist({k: row[k] for k in ("pool", "connection", "window", "ts", "source", "used_pct", "reset_at")})
        if changed:
            self.revision[pool] = self.revision.get(pool, 0) + 1
            self.trigger = "quota"

    def account(self, pool):
        p = self.settings.pools.get(pool)
        return (p.account if p else None) or pool

    def account_pools(self, account):
        return sorted(n for n, p in self.settings.pools.items() if (p.account or n) == account)

    def calibrate(self, pool, window, data, now):
        """eUSD per upstream percent over a whole cycle: spend of every pool on the account since
        the cycle's first observation, divided by how far used_pct rose since then. A hint only."""
        p = self.settings.pools.get(pool)
        if not p or (len(p.connections) > 1 and not p.account):
            return  # several accounts in one pool: their spend cannot be told apart
        account, pct, reset = self.account(pool), data["used_pct"], data.get("reset_at")
        key = (account, window)
        cal = self.calibration.get(key)
        # Without a reset time cycles cannot be told apart, so such a reading is skipped. One whose
        # cycle has already ended, or is older than the one being calibrated, is a router serving
        # a cached value; letting it end the cycle splits one cycle into many.
        if reset is None or reset <= now or (cal and cal["reset_at"] is not None and
                                             reset < cal["reset_at"] - RESET_JITTER):
            return
        # A lower percent alone is no reset: pools on one account read it seconds apart through
        # different routers.
        if cal is None or not same_cycle(cal["reset_at"], reset):
            history = cal["history"] if cal else []
            if cal and cal["eusd_per_percent"] is not None:
                history = (history + [{k: v for k, v in cal.items() if k not in ("history", "pools")}])[-CALIBRATION_HISTORY:]
            # How far the account's other windows rose over this cycle: their ratio to this one
            # holds whoever spends, so it carries a value across windows (docs/M4.md).
            peers = {w: {"reset_at": c["reset_at"], "start_pct": c["used_pct"], "used_pct": c["used_pct"], "delta_pct": 0}
                     for (a, w), c in self.calibration.items() if a == account and w != window and c["reset_at"] is not None}
            self.calibration[key] = {"account": account, "window": window, "pools": self.account_pools(account),
                                     "reset_at": reset, "start_ts": now, "start_pct": pct,
                                     "ts": now, "used_pct": pct, "delta_pct": 0, "eusd": 0.0,
                                     "eusd_per_percent": None, "suggested_limit": None, "peers": peers,
                                     "history": history}
            return
        # Within a cycle the percent only rises. A lower reading is an older one: a lagging router
        # or one stuck on a cached snapshot whose reset time is still ahead.
        if pct < cal["used_pct"]:
            return
        for w, peer in cal.get("peers", {}).items():
            other = self.calibration.get((account, w))
            if peer["delta_pct"] is None:
                continue
            if other is None or not same_cycle(other["reset_at"], peer["reset_at"]):
                peer["delta_pct"] = None  # that window reset meanwhile: the rises no longer compare
            else:
                peer.update(used_pct=other["used_pct"], delta_pct=other["used_pct"] - peer["start_pct"])
        pattern = "*" + window.removeprefix("weekly_") + "*" if window.startswith("weekly_") else None
        pools = self.account_pools(account)
        spent = sum(self.counters.total_since(n, pattern, cal["start_ts"], now, after=True) for n in pools)
        delta_pct = pct - cal["start_pct"]
        ok = delta_pct >= 1 and spent > 0
        cal.update(pools=pools, reset_at=data["reset_at"], ts=now, used_pct=pct, delta_pct=delta_pct, eusd=spent,
                   eusd_per_percent=spent / delta_pct if ok else None,
                   suggested_limit=spent * 100 / delta_pct if ok else None)

    @staticmethod
    def level(pct):
        return "reject" if pct >= 98 else "minimum" if pct >= 90 else "normal"

    def windows(self, pool, now, fresh_only=False):
        p = self.settings.pool_config(pool)
        ttl = self.settings.upstreams[p.upstream].quota.get("stale_after", 360) if p.upstream else 360
        out = []
        for (name, connection, _), q in self.quotas.items():
            if name != pool or (p.connections and connection not in p.connections):
                continue
            expired = q.get("reset_at") is not None and now >= q["reset_at"]
            stale = q.get("stale", False) or now - q["ts"] > ttl
            if not expired and (not fresh_only or not stale):
                out.append(dict(q, stale=stale))
        return out

    def weekly(self, pool, rule, now, fresh_only=False):
        return max((q for q in self.windows(pool, now, fresh_only) if q["window"] == rule.quota_window),
                   key=lambda q: q["used_pct"], default=None)

    def cycle(self, pool, rule, now):
        # Even an expired observation still tells us how cycles were aligned.
        anchors = [q for (p, _, w), q in self.quotas.items() if p == pool and w == rule.quota_window
                   and q.get("reset_at") is not None]
        boundaries = [cycle_bounds(rule, now, q["reset_at"]) for q in anchors]
        # With explicitly grouped accounts, include the union of their current cycles.
        start, end = min(boundaries) if boundaries else cycle_bounds(rule, now)
        used = self.counters.total_since(pool, rule.scope_model, start, now)
        q = self.weekly(pool, rule, now, fresh_only=True)
        if q:
            # Account for traffic since the last real reading, without double counting earlier spend.
            used = max(used, rule.cycle_limit * q["used_pct"] / 100 +
                       self.counters.total_since(pool, rule.scope_model, q["ts"], now, after=True))
        return {"cycle_start": start, "cycle_end": end, "used_cycle": used,
                "remaining_r": max(0.0, rule.cycle_limit - used)}

    def emergency(self, pool, now, model=None):
        windows = self.windows(pool, now)
        return max((self.level(q["used_pct"]) for q in windows),
                   key={"normal": 0, "minimum": 1, "reject": 2}.get, default="normal")

    def value(self, pool, rule, now):
        cycle = self.cycle(pool, rule, now)
        local = datetime.fromtimestamp(now, ZoneInfo(rule.timezone))
        hour = local.replace(minute=0, second=0, microsecond=0).timestamp()
        key = (pool, rule.scope_key)
        freshness = tuple(sorted((q["connection"], q["window"], q["stale"]) for q in self.windows(pool, now)))
        signature = (repr(rule), hour, cycle["cycle_start"], self.revision.get(pool, 0), freshness)
        cached = self.cache.get(key)
        if cached is None or cached[0] != signature:
            trigger = self.trigger if cached is None else "hour"
            if cached is not None:
                if cached[0][2] != signature[2]:
                    trigger = "cycle_reset"
                elif cached[0][3] != signature[3]:
                    trigger = "quota"
                elif cached[0][4] != signature[4]:
                    trigger = "quota_stale"
            weight, remaining = weights_remaining(rule, self.settings.weight_profiles[rule.weight_profile],
                                                   now, cycle["cycle_end"])
            h = max(rule.h_min, min(rule.h_max, cycle["remaining_r"] * weight / remaining * (1 + rule.flex)))
            row = dict(cycle, pool=pool, scope_key=rule.scope_key, ts=now,
                       trigger=trigger,
                       weight_now=weight, weight_rem=remaining, h_value=h, s_rem=None,
                       mode="shadow" if rule.shadow else "enforce")
            self.cache[key] = (signature, row)
            self.on_snapshot(self._limited(pool, rule, dict(row), now))
        return self._limited(pool, rule, dict(self.cache[key][1], **cycle), now)

    def _limited(self, pool, rule, row, now):
        short = None
        if rule.short_limit is not None:
            short = max(0.0, rule.short_limit - self.counters.total_since(pool, rule.scope_model, now - 18000, now, after=True))
            for q in self.windows(pool, now, fresh_only=True):
                if q["window"] == "short":
                    remaining = rule.short_limit * (1 - q["used_pct"] / 100) - self.counters.total_since(
                        pool, rule.scope_model, q["ts"], now, after=True)
                    short = min(short, max(0.0, remaining))
        row["s_rem"] = short
        # Any supported high window reduces every dynamic budget in this pool.
        brake = self.emergency(pool, now, rule.scope_model)
        h = min(row["h_value"], rule.h_min) if brake != "normal" else row["h_value"]
        row["h_value"] = min(h, short) if short is not None else h
        row["brake"] = brake
        return row

    def status(self, pool, now):
        p = self.settings.pool_config(pool)
        rules = p.rules if p else []
        return [self.value(pool, r, now) for r in rules if r.kind == "hourly_budget"]
