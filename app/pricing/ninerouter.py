"""Prices from a 9router's GET /api/pricing (DESIGN §3.2): dashboard password -> auth_token cookie.

The session is reused until it expires; after a failed login nothing is tried for 30 minutes,
because 9router locks the client out after repeated failures.
"""
import threading
import hashlib
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
        self.shared = None

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
            if not self.shared:
                return self._get(path)
            from redis import Redis
            redis = Redis.from_url(self.shared["url"], socket_timeout=self.shared["socket_timeout"])
            # The hash isolates credentials without storing the dashboard password in Redis.
            identity = hashlib.sha256((self.base_url + "\0" + self.password).encode()).hexdigest()
            prefix = "{" + self.shared["namespace"] + "}:router-session:" + identity
            try:
                with redis.lock(prefix + ":lock", timeout=75, blocking_timeout=5):
                    cookie = redis.get(prefix + ":cookie")
                    failed = redis.get(prefix + ":failed")
                    self.cookie = cookie.decode() if cookie else None
                    self.failed_at = float(failed) if failed else None
                    try:
                        return self._get(path)
                    finally:
                        if self.cookie:
                            redis.set(prefix + ":cookie", self.cookie, ex=3600)
                        else:
                            redis.delete(prefix + ":cookie")
                        if self.failed_at is not None:
                            redis.set(prefix + ":failed", self.failed_at, ex=LOGIN_RETRY_SEC)
                        else:
                            redis.delete(prefix + ":failed")
            finally:
                redis.close()

    def _get(self, path):
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
