"""Per-worker read-only billing cache, driven by Lingqu's runtime lifecycle.

Only GETs to the configured New API are allowed. Router quota and gate state come
from existing Lingqu collectors; this module never changes channels or admissions.
"""
import copy
import json
import logging
import math
import threading
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .aggregate import lingqu_calibration, lingqu_limits, stats

log = logging.getLogger("lingqu")


def number(value, integer=False, allow_negative=False):
    value = 0 if value is None else value
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or (value < 0 and not allow_negative)):
        raise ValueError("invalid New API numeric field")
    if integer and int(value) != value:
        raise ValueError("invalid New API integer field")
    return int(value) if integer else value


def parse_info(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


class NewAPI:
    def __init__(self, config, pulse, stopped):
        self.config, self.pulse, self.stopped = config, pulse, stopped
        self.truncated = False

    def get(self, path):
        if not path.startswith("/api/"):
            raise ValueError("invalid New API read path")
        self.pulse()
        response = httpx.get(self.config["base_url"] + path,
                             headers={"Authorization": "Bearer " + self.config["token"],
                                      "New-Api-User": self.config["user_id"]},
                             timeout=15, trust_env=False, follow_redirects=False)
        self.pulse()
        if response.status_code != 200:
            # Response bodies and URLs can contain credentials: never expose them.
            raise RuntimeError(f"New API read failed (HTTP {response.status_code})")
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is False or "data" not in body:
            raise ValueError("invalid New API response")
        return body["data"]

    def items(self, path, maximum):
        count, page = 0, 1
        sep = "&" if "?" in path else "?"
        while not self.stopped():
            data = self.get(f"{path}{sep}p={page}&page_size=100")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ValueError("invalid New API pagination")
            batch = data["items"]
            for item in batch:
                if count >= maximum:
                    self.truncated = True
                    return
                if not isinstance(item, dict):
                    raise ValueError("invalid New API item")
                yield item
                count += 1
            total = data.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("invalid New API pagination total")
            if count >= total or not batch:
                return
            if count >= maximum:
                self.truncated = True
                return
            page += 1


class Usage:
    def __init__(self, runtime):
        self.rt, self.clock = runtime, runtime.clock
        self.config = runtime.settings.usage
        self.state = SimpleNamespace(lock=threading.RLock(), consume={}, errors={}, groups={},
                                     channel_provider={}, channel_group={}, quota_per_usd=500000,
                                     synced_at=0, sync_error="", truncated=False,
                                     watermarks={"consume": 0, "errors": 0})
        self.sync_lock = threading.Lock()
        self.last_attempt = self.last_channels = None
        self.api = NewAPI(self.config.get("newapi", {}), self._pulse, runtime.stop.is_set)

    def _pulse(self):
        self.rt.heartbeats["usage"] = self.clock()

    def midnight(self, timestamp):
        date = datetime.fromtimestamp(timestamp, ZoneInfo(self.config["timezone"]))
        return int(date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def refresh_channels(self):
        groups, providers, channel_groups = {}, {}, {}
        for channel in self.api.items("/api/channel/", 10000):
            name = channel.get("name")
            if not isinstance(name, str) or not name:
                continue
            router = parse_info(channel.get("other_info")).get("machgate_router")
            if self.config["scope"] == "managed":
                if not isinstance(router, str) or not router:
                    continue
                group = name if channel.get("type") == 14 else name.rsplit("-", 1)[0]
            else:
                group = (channel.get("group") or "").split(",")[0]
            if not group or group == "default":
                continue
            kind = "claude" if channel.get("type") == 14 else "codex"
            providers[name], channel_groups[name] = kind, group
            groups.setdefault(group, {"router": router or "", "channels": {}})["channels"][name] = kind
        status = self.api.get("/api/status")
        per_usd = status.get("quota_per_unit") if isinstance(status, dict) else None
        if isinstance(per_usd, bool) or not isinstance(per_usd, (int, float)) or not math.isfinite(per_usd) or per_usd <= 0:
            raise ValueError("New API quota_per_unit must be positive")
        with self.state.lock:
            if self.state.channel_group != channel_groups or self.state.channel_provider != providers:
                self.state.watermarks = {"consume": 0, "errors": 0}
            self.state.groups, self.state.channel_provider, self.state.channel_group = groups, providers, channel_groups
            self.state.quota_per_usd = per_usd
        self.last_channels = self.clock()

    def record(self, item, error=False):
        with self.state.lock:
            group = (self.state.channel_group.get(item.get("channel_name")) if self.config["scope"] == "managed"
                     else item.get("group") or "")
            provider = self.state.channel_provider.get(item.get("channel_name"))
        if group is None:
            return None
        model = item.get("model_name") or ""
        row = {"t": number(item["created_at"], integer=True), "user": item.get("username") or "", "key": item.get("token_name") or "",
               "group": group, "model": model, "provider": provider or ("claude" if model.startswith("claude") else "codex"),
               "dur": number(item.get("use_time"))}
        if error:
            row["message"] = (item.get("content") or "")[:400]
        else:
            other = parse_info(item.get("other"))
            prompt, cache_read = number(item.get("prompt_tokens"), integer=True), number(other.get("cache_tokens"), integer=True)
            cache_write = number(other.get("cache_creation_tokens") or other.get("cache_write_tokens"), integer=True)
            anthropic = other.get("usage_semantic") == "anthropic" or other.get("claude") is True
            row.update(input=prompt if anthropic else max(0, prompt-cache_read-cache_write),
                       cache_read=cache_read, cache_write=cache_write, output=number(item.get("completion_tokens"), integer=True),
                       quota=number(item.get("quota")), frt=number(other.get("frt"), allow_negative=True))
        return row

    def _prune(self, now):
        cutoff = now - self.config["retention_days"] * 86400
        for table in (self.state.consume, self.state.errors):
            for key in [k for k, row in table.items() if row["t"] < cutoff]:
                del table[key]
        records = [(r["t"], table, key) for table in ("consume", "errors")
                   for key, r in getattr(self.state, table).items()]
        maximum = self.config["max_records"]
        if len(records) > maximum:
            self.state.truncated = True
            for _, table, key in sorted(records)[:len(records)-maximum]:
                del getattr(self.state, table)[key]

    def sync(self):
        now = int(self.clock())
        try:
            if self.last_channels is None or now - self.last_channels >= 300:
                self.refresh_channels()
            with self.state.lock:
                completed = dict(self.state.watermarks)
            for kind, name in ((2, "consume"), (5, "errors")):
                with self.state.lock:
                    table = getattr(self.state, name)
                    newest = self.state.watermarks[name]
                since = max(now-self.config["retention_days"]*86400, newest-900)
                path = "/api/log/?" + urlencode({"type": kind, "start_timestamp": since})
                count = 0
                for item in self.api.items(path, self.config["max_records"]):
                    if self.rt.stop.is_set():
                        return
                    row = self.record(item, error=kind == 5)
                    completed[name] = max(completed[name], min(now, number(item["created_at"], integer=True)))
                    if row is not None:
                        with self.state.lock:
                            table[item["id"]] = row
                    count += 1
                    if count % 100 == 0 and len(self.state.consume) + len(self.state.errors) > self.config["max_records"]:
                        with self.state.lock:
                            self._prune(now)
                with self.state.lock:
                    self._prune(now)
                if self.rt.stop.is_set():
                    return
            with self.state.lock:
                # A partial backfill must not move the next read past an older failing row.
                self.state.watermarks = completed
                self.state.synced_at, self.state.sync_error = int(self.clock()), ""
                self.state.truncated |= self.api.truncated
        except Exception:
            with self.state.lock:
                self.state.sync_error = "New API 日志同步失败，请检查连接、系统访问令牌与权限"
            # Do not log exceptions containing authenticated URLs or upstream response bodies.
            log.warning("usage: New API billing sync failed; keeping previous records")

    def tick(self):
        if not self.config or self.rt.stop.is_set() or not self.sync_lock.acquire(blocking=False):
            return
        try:
            if self.last_attempt is None or self.clock()-self.last_attempt >= 20:
                self.last_attempt = self.clock()
                self.sync()
        finally:
            self.sync_lock.release()

    def pool_status(self):
        now = self.clock()
        return {"pools": [{"name": name, "kind": pool.kind, "upstream": pool.upstream,
                           **self.rt.gate.pool_status(name, now), "quota": self.rt.quota.status(name, now)}
                          for name, pool in self.rt.settings.pools.items()]}

    def limits(self):
        return lingqu_limits(self.pool_status())

    def stats(self, period, provider, group):
        result = stats(self, period, provider, group)
        with self.state.lock:
            result["sync_warning"] = "已达到记录上限，较早记录可能不完整" if self.state.truncated else ""
        result["cost_source"] = "newapi_billed"
        result["throttle_source"] = "lingqu_events"
        return result

    def throttles(self, start, end, limits):
        """Count real gate events; billed USD must never be compared to eUSD caps."""
        from sqlalchemy import func
        from ..models import GateEvent
        pools = {name: (name.rsplit("/", 1)[0], pool.kind)
                 for name, pool in self.rt.settings.pools.items() if "/" in name}
        selected = {name: key for name, key in pools.items() if key in limits}
        if not selected:
            return []
        with self.rt.app.app_context():
            counts = (GateEvent.query.with_entities(GateEvent.pool, func.count(GateEvent.id))
                      .filter(GateEvent.event == "throttle_start", GateEvent.ts >= start,
                              GateEvent.ts <= end, GateEvent.pool.in_(selected))
                      .group_by(GateEvent.pool).all())
        totals = {}
        for pool, count in counts:
            key = selected[pool]
            totals[key] = totals.get(key, 0) + count
        return [{"group": key[0], "provider": key[1], "limit": limits[key], "count": count}
                for key, count in sorted(totals.items())]

    def health(self):
        with self.state.lock:
            return {"ok": bool(self.state.synced_at) and not self.state.sync_error,
                    "synced_at": self.state.synced_at, "sync_error": self.state.sync_error,
                    "truncated": self.state.truncated}

    def quotas(self):
        now = self.clock()
        status = self.pool_status()
        limits, calibration = lingqu_limits(status), lingqu_calibration(status, now)
        with self.state.lock:
            groups = copy.deepcopy(self.state.groups)
        # Snapshot the existing manager inventory; never make a request from a page handler.
        with self.rt.manager.lock:
            inventory = copy.deepcopy(self.rt.manager.rows)
        out = []
        for group, meta in sorted(groups.items()):
            accounts, errors, samples = {}, [], {}
            pools = [p for p in status["pools"] if p["name"].rsplit("/", 1)[0] == group]
            for pool in pools:
                quota = pool.get("quota") or {}
                if quota.get("last_error"):
                    errors.append("额度采集失败，保留上次快照")
                row = inventory.get(pool["upstream"], {})
                names = dict(self.rt.quota.connection_names.get(pool["upstream"], {}))
                names.update({c["id"]: c.get("name") or c["id"] for c in row.get("connections", []) if c.get("id")})
                for window in quota.get("windows", []):
                    cid = window.get("connection") or quota.get("account") or pool["name"]
                    account = accounts.setdefault((pool["kind"], cid), {"kind": pool["kind"], "provider": pool["kind"],
                        "name": names.get(cid, cid), "email": "", "active": True, "plan": None, "quotas": [], "note": ""})
                    label = {"short": "session", "weekly_opus": "weekly opus (7d)",
                             "weekly_sonnet": "weekly sonnet (7d)"}.get(window["window"], window["window"])
                    key = (pool["kind"], cid, label)
                    if key in samples and samples[key]["ts"] >= window["ts"]:
                        continue
                    samples[key] = window
                    account["quotas"] = [q for q in account["quotas"] if q["label"] != label]
                    account["quotas"].append({"label": label, "used": window["used_pct"], "total": 100,
                        "resetAt": datetime.fromtimestamp(window["reset_at"], ZoneInfo("UTC")).isoformat()
                                   if window.get("reset_at") else None, "unlimited": False})
                if not quota.get("windows"):
                    accounts.setdefault((pool["kind"], pool["name"]), {"kind": pool["kind"], "provider": pool["kind"],
                        "name": pool["name"], "email": "", "active": True, "plan": None, "quotas": [],
                        "note": "等待灵渠额度采集；请确认 upstream.quota 已配置"})
            for (kind, cid), account in accounts.items():
                if any(w.get("stale") for (k, c, _), w in samples.items() if k == kind and c == cid):
                    account["note"] = "额度快照已过期，等待现有采集器刷新"
            out.append({"group": group, "router": meta["router"], "accounts": list(accounts.values()),
                        "error": "；".join(sorted(set(errors))), "fetched_at": min((w["ts"] for w in samples.values()), default=0),
                        "limits": limits.get(group, []), "calibration": calibration.get(group, {})})
        return {"now": int(now), "limit_error": "", "groups": out}
