"""Prices from New API's GET /api/pricing (DESIGN §3.2).

Read with a personal access token, never by logging in: login sessions are capped per user,
so a long-running process that logs in on every expiry eventually locks the admin out.
"""
import httpx


def to_prices(items):
    """New API ratios -> $ / 1M tokens. Ratio 1 = $2 per 1M input tokens. Group ratios are left
    out on purpose: eUSD measures the upstream subscription, not what a user is charged."""
    prices = {}
    for it in items or []:
        name = it.get("model_name")
        if not name:
            continue
        if it.get("quota_type") == 1 or (it.get("model_price") and not it.get("model_ratio")):
            prices[name] = {"input": 0, "output": 0, "per_request": float(it.get("model_price") or 0)}
            continue
        p_in = float(it.get("model_ratio") or 0) * 2
        price = {"input": p_in, "output": p_in * float(it.get("completion_ratio") or 1)}
        if it.get("cache_ratio") is not None:
            price["cache_read"] = p_in * float(it["cache_ratio"])
        if it.get("create_cache_ratio") is not None:
            price["cache_write_5m"] = p_in * float(it["create_cache_ratio"])
        prices[name] = price
    return prices


def fetch(base_url, token="", timeout=15):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.get(base_url.rstrip("/") + "/api/pricing", headers=headers, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if body.get("success") is False:
        raise RuntimeError(f"New API /api/pricing: {body.get('message') or 'failed'}")
    prices = to_prices(body.get("data"))
    if not prices:
        raise RuntimeError("New API /api/pricing returned no models (pricing page closed and no token?)")
    return prices
