"""New API billing aggregates and Lingqu quota presentation.

Ported from the deployed usage service (deploy c5a2543). Billing comes from New API
log quota units; it is intentionally separate from Lingqu's eUSD ledger.
"""

PERIODS = {"today": None, "24h": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000}
CALIBRATION_DAYS = 35
CALIBRATION_MIN_PCT = {"short": 10}
CALIBRATION_MIN_PCT_DEFAULT = 5
RATIO_MIN_WEEKLY_PCT = 5
RATIO_MIN_SHORT_PCT = 30

def empty_totals():
    return {"requests": 0, "errors": 0, "input": 0, "cache_read": 0, "cache_write": 0,
            "output": 0, "cost": 0.0, "last": 0}

def stats(service, period, provider, group):
    now = int(service.clock())
    start = service.midnight(now) if period == "today" else now - PERIODS[period]
    with service.state.lock:
        consume = [r for r in service.state.consume.values() if start <= r["t"] <= now]
        errors = [r for r in service.state.errors.values() if start <= r["t"] <= now]
        per_usd = service.state.quota_per_usd
        groups = sorted(service.state.groups)
        earliest = min((r["t"] for r in service.state.consume.values()), default=None)
        synced_at, sync_error = service.state.synced_at, service.state.sync_error
    limits = {(g, l["provider"]): l["limit"] for g, ls in service.limits().items() for l in ls
              if (group == "all" or g == group) and (provider == "all" or l["provider"] == provider)}

    # Only groups that still have channels: renamed or removed test groups stay out.
    def keep(r):
        return (r["group"] in groups and (provider == "all" or r["provider"] == provider)
                and (group == "all" or r["group"] == group))

    consume = [r for r in consume if keep(r)]
    errors = [r for r in errors if keep(r)]

    size = {"today": 3600, "24h": 3600, "7d": 10800}.get(period, 86400)
    origin = service.midnight(start)
    first, last = (start - origin) // size, (now - origin) // size
    buckets = [origin + i * size for i in range(first, last + 1)]
    series = {g: {"cost": [0.0] * len(buckets), "requests": [0] * len(buckets), "output": [0] * len(buckets)}
              for g in groups}

    dims = {"group": lambda r: (r["group"], r["provider"]), "user": lambda r: (r["user"], r["group"]),
            "model": lambda r: (r["model"], r["provider"]), "key": lambda r: (r["key"], r["user"])}
    tables = {d: {} for d in dims}
    totals = empty_totals()

    def add(row, r, cost):
        row["requests"] += 1
        for f in ("input", "cache_read", "cache_write", "output"):
            row[f] += r[f]
        row["cost"] += cost
        row["last"] = max(row["last"], r["t"])

    for r in consume:
        cost = r["quota"] / per_usd
        add(totals, r, cost)
        for d, key in dims.items():
            add(tables[d].setdefault(key(r), empty_totals()), r, cost)
        if r["group"] in series:
            i = (r["t"] - origin) // size - first
            if 0 <= i < len(buckets):
                s = series[r["group"]]
                s["cost"][i] += cost
                s["requests"][i] += 1
                s["output"][i] += r["output"]
    for r in errors:
        totals["errors"] += 1
        for d, key in dims.items():
            row = tables[d].setdefault(key(r), empty_totals())
            row["errors"] += 1
            row["last"] = max(row["last"], r["t"])

    # Error messages grouped per user, group and provider, most frequent first (the daily report).
    reasons = {}
    for r in errors:
        k = (r["user"], r["group"], r["provider"], r["message"][:200])
        reasons[k] = reasons.get(k, 0) + 1
    error_reasons = [{"user": k[0], "group": k[1], "provider": k[2], "message": k[3], "count": n}
                     for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:30]]

    recent = sorted([{**r, "ok": True, "cost": r["quota"] / per_usd} for r in consume]
                    + [{**r, "ok": False} for r in errors], key=lambda r: -r["t"])[:80]
    for r in recent:
        r.pop("quota", None)
    return {
        "now": now, "start": start, "period": period, "bucket": size, "buckets": buckets,
        "groups": groups, "series": series, "totals": totals,
        "tables": {d: [{"a": k[0], "b": k[1], **v} for k, v in rows.items()] for d, rows in tables.items()},
        "recent": recent, "error_reasons": error_reasons,
        "profiles": profiles(consume, errors, per_usd),
        "throttles": service.throttles(start, now, limits), "earliest": earliest, "synced_at": synced_at, "sync_error": sync_error,
    }

def percentile(sorted_values, q):
    if not sorted_values:
        return 0
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]

def profiles(consume, errors, per_usd):
    """How each user works, per group and provider (the daily report's advice): context size per
    request (uncached input + cache read + cache write), cache hit rate, output, latency, models."""
    rows = {}
    for r in consume:
        p = rows.setdefault((r["user"], r["group"], r["provider"]), {"ctx": [], "models": {}, "hours": set(),
            "cache_read": 0, "cache_write": 0, "input": 0, "output": 0, "dur": 0, "cost": 0.0, "errors": 0})
        p["ctx"].append(r["input"] + r["cache_read"] + r["cache_write"])
        p["models"][r["model"]] = p["models"].get(r["model"], 0) + 1
        p["hours"].add(r["t"] // 3600)
        for f in ("cache_read", "cache_write", "input", "output", "dur"):
            p[f] += r[f]
        p["cost"] += r["quota"] / per_usd
    for r in errors:
        if (r["user"], r["group"], r["provider"]) in rows:
            rows[(r["user"], r["group"], r["provider"])]["errors"] += 1
    out = []
    for (user, group, provider), p in rows.items():
        ctx, n = sorted(p["ctx"]), len(p["ctx"])
        prompt = p["input"] + p["cache_read"] + p["cache_write"]
        out.append({"user": user, "group": group, "provider": provider, "requests": n, "errors": p["errors"],
                    "active_hours": len(p["hours"]), "ctx_avg": prompt // n, "ctx_p50": percentile(ctx, 0.5),
                    "ctx_p90": percentile(ctx, 0.9), "ctx_max": ctx[-1],
                    "ctx_over_200k": sum(1 for c in ctx if c > 200_000),
                    "cache_hit": round(p["cache_read"] / prompt, 3) if prompt else 0,
                    "cache_write_share": round(p["cache_write"] / prompt, 3) if prompt else 0,
                    "out_avg": p["output"] // n, "dur_avg": round(p["dur"] / n, 1),
                    "cost": round(p["cost"], 4), "models": p["models"]})
    return sorted(out, key=lambda r: -r["cost"])

def lingqu_calibration(res, now):
    """group -> account kind -> quota window ("short" / "weekly") -> what one full window is worth
    in API-list-price USD, from Lingqu's quota calibration (Lingqu docs/M4.md).

    max_usd is the highest value over the cycles (finished ones and the current one) seen in the
    last CALIBRATION_DAYS days whose percent rose by at least min_pct (CALIBRATION_MIN_PCT).
    Traffic that bypasses the gateway raises the percent but not the spend, so it only lowers a
    cycle's value; the highest 5-hour value is the cycle it touched least. A weekly window takes in
    a whole week of such traffic, so its value is derived instead: the highest 5-hour value divided
    by how far the weekly percent rises per 5-hour percent, summed over the 5-hour cycles that rose
    at least RATIO_MIN_SHORT_PCT. That
    ratio holds whoever spends."""
    out, cutoff = {}, now - CALIBRATION_DAYS * 86400
    for p in (res or {}).get("pools") or []:
        if "/" not in p.get("name", ""):
            continue
        group, _ = p["name"].rsplit("/", 1)
        prov = p.get("kind", p["name"].rsplit("/", 1)[1])
        windows = {}
        for c in (p.get("quota") or {}).get("calibration") or []:
            min_pct = CALIBRATION_MIN_PCT.get(c["window"], CALIBRATION_MIN_PCT_DEFAULT)
            counted = [h for h in (c.get("history") or []) + [c]
                       if (h.get("delta_pct") or 0) >= min_pct and (h.get("ts") or 0) >= cutoff]
            cycles = [h for h in counted if h.get("suggested_limit")]
            best = max(cycles, key=lambda h: h["suggested_limit"], default=None)
            rises = [(h["delta_pct"], h["peers"]["weekly"]["delta_pct"]) for h in counted
                     if h["delta_pct"] >= RATIO_MIN_SHORT_PCT
                     and ((h.get("peers") or {}).get("weekly") or {}).get("delta_pct") is not None]
            windows[c["window"]] = {
                "account": c.get("account"), "pools": c.get("pools") or [], "start_ts": c.get("start_ts"),
                "start_pct": c.get("start_pct"), "used_pct": c.get("used_pct"), "eusd": c.get("eusd") or 0,
                "per_percent": c.get("eusd_per_percent"), "window_usd": c.get("suggested_limit"),
                "max_usd": best and best["suggested_limit"], "max_pct": best and best["delta_pct"],
                "max_start_ts": best and best["start_ts"], "max_n": len(cycles), "min_pct": min_pct,
                "since_ts": min((h["start_ts"] for h in cycles), default=None),
                "rise_short": sum(s for s, _ in rises), "rise_weekly": sum(w for _, w in rises),
                "rise_n": len(rises)}
        short, weekly = windows.get("short"), windows.get("weekly")
        if short and weekly and short["max_usd"] and short["rise_weekly"] >= RATIO_MIN_WEEKLY_PCT:
            ratio = short["rise_weekly"] / short["rise_short"]
            weekly["derived"] = {"usd": short["max_usd"] / ratio, "ratio": ratio, "short_usd": short["max_usd"],
                                 "n": short["rise_n"], "direct_usd": weekly["max_usd"]}
        out.setdefault(group, {})["claude" if prov == "claude" else "codex"] = windows
    return out

def lingqu_limits(res):
    """Present the actual gate state in the legacy usage card schema, in eUSD."""
    out = {}
    for p in (res or {}).get("pools") or []:
        if not p.get("limit") or "/" not in p.get("name", ""):
            continue
        group, _ = p["name"].rsplit("/", 1)
        prov = p.get("kind", p["name"].rsplit("/", 1)[1])
        queuing = p.get("state") == "queuing"
        out.setdefault(group, []).append({
            "provider": "claude" if prov == "claude" else "codex", "by": "lingqu",
            "limit": p["limit"], "spent": p.get("spent_1h") or 0, "ratio": p.get("ratio") or 0,
            "state": p.get("state", "open"), "off_sec": None, "on_sec": None, "phase_until": None,
            "gap_sec": (p.get("step") or {}).get("gap") if queuing else None, "held": p.get("held") or 0,
            "wait_p50": p.get("wait_p50_1h"), "wait_max": p.get("wait_max_1h"),
            "clear_at": p.get("clear_at") if queuing else None})
    return out
