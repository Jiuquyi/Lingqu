"""`flask check` (DESIGN §8.2): the config, then every link of the chain, each a line of output;
any failure makes the command exit non-zero."""
import time

import httpx

from . import db
from .dashboard.gateway import DashboardGateway
from .pricing.table import PriceTable


class Report:
    def __init__(self, echo):
        self.echo, self.failed, self.warned = echo, 0, 0

    def ok(self, msg):
        self.echo(f"  ok    {msg}")

    def warn(self, msg):
        self.warned += 1
        self.echo(f"  warn  {msg}")

    def fail(self, msg):
        self.failed += 1
        self.echo(f"  FAIL  {msg}")


def run(app, echo, live=False, path=None):
    from . import settings as settings_mod
    rt = app.extensions["lingqu"]
    r = Report(echo)

    echo("1. config")
    s, errors, warnings = settings_mod.load(path or rt.settings.path)
    for e in errors:
        r.fail(e)
    for w in warnings:
        r.warn(w)
    if errors:
        return r
    r.ok(f"{s.path}: {len(s.upstreams)} upstreams, {len(s.pools)} pools, {len(s.keys)} keys")

    if s.redis:
        try:
            from redis import Redis
            client = Redis.from_url(s.redis["url"], socket_timeout=s.redis["socket_timeout"])
            client.ping()
            client.close()
            r.ok("Redis reachable; shared admission enabled")
        except Exception:
            r.fail("Redis unreachable; shared admission will reject new requests")

    echo("2. upstreams")
    models = {}
    for u in s.upstreams.values():
        try:
            resp = httpx.get(u.base_url + "/v1/models", headers={"Authorization": f"Bearer {u.api_key}"},
                             timeout=10, trust_env=False)
            if resp.status_code == 200:
                models[u.name] = [m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)]
                r.ok(f"{u.name}: /v1/models 200, {len(models[u.name])} models")
            else:
                r.fail(f"{u.name}: /v1/models returned {resp.status_code}")
        except (httpx.HTTPError, ValueError) as e:
            r.fail(f"{u.name}: {e}")

    echo("3. dashboard gates")
    for u in s.upstreams.values():
        if not u.dashboard:
            continue
        gw = DashboardGateway(rt, u.name)
        base = {"REQUEST_METHOD": "GET", "QUERY_STRING": "", "wsgi.input": None, "HTTP_HOST": "check"}
        status, _, _ = gw.handle({**base, "PATH_INFO": "/v1/models"})
        if status.startswith("404"):
            r.ok(f"{u.name}: LLM path /v1/models refused on the gate")
        else:
            r.fail(f"{u.name}: the gate answered /v1/models with {status}")
        if not rt.exposure.is_open(u.name):
            status, _, _ = gw.handle({**base, "PATH_INFO": "/dashboard"})
            if status.startswith("503"):
                r.ok(f"{u.name}: closed gate forwards nothing")
            else:
                r.fail(f"{u.name}: closed gate answered /dashboard with {status}")
        else:
            r.warn(f"{u.name}: dashboard gate is open ({rt.exposure.get(u.name).get('state')})")

    echo("4. isolation")
    ups = list(s.upstreams.values())
    for a in ups:
        for b in ups:
            if a is b or a.base_url == b.base_url or a.api_key == b.api_key or b.name not in models:
                continue
            try:
                resp = httpx.get(b.base_url + "/v1/models", headers={"Authorization": f"Bearer {a.api_key}"},
                                 timeout=10, trust_env=False)
                if resp.status_code == 401:
                    r.ok(f"{a.name}'s key is refused by {b.name}")
                else:
                    r.fail(f"{a.name}'s key got {resp.status_code} from {b.name} (expected 401)")
            except httpx.HTTPError as e:
                r.fail(f"{a.name} -> {b.name}: {e}")

    echo("5. prices")
    try:
        rt.pricing.configure(s)
        table = rt.pricing.sync()
        r.ok(f"source {table.source}: {len(table.prices) + sum(len(m) for m in table.by_provider.values())} prices")
    except Exception as e:
        r.fail(f"price source {s.pricing['source']}: {e}")
        table = rt.pricing.table if rt.pricing.table.prices or rt.pricing.table.by_provider else PriceTable()
    for pool in s.pools.values():
        names = models.get(pool.upstream, [])
        if pool.match:
            names = [m for m in names if pool.matches(m)]
        missing = [m for m in names if table.lookup(m, pool.kind, s.pricing["strip_prefixes"],
                                                    s.pricing["overrides"])[0] is None]
        if missing:
            r.warn(f"pool {pool.name}: no price for {', '.join(sorted(missing)[:10])}"
                   + (" ..." if len(missing) > 10 else ""))
        elif names:
            r.ok(f"pool {pool.name}: all {len(names)} models priced")

    echo("6. database")
    try:
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
        with app.app_context():
            cfg = app.extensions["migrate"].migrate.get_config()
            head = ScriptDirectory.from_config(cfg).get_current_head()
            with db.engine.connect() as conn:
                rev = MigrationContext.configure(conn).get_current_revision()
            db.session.execute(db.text("CREATE TEMP TABLE IF NOT EXISTS lingqu_check (x int)"))
            db.session.commit()
        if rev == head:
            r.ok(f"writable, at migration {rev}")
        else:
            r.fail(f"database at migration {rev}, head is {head}: run `flask db upgrade`")
    except Exception as e:
        r.fail(f"database: {e}")

    if live:
        echo("7. live")
        for pool in s.pools.values():
            live_request(r, s, pool, models.get(pool.upstream, []))
    return r


def live_request(r, s, pool, models):
    """One minimal request per pool straight through the local proxy port."""
    keys = pool.keys or [k for k, b in s.keys.items() if pool.name in b.pools]
    if not keys:
        r.warn(f"pool {pool.name}: no key, skipped")
        return
    names = [m for m in models if not pool.match or pool.matches(m)]
    cheap = sorted(names, key=lambda m: (0 if ("haiku" in m or "mini" in m) else 1, m))
    if not cheap:
        r.warn(f"pool {pool.name}: no model to try")
        return
    model = cheap[0]
    base = f"http://127.0.0.1:{s.proxy_port}"
    if pool.kind == "claude":
        url, body = base + "/v1/messages", {"model": model, "max_tokens": 8,
                                            "messages": [{"role": "user", "content": "ping"}]}
        headers = {"x-api-key": keys[0], "anthropic-version": "2023-06-01"}
    else:
        url, body = base + "/v1/responses", {"model": model, "input": "ping", "max_output_tokens": 16}
        headers = {"Authorization": f"Bearer {keys[0]}"}
    started = time.time()
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=120, trust_env=False)
    except httpx.HTTPError as e:
        r.fail(f"pool {pool.name}: {e}")
        return
    if resp.status_code != 200:
        r.fail(f"pool {pool.name}: {model} returned {resp.status_code}: {resp.text[:200]}")
        return
    from .models import Ledger
    deadline = time.time() + 5
    row = None
    while time.time() < deadline and row is None:
        row = Ledger.query.filter(Ledger.pool == pool.name, Ledger.ts >= started - 1).first()
        if row is None:
            time.sleep(0.5)
            db.session.rollback()
    if row is None:
        r.fail(f"pool {pool.name}: no ledger row after the request")
    elif not row.eusd:
        r.fail(f"pool {pool.name}: ledger row has zero cost")
    else:
        r.ok(f"pool {pool.name}: {model} ok, ${row.eusd:.5f} in the ledger")
