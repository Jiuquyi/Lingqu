"""Status and admin endpoints on the status port (DESIGN §6, §10.2)."""
import hmac
import time

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for

from ..models import GateEvent
from ..observe.stats import stats as compute_stats
from ..runtime import VERSION
from ..settings import parse_duration
from . import status

TOKEN_COOKIE = "lingqu_dash_token"


def rt():
    return current_app.extensions["lingqu"]


def bearer():
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def require_admin():
    token = rt().settings.admin_token
    if not token or not hmac.compare_digest(bearer(), token):
        abort(401)


PERIODS = [("today", "今天"), ("24h", "24 小时"), ("7d", "7 天"), ("30d", "30 天")]
EVENT_NAMES = {"throttle_start": "开始排队", "throttle_end": "解除", "step_change": "换档", "rejected": "拒绝（503）",
               "override": "管理员覆盖", "gate_error": "闸门错误",
               "account_auth": "账户授权失效", "account_auth_recovered": "账户授权恢复",
               "router_unreachable": "连不上 9router", "router_unreachable_recovered": "9router 恢复"}

@status.app_template_filter("clock")
def _clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "—"

@status.app_template_filter("when")
def _when(t):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if t else "—"

@status.app_template_filter("ago")
def _ago(t):
    s = max(0, int(time.time() - (t or 0)))
    return f"{s} 秒前" if s < 60 else f"{s // 60} 分钟前" if s < 3600 else f"{s // 3600} 小时前"

@status.app_template_filter("tok")
def _tok(n):
    if n is None:
        return "—"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)

@status.app_template_filter("percent")
def _percent(x):
    return "—" if x is None else f"{x * 100:.1f}%"

@status.app_template_filter("describe")
def _describe(d):
    if not d:
        return ""
    if "action" in d:
        out = {"limit": f"临时限额 ${d.get('value')}", "lift": "临时取消限额", "clear": "撤销覆盖"}.get(d["action"], d["action"])
        return out + (f"，到 {_when(d['until'])}" if d.get("until") else "")
    return d.get("message") or d.get("error") or ", ".join(f"{k}={v}" for k, v in d.items())

@status.route("/")
def overview():
    """Read-only overview on the status port: pools, metering, throttle events. No token, no
    actions; the same data as /status, /stats and /events."""
    period = request.args.get("period", "today")
    if period not in dict(PERIODS):
        period = "today"
    status_body = status_json().get_json()
    st = compute_stats(period)
    since = time.time() - 7 * 86400
    events = GateEvent.query.filter(GateEvent.ts >= since).order_by(GateEvent.ts.desc()).limit(100).all()
    return render_template("overview.html", now=time.time(), status=status_body, health=rt().health(),
                           period=period, periods=PERIODS, rows=sorted(st["pools"].items()), events=events,
                           event_names=EVENT_NAMES)

@status.route("/healthz")
def healthz():
    h = rt().health()
    return jsonify(h), (200 if h["ok"] else 503)


@status.route("/status")
def status_json():
    r = rt()
    now = time.time()
    pools = []
    for name, p in r.settings.pools.items():
        st = r.gate.pool_status(name, now)
        pools.append({"name": name, "kind": p.kind, "upstream": p.upstream, **st,
                      "budget": None, "quota": None, "last_error": r.last_errors.get(name)})
    return jsonify({
        "now": int(now), "version": VERSION,
        "pricing": {"source": r.pricing.table.source, "synced_at": r.pricing.table.synced_at,
                    "stale": r.pricing.stale(), "last_error": r.pricing.last_error or None,
                    "unpriced_models": sorted(r.pricing.unpriced)},
        "health": {"gate_errors_1h": r.gate.errors_1h(), "ledger_lag_sec": r.ledger.lag(),
                   "ledger_dropped": r.ledger.dropped, "held": r.gate.held_total()},
        "pools": pools,
        "dashboards": {n: {k: v for k, v in st.items()} for n, st in r.exposure.snapshot().items()},
    })


@status.route("/stats")
def stats_json():
    period = request.args.get("period", "24h")
    if period not in ("today", "24h", "7d", "30d"):
        abort(400)
    return jsonify(compute_stats(period, request.args.get("pool") or None))


@status.route("/events")
def events_json():
    q = GateEvent.query
    if request.args.get("from"):
        q = q.filter(GateEvent.ts >= float(request.args["from"]))
    if request.args.get("pool"):
        q = q.filter(GateEvent.pool == request.args["pool"])
    rows = q.order_by(GateEvent.ts.desc()).limit(min(int(request.args.get("limit", 500)), 5000)).all()
    return jsonify([{"ts": e.ts, "pool": e.pool, "event": e.event, "ratio": e.ratio, "spent_1h": e.spent_1h,
                     "limit": e.limit_value, "step": e.step, "detail": e.detail} for e in rows])


@status.route("/admin/reload", methods=["POST"])
def reload():
    require_admin()
    ok, errors, warnings = rt().reload()
    return jsonify({"ok": ok, "errors": errors, "warnings": warnings}), (200 if ok else 400)


@status.route("/admin/pools/<path:pool>/override", methods=["POST"])
def override(pool):
    require_admin()
    r = rt()
    if pool not in r.settings.pools:
        abort(404)
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("lift", "limit", "clear"):
        return jsonify({"error": "action must be lift, limit or clear"}), 400
    value = body.get("value")
    if action == "limit" and not (isinstance(value, (int, float)) and value > 0):
        return jsonify({"error": "limit needs a positive value"}), 400
    until = body.get("until")
    if action != "clear" and not (isinstance(until, (int, float)) and until > time.time()):
        return jsonify({"error": "until must be a future unix time"}), 400
    r.gate.set_override(pool, action, value, until)
    return jsonify(r.gate.pool_status(pool))


@status.route("/admin/pools/<path:pool>/counters/reset", methods=["POST"])
def counters_reset(pool):
    require_admin()
    r = rt()
    if pool not in r.settings.pools:
        abort(404)
    r.gate.counters.reset(pool)
    r.gate.set_override(pool, "clear", None, None)
    return jsonify(r.gate.pool_status(pool))


@status.route("/admin/pricing/sync", methods=["POST"])
def pricing_sync():
    require_admin()
    try:
        table = rt().pricing.sync()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "source": table.source, "models": len(table.prices) +
                    sum(len(m) for m in table.by_provider.values())})


# ------------------------------------------------------------ dashboard gate (DESIGN §2.4)

def dash_token():
    return bearer() or request.cookies.get(TOKEN_COOKIE, "")


def dash_view(token):
    r = rt()
    names = r.exposure.manages(token)
    out = []
    for n in names:
        u = r.settings.upstreams[n]
        st = r.exposure.get(n)
        out.append({"upstream": n, **st, "open": r.exposure.is_open(n), "port": u.dashboard.port,
                    "host": u.dashboard.host, "allow_persistent": u.dashboard.allow_persistent,
                    "default_ttl": u.dashboard.default_ttl, "max_ttl": u.dashboard.max_ttl})
    return out


def dash_action(upstream, token):
    r = rt()
    managed = r.exposure.manages(token)
    if not managed:
        abort(401)
    if upstream not in managed:
        abort(403)
    return r, r.exposure.actor(token)


@status.route("/admin/dashboards")
def dashboards_json():
    token = dash_token()
    if not rt().exposure.manages(token):
        abort(401)
    return jsonify(dash_view(token))


@status.route("/admin/dashboards/<upstream>/open", methods=["POST"])
def dashboard_open(upstream):
    r, actor = dash_action(upstream, dash_token())
    body = request.get_json(silent=True) or {}
    try:
        seconds = parse_duration(body["for"]) if body.get("for") else None
        st = r.exposure.open(upstream, actor, seconds=seconds, persistent=bool(body.get("persistent")))
    except (ValueError, PermissionError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(st)


@status.route("/admin/dashboards/<upstream>/close", methods=["POST"])
def dashboard_close(upstream):
    r, actor = dash_action(upstream, dash_token())
    return jsonify(r.exposure.close(upstream, actor))


@status.route("/dashboards", methods=["GET", "POST"])
def dashboards_page():
    """The one HTML page: log in with a token, then open / keep open / close each 9router."""
    r = rt()
    token = request.cookies.get(TOKEN_COOKIE, "")
    error = None
    if request.method == "POST":
        form = request.form
        if form.get("action") == "login":
            token = form.get("token", "").strip()
            if not r.exposure.manages(token):
                error = "token 无效"
                token = ""
        elif form.get("action") == "logout":
            token = ""
        else:
            upstream = form.get("upstream", "")
            if upstream not in r.exposure.manages(token):
                abort(403)
            actor = r.exposure.actor(token)
            try:
                if form["action"] == "open":
                    r.exposure.open(upstream, actor, source="page")
                elif form["action"] == "persist":
                    r.exposure.open(upstream, actor, persistent=True, source="page")
                elif form["action"] == "close":
                    r.exposure.close(upstream, actor, source="page")
            except (ValueError, PermissionError) as e:
                error = str(e)
        if error is None:
            resp = redirect(url_for("status.dashboards_page"), code=303)
            if token:
                resp.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="Strict", max_age=7 * 86400)
            else:
                resp.delete_cookie(TOKEN_COOKIE)
            return resp
    rows = dash_view(token) if token else []
    host = request.host.split(":")[0]
    return render_template("dashboards.html", rows=rows, logged_in=bool(rows), error=error, host=host,
                           now=time.time())
