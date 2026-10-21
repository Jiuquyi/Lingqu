"""9router management adapter. Inventories are allowlisted before leaving the adapter.

The live dashboard remains the place to change OAuth credentials. Inventory synchronization
never copies OAuth tokens or logs a response body from an authenticated dashboard endpoint.
"""
import copy
import threading
from urllib.parse import quote

import httpx

from .pricing.ninerouter import Session

CONNECTION_FIELDS = ('id', 'provider', 'name', 'authType', 'isActive', 'testStatus', 'expiresAt', 'lastTested')


class Adapter:
    def __init__(self, runtime, upstream):
        self.rt, self.upstream = runtime, upstream
        u = runtime.settings.upstreams[upstream]
        self.base_url, self.api_key = u.base_url, u.api_key
        password = u.manager.get('password') or u.quota.get('password')
        self.session = next((s for s in (runtime.quota.sessions.get(upstream), runtime.pricing.session)
                             if s and s.base_url == u.base_url and s.password == password), None)
        if self.session is None and password:
            self.session = Session(u.base_url, password, runtime.clock)

    def get(self, path):
        if self.session is None:
            raise ValueError('configure manager.password to inspect dashboard APIs')
        return self.session.get(path)

    def health(self):
        start = self.rt.clock()
        response = self.rt.client(self.upstream).get(self.base_url + '/v1/models',
            headers={'Authorization': 'Bearer ' + self.api_key}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise ValueError('invalid models response')
        return {'ok': True, 'latency_ms': round((self.rt.clock() - start) * 1000)}, [
            {'id': m['id'], 'owned_by': m.get('owned_by')} for m in body['data']
            if isinstance(m, dict) and isinstance(m.get('id'), str)]

    def list_connections(self):
        body = self.get('/api/providers')
        rows = body.get('connections') if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ValueError('invalid providers response')
        return [{k: c[k] for k in CONNECTION_FIELDS if k in c} for c in rows if isinstance(c, dict)]

    def list_combos(self):
        body = self.get('/api/combos')
        if not isinstance(body, dict) or not isinstance(body.get('combos'), list):
            raise ValueError('invalid combos response')
        return [{'id': c.get('id'), 'name': c.get('name'), 'models': c.get('models', [])}
                for c in body['combos'] if isinstance(c, dict)]

    def account_usage(self, connection):
        from .quota import normalize
        return normalize(self.get('/api/usage/' + quote(connection, safe='')))

    def usage_totals(self):
        from .quota import finite
        body = self.get('/api/usage/history')
        if not isinstance(body, dict):
            raise ValueError('invalid usage history response')
        # byApiKey contains full API keys in some 9router versions: never retain the raw body.
        totals = {field: finite(body[field]) for field in ('totalCompletionTokens', 'totalRequests')}
        if any(n < 0 for n in totals.values()):
            raise ValueError('negative usage totals')
        return totals

    def pricing(self):
        from .pricing.ninerouter import to_prices
        return to_prices(self.get('/api/pricing'))


class Manager:
    def __init__(self, runtime):
        self.rt = runtime
        self.lock = threading.RLock()
        self.rows = {}
        self.adapters = {}
        self.configure()

    def configure(self):
        with self.lock:
            previous = self.adapters
            self.adapters = {n: Adapter(self.rt, n) for n, u in self.rt.settings.upstreams.items() if u.manager}
            self.rows = {n: row for n, row in self.rows.items() if n in self.adapters and n in previous and
                         previous[n].base_url == self.adapters[n].base_url and
                         previous[n].api_key == self.adapters[n].api_key and
                         previous[n].session.password == self.adapters[n].session.password}

    def tick(self):
        now = self.rt.clock()
        for name in list(self.adapters):
            row = self.rows.get(name, {})
            if now - row.get('attempted_at', 0) >= self.rt.settings.upstreams[name].manager['refresh']:
                self.sync(name)

    def sync(self, upstream=None):
        from .quota import timestamp
        errors = {}
        with self.lock:
            for name, adapter in self.adapters.items():
                if upstream is not None and name != upstream:
                    continue
                now = self.rt.clock()
                old = self.rows.get(name, {})
                row = copy.deepcopy(old)
                row.update(upstream=name, attempted_at=now, stale=True, errors={})
                try:
                    row['health'], row['models'] = adapter.health()
                except Exception:
                    row['health'] = {'ok': False}
                    row['errors']['health'] = '9router health or model listing failed'
                for section, read in (('connections', adapter.list_connections), ('combos', adapter.list_combos),
                                       ('pricing', adapter.pricing), ('totals', adapter.usage_totals)):
                    try:
                        row[section] = read()
                    except Exception:
                        row['errors'][section] = f'9router {section} sync failed; check API compatibility and credentials'
                row['usage'] = {}
                for connection in row.get('connections', []):
                    cid = connection.get('id')
                    if not isinstance(cid, str) or not connection.get('isActive', True):
                        continue
                    if connection.get('authType', 'oauth') == 'oauth':
                        try:
                            row['usage'][cid] = adapter.account_usage(cid)
                        except Exception:
                            row['errors']['usage/' + cid] = 'account quota unavailable'
                    expiry = connection.get('expiresAt')
                    try:
                        expired = expiry is not None and timestamp(expiry) <= now
                    except (ValueError, TypeError):
                        expired = False
                    if expired or connection.get('testStatus') in ('error', 'failed', 'invalid'):
                        self.rt.notifier.alert('connection_auth', name + '/' + cid,
                            f'9router {name}: connection {cid} needs authorization or health review')
                if 'totals' not in row['errors'] and 'totals' in row:
                    self._reconcile(name, row, now)
                row['routing'] = self._routing(name, row.get('models', []))
                row['stale'] = bool(row['errors'])
                if not row['stale']:
                    row['synced_at'] = now
                self.rows[name] = row
                if row['errors']:
                    errors[name] = row['errors']
                self.rt.ledger.add_event({'ts': now, 'pool': None, 'event': 'manager_sync',
                    'detail': {'upstream': name, 'ok': not row['stale'], 'errors': list(row['errors'])}})
            self._duplicates()
        return errors

    def _reconcile(self, upstream, row, now):
        from sqlalchemy import func
        from . import db
        from .models import Ledger
        pools = [p.name for p in self.rt.settings.pools.values() if p.upstream == upstream]
        with self.rt.app.app_context():
            local = db.session.query(func.coalesce(func.sum(Ledger.output_tokens), 0)).filter(
                Ledger.pool.in_(pools), Ledger.gate_result != 'rejected', Ledger.usage_source == 'upstream').scalar()
        current = {'ts': now, 'upstream_output': row['totals']['totalCompletionTokens'], 'lingqu_output': local}
        baseline = row.get('reconcile_baseline')
        if baseline is None or current['upstream_output'] < baseline['upstream_output'] or local < baseline['lingqu_output']:
            row['reconcile_baseline'] = current
            row['reconciliation'] = {'state': 'collecting', 'minimum_window_seconds': 600}
            return
        elapsed = now - baseline['ts']
        if elapsed < 600:
            return
        remote_delta = current['upstream_output'] - baseline['upstream_output']
        local_delta = local - baseline['lingqu_output']
        deviation = abs(local_delta - remote_delta) / max(remote_delta, local_delta, 1)
        enough = max(remote_delta, local_delta) >= 1000
        row['reconciliation'] = {'state': 'diverged' if enough and deviation > .1 else 'ok' if enough else 'insufficient_data',
            'window_seconds': elapsed, 'upstream_output': remote_delta, 'lingqu_output': local_delta,
            'deviation': deviation, 'threshold': .1}
        row['reconcile_baseline'] = current
        if enough and deviation > .1:
            self.rt.notifier.alert('usage_divergence', upstream,
                f'9router {upstream}: output-token counts differ by {deviation:.1%}; check direct traffic and metering coverage')

    def _routing(self, upstream, models):
        from .proxy.views import resolve
        out = []
        # Do not expose keys or even key suffixes in the model audit.
        bindings = {(b.pool, tuple(b.pools), b.unmatched, b.fallback_pool): b
                    for b in self.rt.settings.keys.values() if b.upstream == upstream}
        for binding in bindings.values():
            for model in models:
                try:
                    pool, _ = resolve(self.rt, binding, model['id'], None)
                    out.append({'model': model['id'], 'pools': binding.pools or [binding.pool], 'result': pool})
                except ValueError as e:
                    out.append({'model': model['id'], 'pools': binding.pools, 'error': str(e)})
        return out

    def _duplicates(self):
        seen = {}
        for upstream, row in self.rows.items():
            row['duplicate_connections'] = []
            for c in row.get('connections', []):
                if c.get('authType', 'oauth') != 'oauth' or not c.get('id'):
                    continue
                key = (c.get('provider'), c['id'])
                if key in seen:
                    other = seen[key]
                    row['duplicate_connections'].append({'connection': c['id'], 'upstream': other})
                    self.rows[other]['duplicate_connections'].append({'connection': c['id'], 'upstream': upstream})
                    self.rt.notifier.alert('duplicate_oauth', c['id'],
                        f'same OAuth connection id appears on {other} and {upstream}; check account isolation')
                else:
                    seen[key] = upstream

    def snapshot(self):
        with self.lock:
            rows = []
            for name in sorted(self.adapters):
                row = copy.deepcopy(self.rows.get(name, {'upstream': name, 'stale': True, 'errors': {},
                                                         'health': {'ok': False}}))
                if self.rt.clock() - row.get('synced_at', 0) > 2 * self.rt.settings.upstreams[name].manager['refresh']:
                    row['stale'] = True
                rows.append(row)
            return rows
