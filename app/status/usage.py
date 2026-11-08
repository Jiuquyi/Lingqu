"""Billing dashboard on the status port, always protected by console/admin auth."""
from flask import abort, jsonify, redirect, render_template, request, url_for

from . import status
from .console import console_authenticated
from .views import bearer, require_admin, rt
from ..usage.aggregate import PERIODS


@status.before_request
def _usage_auth():
    if not (request.endpoint or "").startswith("status.usage_"):
        return
    if not rt().settings.usage:
        abort(404)
    if request.endpoint == "status.usage_page" and not bearer() and not console_authenticated():
        return redirect(url_for("status.console_login", next=request.script_root + request.full_path.rstrip("?")))
    require_admin()


@status.route("/usage/index.html")
@status.route("/usage")
@status.route("/usage/")
def usage_page():
    if request.path == "/usage":
        return redirect(url_for("status.usage_page") + ("?" + request.query_string.decode() if request.query_string else ""), 302)
    config = rt().settings.usage
    return render_template("usage.html", usage=config,
                           usage_frontend={**{key: config[key] for key in ("cost_label", "cost_hint")},
                                           "stats_url": url_for("status.usage_stats"),
                                           "quotas_url": url_for("status.usage_quotas"),
                                           "overview_url": url_for("status.overview")})


@status.route("/usage/api/stats")
def usage_stats():
    period, provider, group = (request.args.get("period", "today"), request.args.get("provider", "all"),
                               request.args.get("group", "all"))
    if period not in PERIODS or provider not in ("all", "claude", "codex") or len(group) > 128:
        return jsonify(error="invalid usage filter"), 400
    return jsonify(rt().usage.stats(period, provider, group))


@status.route("/usage/api/quotas")
def usage_quotas():
    return jsonify(rt().usage.quotas())


@status.route("/usage/healthz")
def usage_healthz():
    health = rt().usage.health()
    return jsonify(health), (200 if health["ok"] else 503)
