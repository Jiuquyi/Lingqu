"""Prices from a 9router's GET /api/pricing (DESIGN §3.2): dashboard password -> auth_token cookie.

The session is reused until it expires; after a failed login nothing is tried for 30 minutes,
because 9router locks the client out after repeated failures.
"""
import threading
import time

import httpx

LOGIN_RETRY_SEC = 1800


def to_prices(body):
    """{provider: {model: {input, output, cached, cache_creation, reasoning}}} -> our keys."""
    out = {}
    for provider, models in (body or {}).items():
        if not isinstance(models, dict):
            continue
        for model, p in models.items():
            if not isinstance(p, dict) or "input" not in p:
                continue
            price = {"input": p.get("input"), "output": p.get("output", p.get("input"))}
            if p.get("cached") is not None:
                price["cache_read"] = p["cached"]
            if p.get("cache_creation") is not None:
                price["cache_write_5m"] = p["cache_creation"]
            out.setdefault(provider, {})[model] = price
    return out


class Session:
    def __init__(self, base_url, password, clock=time.time):
        self.base_url, self.password, self.clock = base_url.rstrip("/"), password, clock
        self.cookie, self.failed_at = None, None
        self.lock = threading.Lock()

    def login(self):
        if self.failed_at is not None and self.clock() - self.failed_at < LOGIN_RETRY_SEC:
            raise RuntimeError("9router login failed recently; waiting before trying again")
        self.failed_at = self.clock()  # network failures also observe the login cooldown
        r = httpx.post(self.base_url + "/api/auth/login", json={"password": self.password}, timeout=15, trust_env=False)
        token = r.cookies.get("auth_token")
        if r.status_code != 200 or not token:
            self.failed_at = self.clock()
            raise RuntimeError(f"9router login failed: HTTP {r.status_code}")
        self.failed_at, self.cookie = None, token

    def get(self, path):
        with self.lock:
            if not self.cookie:
                self.login()
            r = httpx.get(self.base_url + path, cookies={"auth_token": self.cookie}, timeout=15, trust_env=False)
            if r.status_code == 401:
                self.cookie = None
                self.login()
                r = httpx.get(self.base_url + path, cookies={"auth_token": self.cookie}, timeout=15, trust_env=False)
            r.raise_for_status()
            return r.json()


def fetch(session):
    prices = to_prices(session.get("/api/pricing"))
    if not prices:
        raise RuntimeError("9router /api/pricing returned no prices")
    return prices
