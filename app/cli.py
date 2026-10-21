"""`flask` commands (DESIGN §10.3). Commands that change live state (dashboard, counters) talk to
the running instance's admin API: that state lives in the server process's memory."""
import csv
import json
import os
import subprocess
import sys
import time

import click
import httpx
from flask.cli import AppGroup

from . import db


def _admin(app, method, path, body=None):
    rt = app.extensions["lingqu"]
    s = rt.settings
    base = os.environ.get("LINGQU_ADMIN_URL") or f"http://127.0.0.1:{s.status_port}"
    try:
        r = httpx.request(method, base + path, json=body, timeout=30, trust_env=False,
                          headers={"Authorization": f"Bearer {s.admin_token}"})
    except httpx.HTTPError as e:
        raise click.ClickException(f"cannot reach the running instance at {base}: {e}")
    try:
        data = r.json()
    except ValueError:
        data = r.text
    if r.status_code >= 400:
        raise click.ClickException(f"{method} {path}: HTTP {r.status_code} {data}")
    return data


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def register(app, cov=None):
    @app.cli.command()
    @click.option("--coverage/--no-coverage", default=False, help="Run tests under code coverage.")
    @click.argument("test_names", nargs=-1)
    def test(coverage, test_names):
        """Run the unit tests."""
        if coverage and not os.environ.get("FLASK_COVERAGE"):
            os.environ["FLASK_COVERAGE"] = "1"
            sys.exit(subprocess.call(sys.argv))
        import unittest
        if test_names:
            tests = unittest.TestLoader().loadTestsFromNames(test_names)
        else:
            tests = unittest.TestLoader().discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(tests)
        if cov:
            cov.stop()
            cov.save()
            print("Coverage Summary:")
            cov.report()
            covdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp/coverage")
            cov.html_report(directory=covdir)
            print(f"HTML version: file://{covdir}/index.html")
            cov.erase()
        sys.exit(0 if result.wasSuccessful() else 1)

    @app.cli.command()
    @click.option("--live", is_flag=True, help="Also send one minimal request per pool through the proxy.")
    @click.option("--config", "path", default=None, help="Check this file instead of the loaded one.")
    def check(live, path):
        """Check the config and the whole chain; exits non-zero on any failure."""
        from .check import run
        r = run(app, click.echo, live=live, path=path)
        click.echo(f"{r.failed} failed, {r.warned} warnings")
        sys.exit(1 if r.failed else 0)

    @app.cli.command()
    def deploy():
        """Run deployment tasks: migrate the database to the latest revision."""
        from flask_migrate import upgrade
        upgrade()

    # ------------------------------------------------------------ dashboard

    dashboard = AppGroup("dashboard", help="Open, close or list the 9router dashboard gates.")

    @dashboard.command("list")
    def dashboard_list():
        for row in _admin(app, "GET", "/admin/dashboards"):
            state = row["state"] if row["open"] else "closed"
            until = _when(row["until"]) if state == "temporary" else "-"
            addr = f"{row.get('host') or ''}:{row.get('port') or ''}"
            click.echo(f"{row['upstream']:<24} {state:<11} until {until:<16} {addr:<24} by {row.get('opened_by') or '-'}")

    @dashboard.command("open")
    @click.argument("upstream")
    @click.option("--for", "duration", default=None, help="How long, e.g. 30m or 2h (default: the gate's default_ttl).")
    @click.option("--persistent", is_flag=True, help="Keep it open until closed.")
    def dashboard_open(upstream, duration, persistent):
        body = {"persistent": True} if persistent else ({"for": duration} if duration else {})
        st = _admin(app, "POST", f"/admin/dashboards/{upstream}/open", body)
        click.echo(f"{upstream}: {st['state']}" + (f" until {_when(st['until'])}" if st.get("until") else ""))

    @dashboard.command("close")
    @click.argument("upstream")
    def dashboard_close(upstream):
        st = _admin(app, "POST", f"/admin/dashboards/{upstream}/close")
        click.echo(f"{upstream}: {st['state']}")

    app.cli.add_command(dashboard)

    router = AppGroup("router", help="Inspect and synchronize the configured 9router managers.")

    @router.command("list")
    def router_list():
        for row in _admin(app, "GET", "/admin/routers"):
            click.echo(f"{row['upstream']}: " + ("stale" if row["stale"] else "synced"))

    @router.command("sync")
    @click.argument("upstream")
    def router_sync(upstream):
        from urllib.parse import quote
        result = _admin(app, "POST", "/admin/routers/" + quote(upstream, safe="") + "/sync")
        click.echo(json.dumps(result, ensure_ascii=False))

    app.cli.add_command(router)

    # ------------------------------------------------------------ pricing

    pricing = AppGroup("pricing", help="Show or sync the price table.")

    @pricing.command("show")
    @click.argument("model", required=False)
    def pricing_show(model):
        rt = app.extensions["lingqu"]
        rt.pricing.load_snapshot()
        t = rt.pricing.table
        click.echo(f"source {t.source}, synced {_when(t.synced_at)}")
        s = rt.settings.pricing
        if model:
            for kind in ("claude", "codex"):
                price, name = t.lookup(model, kind, s["strip_prefixes"], s["overrides"])
                click.echo(f"{model} ({kind}): {name} {json.dumps(price) if price else 'not found'}")
            return
        rows = list(t.prices.items()) + [(f"{pv}/{m}", p) for pv, ms in t.by_provider.items() for m, p in ms.items()]
        for name, p in sorted(rows):
            click.echo(f"{name:<48} in {p['input']:>7.3f}  out {p['output']:>7.3f}  "
                       f"cache r {p['cache_read']:>6.3f}  w {p['cache_write_5m']:>6.3f}"
                       + (f"  per request {p['per_request']}" if p.get("per_request") is not None else ""))

    @pricing.command("sync")
    def pricing_sync():
        t = app.extensions["lingqu"].pricing.sync()
        click.echo(f"synced {len(t.prices) + sum(len(m) for m in t.by_provider.values())} prices from {t.source}")

    app.cli.add_command(pricing)

    # ------------------------------------------------------------ ledger and counters

    ledger = AppGroup("ledger", help="Ledger tools.")

    @ledger.command("export")
    @click.option("--from", "start", required=True, help="YYYY-MM-DD")
    @click.option("--to", "end", required=True, help="YYYY-MM-DD (exclusive)")
    @click.option("--pool", default=None)
    def ledger_export(start, end, pool):
        from .models import Ledger
        from .replay import parse_day
        q = Ledger.query.filter(Ledger.ts >= parse_day(start), Ledger.ts < parse_day(end))
        if pool:
            q = q.filter(Ledger.pool == pool)
        cols = [c.name for c in Ledger.__table__.columns]
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        for r in q.order_by(Ledger.ts).yield_per(1000):
            w.writerow([getattr(r, c) for c in cols])

    app.cli.add_command(ledger)

    counters = AppGroup("counters", help="Rolling window counters.")

    @counters.command("reset")
    @click.argument("pool")
    def counters_reset(pool):
        st = _admin(app, "POST", f"/admin/pools/{pool}/counters/reset")
        click.echo(f"{pool}: spent_1h {st['spent_1h']}")

    app.cli.add_command(counters)

    # ------------------------------------------------------------ replay

    @app.cli.command()
    @click.option("--source", type=click.Choice(["ledger", "newapi", "jsonl"]), default="ledger")
    @click.option("--pool", required=True, help="Pool to replay (its kind and prices are used).")
    @click.option("--from", "start", default=None, help="YYYY-MM-DD (required unless --source jsonl)")
    @click.option("--to", "end", default=None, help="YYYY-MM-DD, exclusive (required unless --source jsonl)")
    @click.option("--policy", "policy_path", default=None, help="YAML file with one policy or a policies list.")
    @click.option("--compare", type=click.Choice(["current", "none"]), default="current",
                  help="Also run the pool's configured rule (current) or nothing extra.")
    @click.option("--limit", type=float, default=None, help="Default hourly limit for policies that set none.")
    @click.option("--group", default=None, help="newapi: only this New API group.")
    @click.option("--jsonl", "jsonl_path", default=None, help="jsonl: the request file.")
    def replay(source, pool, start, end, policy_path, compare, limit, group, jsonl_path):
        """Replay past requests under other policies (DESIGN §7)."""
        from . import replay as rp
        rt = app.extensions["lingqu"]
        s = rt.settings
        if pool not in s.pools:
            raise click.ClickException(f"unknown pool {pool}")
        if source != "jsonl" and not (start and end):
            raise click.ClickException("--from and --to are required with --source ledger or newapi")
        p = s.pools[pool]
        rt.pricing.load_snapshot()

        def price_of(model):
            return rt.pricing.price(model, p.kind)[0]

        t0, t1 = (rp.parse_day(start) if start else 0), (rp.parse_day(end) if end else float("inf"))
        if source == "ledger":
            reqs = rp.from_ledger(pool, t0, t1, price_of)
        elif source == "jsonl":
            if not jsonl_path:
                raise click.ClickException("--jsonl is required with --source jsonl")
            reqs = [r for r in rp.from_jsonl(jsonl_path, price_of) if t0 <= r.t < t1]
        else:
            na = s.pricing.get("newapi") or {}
            token = os.environ.get("NEWAPI_ACCESS_TOKEN") or na.get("token")
            uid = os.environ.get("NEWAPI_USER_ID", "1")
            if not na.get("base_url") or not token:
                raise click.ClickException("newapi source needs pricing.newapi.base_url and a token "
                                           "(NEWAPI_ACCESS_TOKEN, an admin user's access token)")
            reqs = rp.from_newapi(na["base_url"], token, uid, t0, t1, group,
                                  lambda m: (not p.match or p.matches(m)) and
                                  (m.startswith(("claude", "cc/")) if p.kind == "claude" else not m.startswith(("claude", "cc/"))),
                                  price_of)
        cur = rp.current_policy(s, pool)
        default_limit = limit or (cur.limit if cur else 20.0)
        policies = [rp.Policy(name="no limit", release="none", limit=default_limit)]
        if compare == "current" and cur:
            policies.append(cur)
        if policy_path:
            policies += rp.load_policies(policy_path, default_limit)
        sessions = len({r.session for r in reqs})
        click.echo(f"{pool} {start or '-'}..{end or '-'}: {len(reqs)} requests, {sessions} sessions, "
                   f"${sum(r.cost for r in reqs):.2f}")
        if not reqs:
            return
        # Does "nothing from this session for cache_ttl" really mean a cache rebuild on this pool?
        rp.classify_table(rp.classify_check(reqs, s.cache_ttl), click.echo)
        rp.table([rp.simulate(reqs, pol) for pol in policies], click.echo)

    @app.shell_context_processor
    def make_shell_context():
        from . import models
        return dict(db=db, rt=app.extensions["lingqu"], models=models)
