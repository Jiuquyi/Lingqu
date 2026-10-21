"""Load and validate lingqu.yaml.

The file is the single source of rules (DESIGN §8). Secrets come in as ${ENV} references.
`load()` never raises on bad content: it returns a Settings object together with the list of
errors and warnings, so `flask check` can report everything at once and a reload can keep the
old settings when the new ones are bad.
"""
import fnmatch
import os
import re
from dataclasses import dataclass, field

import yaml

KINDS = ("claude", "codex")
RULE_KINDS = ("hourly_cap", "concurrency", "rpm")
LATER_RULE_KINDS = ("hourly_budget", "cycle_cap")  # third version: accepted, not enforced
ON_EXCEED = ("hold", "reject", "shadow")
RELEASES = ("fifo", "phase")

DEFAULT_FIFO = [(1.0, 15), (1.25, 22.5), (1.5, 30), (2.0, 45), (2.5, 60)]
DEFAULT_PHASE = [(1.0, 45, 60), (1.25, 75, 50), (1.5, 100, 45), (2.0, 120, 45), (2.5, 120, 30)]
DEFAULT_PRICE = {"claude": {"input": 5, "output": 25}, "codex": {"input": 4, "output": 20}}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_UNITS = {None: 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_duration(value):
    """Seconds from 150, "150s", "10m", "4h" or "7d"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = _DURATION.match(str(value))
    if not m:
        raise ValueError(f"bad duration {value!r}")
    return float(m.group(1)) * _UNITS[m.group(2)]


@dataclass
class Rule:
    kind: str
    on_exceed: str = "hold"
    release: str = "fifo"
    shadow: bool = False
    limit: float = 0.0          # hourly_cap: eUSD per rolling hour
    max: int = 0                # concurrency: requests in flight; rpm: requests per minute
    scope_model: str | None = None
    session_aware: bool = False  # hourly_cap with fifo: warm requests first, cold ones wait longer

    @property
    def scope_key(self):
        return f"model={self.scope_model}" if self.scope_model else "*"

    def applies(self, model):
        return self.scope_model is None or fnmatch.fnmatchcase(model or "", self.scope_model)


@dataclass
class Pool:
    name: str
    kind: str
    upstream: str
    keys: list = field(default_factory=list)
    match: list = field(default_factory=list)
    rules: list = field(default_factory=list)

    def matches(self, model):
        return any(fnmatch.fnmatchcase(model or "", p) for p in self.match)


@dataclass
class Dashboard:
    port: int | None = None
    host: str | None = None
    default_ttl: float = 1800
    max_ttl: float = 4 * 3600
    allow_persistent: bool = True
    openers: list = field(default_factory=list)  # [(name, token)]


@dataclass
class Upstream:
    name: str
    base_url: str
    api_key: str
    dashboard: Dashboard | None = None


@dataclass
class KeyBinding:
    """What one lq- key opens: a single pool, or a set of pools on one upstream picked by model."""
    upstream: str
    pool: str | None = None
    pools: list = field(default_factory=list)


@dataclass
class Settings:
    admin_token: str = ""
    fail_mode: str = "open"
    proxy_port: int = 8090
    status_port: int = 8091
    dsn: str = ""
    pricing: dict = field(default_factory=dict)
    max_hold: float = 150
    max_queue: int = 8
    hold_cap_cached: float = 120
    hold_cap_cold: float = 300
    cache_ttl: float = 300
    default_price: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PRICE.items()})
    fifo: list = field(default_factory=lambda: list(DEFAULT_FIFO))
    phase: list = field(default_factory=lambda: list(DEFAULT_PHASE))
    upstreams: dict = field(default_factory=dict)
    pools: dict = field(default_factory=dict)
    keys: dict = field(default_factory=dict)  # key -> KeyBinding
    webhook: str = ""
    notify_throttle: bool = True
    auth_streak: int = 2        # account_auth / router_unreachable responses in a row before an alert
    path: str = ""

    def dashboard_ports(self):
        return sorted({u.dashboard.port for u in self.upstreams.values() if u.dashboard and u.dashboard.port})


class _Loader:
    def __init__(self):
        self.errors, self.warnings, self.missing_env = [], [], set()

    def env(self, value):
        """Expand ${VAR} in every string of a parsed YAML tree."""
        if isinstance(value, str):
            def sub(m):
                if m.group(1) not in os.environ:
                    self.missing_env.add(m.group(1))
                    return ""
                return os.environ[m.group(1)]
            return _ENV.sub(sub, value)
        if isinstance(value, list):
            return [self.env(v) for v in value]
        if isinstance(value, dict):
            return {k: self.env(v) for k, v in value.items()}
        return value

    def duration(self, where, value, default):
        if value is None:
            return default
        try:
            return parse_duration(value)
        except ValueError as e:
            self.errors.append(f"{where}: {e}")
            return default

    def number(self, where, value, default, positive=True):
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.errors.append(f"{where}: expected a number, got {value!r}")
            return default
        if positive and value <= 0:
            self.errors.append(f"{where}: must be greater than 0")
        return value


def load(path=None, text=None):
    """(Settings, errors, warnings) from a file or a YAML string."""
    ld = _Loader()
    s = Settings(path=path or "")
    try:
        if text is None:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        raw = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as e:
        return s, [f"cannot read config: {e}"], []
    if not isinstance(raw, dict):
        return s, ["config: top level must be a mapping"], []
    raw = ld.env(raw)
    _server(ld, s, raw.get("server") or {})
    s.dsn = ((raw.get("storage") or {}).get("dsn") or "").strip()
    _pricing(ld, s, raw.get("pricing") or {})
    _defaults(ld, s, raw.get("defaults") or {})
    s.fifo = _steps(ld, "fifo", raw.get("fifo"), DEFAULT_FIFO, ("at", "gap"))
    s.phase = _steps(ld, "phase", raw.get("phase"), DEFAULT_PHASE, ("at", "off", "on"))
    _upstreams(ld, s, raw.get("upstreams") or [])
    _pools(ld, s, raw.get("pools") or [])
    _keys(ld, s, raw.get("keys") or [])
    notify = raw.get("notify") or {}
    s.webhook = (notify.get("webhook") or "").strip()
    s.notify_throttle = bool(notify.get("throttle_events", True))
    s.auth_streak = int(ld.number("notify.auth_streak", notify.get("auth_streak"), 2))
    _cross_checks(ld, s)
    for var in sorted(ld.missing_env):
        ld.errors.append(f"environment variable {var} is not set")
    return s, ld.errors, ld.warnings


def _server(ld, s, raw):
    s.admin_token = raw.get("admin_token") or ""
    s.fail_mode = raw.get("fail_mode", "open")
    if s.fail_mode not in ("open", "closed"):
        ld.errors.append(f"server.fail_mode: {s.fail_mode!r} is not open or closed")
    s.proxy_port = int(ld.number("server.proxy_port", raw.get("proxy_port"), 8090))
    s.status_port = int(ld.number("server.status_port", raw.get("status_port"), 8091))
    if not s.admin_token:
        ld.warnings.append("server.admin_token is empty: admin endpoints are disabled")


def _pricing(ld, s, raw):
    source = raw.get("source", "static")
    if source not in ("newapi", "ninerouter", "static"):
        ld.errors.append(f"pricing.source: {source!r} is not newapi, ninerouter or static")
    p = {"source": source,
         "refresh": ld.duration("pricing.refresh", raw.get("refresh"), 600),
         "strip_prefixes": list(raw.get("strip_prefixes") or ["cc/", "cx/", "os/cx/"]),
         "overrides": raw.get("overrides") or {},
         "static": raw.get("static") or {},
         "newapi": raw.get("newapi") or {},
         "ninerouter": raw.get("ninerouter") or {}}
    if source == "newapi" and not p["newapi"].get("base_url"):
        ld.errors.append("pricing.newapi.base_url is required when source is newapi")
    if source == "ninerouter" and not (p["ninerouter"].get("base_url") or p["ninerouter"].get("upstream")):
        ld.errors.append("pricing.ninerouter needs base_url or upstream when source is ninerouter")
    for section in ("overrides", "static"):
        for model, price in p[section].items():
            if not isinstance(price, dict) or "input" not in price or "output" not in price:
                ld.errors.append(f"pricing.{section}.{model}: needs at least input and output ($ / 1M tokens)")
    s.pricing = p


def _defaults(ld, s, raw):
    s.max_hold = ld.duration("defaults.max_hold", raw.get("max_hold"), 150)
    s.max_queue = int(ld.number("defaults.max_queue", raw.get("max_queue"), 8))
    s.hold_cap_cached = ld.duration("defaults.hold_cap_cached", raw.get("hold_cap_cached"), 120)
    s.hold_cap_cold = ld.duration("defaults.hold_cap_cold", raw.get("hold_cap_cold"), 300)
    s.cache_ttl = ld.duration("defaults.cache_ttl", raw.get("cache_ttl"), 300)
    if s.hold_cap_cold < s.hold_cap_cached:
        ld.warnings.append("defaults.hold_cap_cold is shorter than hold_cap_cached: in a session-aware pool "
                           "a request that rebuilds the cache anyway would wait less than one that hits it")
    if not 60 <= s.cache_ttl <= 3600:
        ld.errors.append("defaults.cache_ttl: must be between 60 s and 1 h (the prompt cache lifetime)")
    if s.hold_cap_cached > s.max_hold:
        ld.warnings.append("defaults.hold_cap_cached is longer than max_hold: held requests get a 503 "
                           "before the queue-head timeout lets them through")
    if s.hold_cap_cached > 150:
        ld.warnings.append("defaults.hold_cap_cached above 150 s risks letting the 5-minute prompt cache expire")
    for kind, price in (raw.get("default_price") or {}).items():
        if kind not in KINDS or not isinstance(price, dict) or "input" not in price or "output" not in price:
            ld.errors.append(f"defaults.default_price.{kind}: needs a pool kind and input / output prices")
        else:
            s.default_price[kind] = price


def _steps(ld, name, raw, default, keys):
    if raw is None:
        return list(default)
    steps = []
    if not isinstance(raw, list) or not raw:
        ld.errors.append(f"{name}: must be a non-empty list of steps")
        return list(default)
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or any(k not in item for k in keys):
            ld.errors.append(f"{name}[{i}]: needs {', '.join(keys)}")
            return list(default)
        try:
            steps.append(tuple(float(item[k]) if k == "at" else parse_duration(item[k]) for k in keys))
        except (TypeError, ValueError) as e:
            ld.errors.append(f"{name}[{i}]: {e}")
            return list(default)
    if steps[0][0] != 1.0:
        ld.errors.append(f"{name}: the first step must start at at = 1.0")
    if any(b[0] <= a[0] for a, b in zip(steps, steps[1:])):
        ld.errors.append(f"{name}: at must increase from step to step")
    if any(v <= 0 for st in steps for v in st[1:]):
        ld.errors.append(f"{name}: durations must be greater than 0")
    if name == "fifo":
        for (at, gap), (_, d) in zip(steps, DEFAULT_FIFO + [DEFAULT_FIFO[-1]] * len(steps)):
            if gap > 1.5 * d:
                ld.warnings.append(f"fifo: gap {gap:g} s at {at:g} is over 1.5 x the default ({d:g} s); "
                                   "cache expiry starts costing more than the gap saves")
    return steps


def _upstreams(ld, s, raw):
    if not isinstance(raw, list):
        ld.errors.append("upstreams: must be a list")
        return
    for i, u in enumerate(raw):
        name = (u or {}).get("name")
        if not name:
            ld.errors.append(f"upstreams[{i}]: name is required")
            continue
        if name in s.upstreams:
            ld.errors.append(f"upstreams: duplicate name {name}")
            continue
        if not u.get("base_url"):
            ld.errors.append(f"upstream {name}: base_url is required")
        if not u.get("api_key"):
            ld.errors.append(f"upstream {name}: api_key is empty")
        dash = None
        if u.get("dashboard"):
            d = u["dashboard"]
            openers = []
            for j, o in enumerate(d.get("openers") or []):
                if isinstance(o, dict):
                    openers.append((o.get("name") or f"{name}-opener-{j + 1}", o.get("token") or ""))
                else:
                    openers.append((f"{name}-opener-{j + 1}", o or ""))
            for oname, token in openers:
                if not token:
                    ld.errors.append(f"upstream {name}: dashboard opener {oname} has an empty token")
            dash = Dashboard(port=d.get("port"), host=d.get("host"),
                             default_ttl=ld.duration(f"upstream {name}: dashboard.default_ttl", d.get("default_ttl"), 1800),
                             max_ttl=ld.duration(f"upstream {name}: dashboard.max_ttl", d.get("max_ttl"), 4 * 3600),
                             allow_persistent=bool(d.get("allow_persistent", True)), openers=openers)
            if not dash.port and not dash.host:
                ld.errors.append(f"upstream {name}: dashboard needs a port or a host")
            if dash.default_ttl > dash.max_ttl:
                ld.errors.append(f"upstream {name}: dashboard.default_ttl is longer than max_ttl")
        s.upstreams[name] = Upstream(name=name, base_url=(u.get("base_url") or "").rstrip("/"),
                                     api_key=u.get("api_key") or "", dashboard=dash)


def _rule(ld, pool, i, raw):
    where = f"pool {pool}: rules[{i}]"
    kind = (raw or {}).get("kind")
    if kind in LATER_RULE_KINDS:
        ld.warnings.append(f"{where}: {kind} is not implemented in this version and is ignored")
        return None
    if kind not in RULE_KINDS:
        ld.errors.append(f"{where}: unknown kind {kind!r}")
        return None
    on_exceed = raw.get("on_exceed", "hold")
    if on_exceed == "downgrade":
        ld.errors.append(f"{where}: on_exceed downgrade is not implemented yet")
        return None
    if on_exceed not in ON_EXCEED:
        ld.errors.append(f"{where}: on_exceed {on_exceed!r} is not one of {', '.join(ON_EXCEED)}")
    release = raw.get("release", "fifo")
    if release not in RELEASES:
        ld.errors.append(f"{where}: release {release!r} is not fifo or phase")
    mode = raw.get("mode", "enforce")
    if mode not in ("enforce", "shadow"):
        ld.errors.append(f"{where}: mode {mode!r} is not enforce or shadow")
    scope = raw.get("scope") or {}
    if set(scope) - {"model"}:
        ld.errors.append(f"{where}: only a model scope is supported")
    aware = raw.get("session_aware", False)
    if not isinstance(aware, bool):
        ld.errors.append(f"{where}: session_aware must be true or false")
        aware = False
    if aware and (kind != "hourly_cap" or release != "fifo"):
        ld.errors.append(f"{where}: session_aware applies only to an hourly_cap with release fifo")
        aware = False
    r = Rule(kind=kind, on_exceed=on_exceed, release=release,
             shadow=mode == "shadow" or on_exceed == "shadow", scope_model=scope.get("model"),
             session_aware=aware)
    if kind == "hourly_cap":
        r.limit = float(ld.number(f"{where}: limit", raw.get("limit"), 0) or 0)
        if not raw.get("limit"):
            ld.errors.append(f"{where}: hourly_cap needs a limit")
    else:
        r.max = int(ld.number(f"{where}: max", raw.get("max"), 0) or 0)
        if not raw.get("max"):
            ld.errors.append(f"{where}: {kind} needs a max")
    return r


def _pools(ld, s, raw):
    if not isinstance(raw, list):
        ld.errors.append("pools: must be a list")
        return
    for i, p in enumerate(raw):
        name = (p or {}).get("name")
        if not name:
            ld.errors.append(f"pools[{i}]: name is required")
            continue
        if name in s.pools:
            ld.errors.append(f"pools: duplicate name {name}")
            continue
        kind = p.get("kind")
        if kind not in KINDS:
            ld.errors.append(f"pool {name}: kind must be claude or codex")
        if p.get("upstream") not in s.upstreams:
            ld.errors.append(f"pool {name}: upstream {p.get('upstream')!r} is not defined")
        rules = [r for r in (_rule(ld, name, j, x) for j, x in enumerate(p.get("rules") or [])) if r]
        caps = [r for r in rules if r.kind == "hourly_cap" and not r.shadow]
        if len({r.release for r in caps}) > 1:
            ld.errors.append(f"pool {name}: all enforcing hourly_cap rules must use the same release")
        if len({r.session_aware for r in caps}) > 1:
            ld.errors.append(f"pool {name}: set session_aware on all of its enforcing hourly_cap rules or none")
        if len([r for r in rules if r.kind == "hourly_cap" and r.scope_model is None]) > 1:
            ld.errors.append(f"pool {name}: more than one pool-wide hourly_cap")
        match = (p.get("match") or {}).get("models") or []
        keys = [k for k in (p.get("keys") or [])]
        if any(not k for k in keys):
            ld.errors.append(f"pool {name}: a key is empty")
        s.pools[name] = Pool(name=name, kind=kind, upstream=p.get("upstream"),
                             keys=[k for k in keys if k], match=list(match), rules=rules)
        for k in keys:
            if not k:
                continue
            if k in s.keys:
                ld.errors.append(f"pool {name}: a key is already bound to another pool")
            else:
                s.keys[k] = KeyBinding(upstream=p.get("upstream"), pool=name)


def _keys(ld, s, raw):
    for i, k in enumerate(raw):
        key, pools = (k or {}).get("key"), (k or {}).get("upstream_pools") or []
        if not key:
            ld.errors.append(f"keys[{i}]: key is empty")
            continue
        if key in s.keys:
            ld.errors.append(f"keys[{i}]: the key is already bound")
            continue
        missing = [p for p in pools if p not in s.pools]
        if not pools or missing:
            ld.errors.append(f"keys[{i}]: upstream_pools must name defined pools (missing: {', '.join(missing) or '-'})")
            continue
        ups = {s.pools[p].upstream for p in pools}
        if len(ups) != 1:
            ld.errors.append(f"keys[{i}]: all upstream_pools must share one upstream")
            continue
        for p in pools:
            if not s.pools[p].match:
                ld.errors.append(f"keys[{i}]: pool {p} has no match.models, so no model can reach it")
        s.keys[key] = KeyBinding(upstream=ups.pop(), pools=list(pools))


def _cross_checks(ld, s):
    ports = {s.proxy_port: "server.proxy_port", s.status_port: "server.status_port"}
    if s.proxy_port == s.status_port:
        ld.errors.append("server.proxy_port and server.status_port must differ")
    seen_host = {}
    for u in s.upstreams.values():
        d = u.dashboard
        if not d:
            continue
        if d.port and d.port in ports and not ports[d.port].startswith("dashboard"):
            ld.errors.append(f"upstream {u.name}: dashboard.port {d.port} clashes with {ports[d.port]}")
        if d.port and not d.host:
            if ports.get(d.port, "").startswith("dashboard"):
                ld.errors.append(f"upstream {u.name}: dashboard.port {d.port} is already used; "
                                 "share a port only with distinct hosts")
            ports[d.port] = f"dashboard {u.name}"
        if d.host:
            key = (d.host.lower(), d.port)
            if key in seen_host:
                ld.errors.append(f"upstream {u.name}: dashboard host {d.host} is already used by {seen_host[key]}")
            seen_host[key] = u.name
    for p in s.pools.values():
        if not p.keys and not any(p.name in b.pools for b in s.keys.values()):
            ld.warnings.append(f"pool {p.name}: no key reaches it")
