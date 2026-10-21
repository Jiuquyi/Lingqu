"""Aggregates over the ledger for GET /stats (DESIGN §6.3)."""
import collections
import time

from ..models import Ledger

PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def period_start(period, now=None):
    now = time.time() if now is None else now
    if period == "today":
        t = time.localtime(now)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return now - PERIODS.get(period, 86400)


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def summarize(rows):
    ctx = [r.input_tokens + r.cache_read_tokens + r.cache_write_5m_tokens + r.cache_write_1h_tokens for r in rows]
    total_ctx = sum(ctx)
    cache_read = sum(r.cache_read_tokens for r in rows)
    cache_write = sum(r.cache_write_5m_tokens + r.cache_write_1h_tokens for r in rows)
    held = [r.held_ms / 1000 for r in rows if (r.held_ms or 0) > 0]
    errors = collections.Counter(r.error_class for r in rows if r.error_class)
    return {
        "requests": len(rows), "errors": sum(errors.values()), "error_classes": dict(errors),
        "tokens": {"input": sum(r.input_tokens for r in rows), "cache_read": cache_read,
                   "cache_write": cache_write, "output": sum(r.output_tokens for r in rows)},
        "eusd": round(sum(r.eusd or 0 for r in rows), 4),
        "context": {"avg": round(total_ctx / len(ctx)) if ctx else None, "p50": pct(ctx, 0.5),
                    "p90": pct(ctx, 0.9), "max": max(ctx) if ctx else None,
                    "over_200k": sum(1 for c in ctx if c > 200_000)},
        "cache_hit_rate": round(cache_read / total_ctx, 4) if total_ctx else None,
        "cache_write_share": round(cache_write / total_ctx, 4) if total_ctx else None,
        "held": {"count": len(held), "p50": pct(held, 0.5), "p90": pct(held, 0.9),
                 "timeouts": sum(1 for r in rows if r.gate_result == "rejected")},
    }


def stats(period="24h", pool=None, now=None):
    now = time.time() if now is None else now
    start = period_start(period, now)
    q = Ledger.query.filter(Ledger.ts >= start)
    if pool:
        q = q.filter(Ledger.pool == pool)
    rows = q.all()
    by = {"pool": collections.defaultdict(list), "model": collections.defaultdict(list),
          "session": collections.defaultdict(list)}
    step = 3600 if period in ("today", "24h") else 86400
    series = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        by["pool"][r.pool].append(r)
        by["model"][r.model_actual or r.model_req or "-"].append(r)
        if r.session_tag:
            by["session"][r.session_tag].append(r)
        series[r.pool][int(r.ts // step * step)] += r.eusd or 0
    return {
        "period": period, "from": int(start), "to": int(now),
        "total": summarize(rows),
        "pools": {k: summarize(v) for k, v in sorted(by["pool"].items())},
        "models": {k: summarize(v) for k, v in sorted(by["model"].items())},
        "sessions": {k: summarize(v) for k, v in sorted(by["session"].items(), key=lambda kv: -len(kv[1]))[:50]},
        "series": {"step": step, "pools": {p: sorted((t, round(v, 4)) for t, v in s.items())
                                           for p, s in series.items()}},
    }
