"""Server-rendered administrative console: signed login cookie + per-session CSRF token."""
import hashlib
import hmac
import secrets

from werkzeug.exceptions import HTTPException

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from . import status
from .views import rt, require_admin, status_json
from ..observe.stats import stats
from ..models import GateEvent
from ..settings import parse_duration


def csrf_token():
    if 'console_csrf' not in session:
        session['console_csrf'] = secrets.token_urlsafe(32)
    return session['console_csrf']


def console_authenticated():
    token = rt().settings.admin_token
    expected = hashlib.sha256(token.encode()).hexdigest() if token else ''
    return bool(expected and hmac.compare_digest(session.get('console_auth', ''), expected))


def check_csrf():
    supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
    if not supplied or not hmac.compare_digest(supplied, session.get('console_csrf', '')):
        abort(400, 'invalid CSRF token')


@status.app_context_processor
def console_context():
    return {'csrf_token': csrf_token}


@status.after_app_request
def console_headers(response):
    if request.path.startswith('/console') or request.path.startswith('/admin/'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
    return response


@status.route('/console/login', methods=['GET', 'POST'])
def console_login():
    error = None
    if request.method == 'POST':
        check_csrf()
        token = request.form.get('token', '')
        if rt().settings.admin_token and hmac.compare_digest(token, rt().settings.admin_token):
            session.clear()
            session.permanent = True
            session['console_auth'] = hashlib.sha256(token.encode()).hexdigest()
            csrf_token()
            return redirect(url_for('status.console'), 303)
        error = '管理员 token 无效'
    return render_template('console_login.html', error=error), (401 if error else 200)


@status.route('/console/logout', methods=['POST'])
def console_logout():
    check_csrf()
    session.clear()
    return redirect(url_for('status.console_login'), 303)


@status.route('/console', methods=['GET', 'POST'])
def console():
    if not console_authenticated():
        return redirect(url_for('status.console_login'), 303)
    r = rt()
    section = request.args.get('section', 'overview')
    if section not in ('overview', 'pools', 'users', 'routers', 'pricing', 'stats', 'events', 'config'):
        abort(404)
    if request.method == 'POST':
        check_csrf()
        action = request.form.get('action')
        try:
            if action == 'reload':
                ok, errors, warnings = r.reload()
                # Loader errors can contain bad user-supplied values: do not render secrets from a config.
                flash('配置已重新加载' if ok else '配置未通过验证，继续使用原配置；请运行 flask check 查看原因',
                      'success' if ok else 'danger')
            elif action == 'pricing':
                r.pricing.sync()
                flash('价格已同步', 'success')
            elif action == 'quota':
                errors = r.quota.sync()
                flash('额度同步失败，请检查上游和凭据' if errors else '额度已同步', 'danger' if errors else 'success')
            elif action == 'manager':
                upstream = request.form.get('upstream') or None
                if upstream is not None and upstream not in r.manager.adapters:
                    abort(404)
                errors = r.manager.sync(upstream)
                flash('部分项目同步失败，请查看实例状态' if errors else '9router 已同步', 'warning' if errors else 'success')
            elif action == 'override':
                pool = request.form.get('pool')
                if pool not in r.settings.pools:
                    abort(404)
                mode = request.form.get('mode')
                if mode not in ('lift', 'limit', 'clear'):
                    raise ValueError('unknown override')
                value = float(request.form.get('value') or 0)
                import math
                if mode == 'limit' and (not math.isfinite(value) or value <= 0):
                    raise ValueError('invalid limit')
                duration = parse_duration(request.form.get('duration', '30m'))
                if not math.isfinite(duration) or not 0 < duration <= 86400:
                    raise ValueError('invalid duration')
                r.gate.set_override(pool, mode, value, r.clock() + duration if mode != 'clear' else None)
                flash('临时规则已更新', 'success')
            elif action in ('dashboard_open', 'dashboard_close', 'dashboard_persist'):
                name = request.form.get('upstream')
                if name not in r.exposure.state:
                    abort(404)
                if action == 'dashboard_close':
                    r.exposure.close(name, 'admin', source='console')
                else:
                    r.exposure.open(name, 'admin', persistent=action == 'dashboard_persist', source='console')
                flash('Dashboard 入口已更新', 'success')
            else:
                abort(400)
        except HTTPException:
            raise
        except (ValueError, PermissionError):
            flash('参数无效或操作不允许', 'danger')
        except Exception:
            current_app.logger.exception('console operation failed')
            flash('操作失败，请检查服务日志和健康状态', 'danger')
        return redirect(url_for('status.console', section=section), 303)
    if not r.health()["coordination"]["ok"]:
        return render_template('console_unavailable.html', section=section), 503
    state = status_json().get_json()
    user_rows = []
    for name, user in r.settings.users.items():
        user_state = r.gate.pool_status('@user/' + name)
        user_rows.append({'name': name, **user_state, 'metrics': user_state['rules'], 'rules': user.rules})
    from urllib.parse import urlsplit
    dashboard_urls = {}
    for name, upstream in r.settings.upstreams.items():
        d = upstream.dashboard
        if d:
            host = d.host or urlsplit(request.host_url).hostname or 'localhost'
            if ':' in host and not host.startswith('['):
                host = '[' + host + ']'
            dashboard_urls[name] = request.scheme + '://' + host + (':' + str(d.port) if d.port else '')
    prices = r.pricing.table
    price_rows = list(prices.prices.items()) + [(p + '/' + m, v) for p, models in prices.by_provider.items()
                                               for m, v in models.items()]
    period = request.args.get('period', '24h')
    if period not in ('today', '24h', '7d', '30d'):
        period = '24h'
    return render_template('console.html', section=section, state=state, health=r.health(),
        settings=r.settings, users=user_rows, routers=r.manager.snapshot(), dashboard_urls=dashboard_urls, prices=sorted(price_rows),
        stats=stats(period), period=period,
        events=GateEvent.query.order_by(GateEvent.ts.desc()).limit(100).all())


@status.route('/admin/routers')
def routers_json():
    require_admin()
    return jsonify(rt().manager.snapshot())


@status.route('/admin/routers/<upstream>/sync', methods=['POST'])
def router_sync(upstream):
    require_admin()
    if upstream not in rt().manager.adapters:
        abort(404)
    errors = rt().manager.sync(upstream)
    return jsonify({'ok': not errors, 'errors': errors}), (502 if errors else 200)


@status.route('/admin/users')
def users_json():
    require_admin()
    return jsonify([{'name': n, **rt().gate.pool_status('@user/' + n)} for n in rt().settings.users])


@status.route('/admin/routing')
def routing_json():
    require_admin()
    from ..proxy.views import resolve
    model = request.args.get('model', '')
    rows = []
    seen = set()
    for binding in rt().settings.keys.values():
        group = (binding.pool, tuple(binding.pools), binding.unmatched, binding.fallback_pool)
        if group in seen:
            continue
        seen.add(group)
        row = {'pools': binding.pools or [binding.pool]}
        try:
            row['result'], _ = resolve(rt(), binding, model, None)
        except ValueError as e:
            row['error'] = str(e)
        rows.append(row)
    return jsonify({'model': model, 'bindings': rows})
