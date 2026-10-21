import copy
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from app import create_app, db
from app.gate.hold import Gate
from app.gate.shared import RedisGate, SharedStateError
from app.models import Ledger
from app.ledger import restore_counters, save_counters
from app.proxy.views import resolve
from app.settings import Rule, load as read_settings
from tests.helpers import BASE, FakeClock, load

POOL = 'a/claude'
MODEL = 'claude-opus-5-5'
USER_CONFIG = '''
users:
  - name: alice
    rules:
      - {kind: concurrency, max: 1, on_exceed: reject}
      - {kind: rpm, max: 3, on_exceed: reject}
      - {kind: hourly_cap, limit: 2, on_exceed: reject}
  - {name: bob, rules: []}
keys:
  - {key: alice-one, pool: a/claude, user: alice}
  - {key: alice-two, pool: a/codex, user: alice}
  - {key: bob-one, pool: a/claude, user: bob}
'''


class UserLimitsTest(unittest.TestCase):
    def setUp(self):
        self.settings = load(BASE + USER_CONFIG)
        self.clock = FakeClock()
        self.gate = Gate(self.settings, clock=self.clock)

    def test_joint_admission_across_pools_and_idempotent_finish(self):
        first = self.gate.admit(POOL, MODEL, user='alice')
        other = self.gate.admit('a/codex', 'gpt-x', user='alice')
        self.assertTrue(other.rejected)
        self.assertEqual(self.gate.pool_status('a/codex')['in_flight'], 0)
        self.gate.finish(first)
        next_ticket = self.gate.admit('a/codex', 'gpt-x', user='alice')
        self.gate.finish(first)
        self.assertEqual(self.gate.pool_status('@user/alice')['in_flight'], 1)
        self.assertFalse(next_ticket.rejected)
        self.assertFalse(self.gate.admit(POOL, MODEL, user='bob').rejected)

    def test_money_cross_pool_and_rpm_rolling_window(self):
        self.gate.charge('a/codex', 'gpt-x', 2, user='alice')
        self.assertTrue(self.gate.admit(POOL, MODEL, user='alice').rejected)
        self.assertEqual(self.gate.pool_status(POOL)['spent_1h'], 0)
        self.clock.advance(3600)
        for _ in range(3):
            t = self.gate.admit(POOL, MODEL, user='alice')
            self.assertFalse(t.rejected)
            self.gate.finish(t)
        self.assertTrue(self.gate.admit('a/codex', 'gpt-x', user='alice').rejected)
        self.clock.advance(60)
        self.assertFalse(self.gate.admit(POOL, MODEL, user='alice').rejected)

    def test_model_scope_added_after_charges(self):
        self.gate.charge(POOL, MODEL, 3)
        self.settings.pools[POOL].rules.append(Rule('hourly_cap', limit=1, scope_model='claude-opus-*', on_exceed='reject'))
        self.gate.reload(self.settings)
        self.assertTrue(self.gate.admit(POOL, MODEL).rejected)
        self.assertFalse(self.gate.admit(POOL, 'claude-sonnet-x').rejected)

    def test_model_scoped_user_limit(self):
        self.settings.users['alice'].rules = [Rule('rpm', max=1, scope_model='claude-*', on_exceed='reject')]
        first = self.gate.admit(POOL, MODEL, user='alice')
        self.gate.finish(first)
        self.assertTrue(self.gate.admit(POOL, MODEL, user='alice').rejected)
        self.assertFalse(self.gate.admit('a/codex', 'gpt-x', user='alice').rejected)

    def test_count_scopes_added_after_requests_started(self):
        first = self.gate.admit(POOL, MODEL)
        self.settings.pools[POOL].rules += [Rule('concurrency', max=1,
            scope_model='claude-opus-*', on_exceed='reject')]
        self.gate.reload(self.settings)
        self.assertTrue(self.gate.admit(POOL, MODEL).rejected)
        self.assertFalse(self.gate.admit(POOL, 'claude-sonnet-x').rejected)
        self.gate.finish(first)
        self.settings.pools[POOL].rules[-1] = Rule('rpm', max=1, scope_model='claude-opus-*', on_exceed='reject')
        self.gate.reload(self.settings)
        self.assertTrue(self.gate.admit(POOL, MODEL).rejected)
        self.clock.advance(60)
        self.assertFalse(self.gate.admit(POOL, MODEL).rejected)

    def test_shadow_user_rule_marks_parent_ticket(self):
        self.settings.users['alice'].rules = [Rule('rpm', max=1, shadow=True)]
        self.gate.finish(self.gate.admit(POOL, MODEL, user='alice'))
        ticket = self.gate.admit('a/codex', 'gpt-x', user='alice')
        self.assertEqual(ticket.result, 'shadow_would_hold')
        self.assertEqual(ticket.shadow, 'user:rpm')

    def test_restart_recovers_trusted_user_only(self):
        app = create_app('testing', self.settings)
        with app.app_context():
            db.create_all()
            now = self.clock()
            db.session.add(Ledger(ts=now, pool=POOL, model_req=MODEL, user_ref='alice',
                                  user_tag='bob', eusd=3, eusd_input=1, counted_at=now, accounted_at=now))
            db.session.commit()
            restored = Gate(self.settings, clock=self.clock)
            restore_counters(app, restored, self.clock)
            self.assertEqual(restored.pool_status('@user/alice')['spent_1h'], 3)
            self.assertEqual(restored.pool_status('@user/bob')['spent_1h'], 0)
            save_counters(app, restored.counters, self.clock)
            restored2 = Gate(self.settings, clock=self.clock)
            restore_counters(app, restored2, self.clock)
            self.assertEqual(restored2.pool_status('@user/alice')['spent_1h'], 3)
            self.assertTrue(restored2.admit(POOL, MODEL, user='alice').rejected)
            db.session.remove()
            db.drop_all()


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.s = load(BASE)
        self.s.pools['a/claude'].match = ['claude-*']
        self.s.pools['a/codex'].match = ['*opus*', 'gpt-*']
        from app.settings import KeyBinding
        self.binding = KeyBinding('r1', pools=['a/claude', 'a/codex'])
        self.rt = Mock(settings=self.s)

    def test_priority_ambiguity_and_unmatched_policies(self):
        with self.assertRaises(ValueError):
            resolve(self.rt, self.binding, MODEL, None)
        self.s.pools[POOL].priority = 1
        self.assertEqual(resolve(self.rt, self.binding, MODEL, None)[0], POOL)
        with self.assertRaises(ValueError):
            resolve(self.rt, self.binding, 'unrecognized', None)
        self.binding.unmatched = 'unknown'
        self.assertEqual(resolve(self.rt, self.binding, 'unrecognized', None)[0], 'unknown')
        self.binding.unmatched, self.binding.fallback_pool = 'fallback', POOL
        self.assertEqual(resolve(self.rt, self.binding, '', None)[0], POOL)

    def test_validation(self):
        for extra in ('redis: {url: http://localhost}', 'redis: {url: redis://localhost, lease: 1s}',
                      'users: [{name: a}, {name: a}]', 'keys: [{key: z, pool: a/claude, user: missing}]',
                      'users: [{name: a, rules: [{kind: concurrency, max: 1.5}]}]',
                      'users: [{name: a, rules: [{kind: hourly_budget, cycle_limit: 1}]}]'):
            with self.subTest(extra=extra):
                _, errors, _ = read_settings(text=BASE + '\n' + extra)
                self.assertTrue(errors)


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing', load(BASE + USER_CONFIG))
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions['lingqu']
        self.rt.pricing.sync()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def token(self):
        body = self.client.get('/console/login').get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', body)[1]

    def login(self):
        token = self.token()
        response = self.client.post('/console/login', data={'token': 'adm', 'csrf_token': token})
        self.assertEqual(response.status_code, 303)
        with self.client.session_transaction() as sess:
            return sess['console_csrf']

    def test_authentication_csrf_and_token_rotation(self):
        self.assertEqual(self.client.get('/console').status_code, 303)
        self.assertEqual(self.client.get('/admin/routers').status_code, 401)
        self.assertEqual(self.client.post('/console/login', data={'token': 'adm'}).status_code, 400)
        csrf = self.login()
        self.assertEqual(self.client.post('/admin/pricing/sync').status_code, 400)
        self.assertEqual(self.client.post('/admin/pricing/sync', headers={'X-CSRF-Token': csrf}).status_code, 200)
        self.rt.settings.admin_token = 'changed'
        self.assertEqual(self.client.get('/admin/users').status_code, 401)

    def test_console_sections_and_local_assets(self):
        self.login()
        for section in ('overview', 'pools', 'users', 'routers', 'pricing', 'stats', 'events', 'config'):
            with self.subTest(section=section):
                response = self.client.get('/console?section=' + section)
                self.assertEqual(response.status_code, 200)
                self.assertIn('bootstrap.min.css', response.get_data(as_text=True))
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertNotIn('alice-one', response.get_data(as_text=True))
        response = self.client.get('/static/vendor/bootstrap.min.css')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_override_requires_csrf(self):
        csrf = self.login()
        form = {'action': 'override', 'pool': POOL, 'mode': 'limit', 'value': '5', 'duration': '30m'}
        self.assertEqual(self.client.post('/console?section=pools', data=form).status_code, 400)
        form['csrf_token'] = csrf
        self.assertEqual(self.client.post('/console?section=pools', data=form).status_code, 303)
        self.assertEqual(self.rt.gate.pool_status(POOL)['override']['value'], 5)


class ManagerTest(unittest.TestCase):
    setUp = ConsoleTest.setUp
    tearDown = ConsoleTest.tearDown

    def test_inventory_allowlist_and_partial_failure(self):
        self.rt.settings.upstreams['r1'].manager = {'password': 'secret', 'refresh': 180}
        self.rt.manager.configure()
        adapter = self.rt.manager.adapters['r1']
        adapter.health = Mock(return_value=({'ok': True, 'latency_ms': 1}, [{'id': MODEL}]))
        adapter.session.get = Mock(side_effect=lambda path: {
            '/api/providers': {'connections': [{'id': 'conn', 'provider': 'claude', 'authType': 'oauth',
                'accessToken': 'DO-NOT-EXPOSE', 'providerSpecificData': {'refreshToken': 'DO-NOT-EXPOSE'}, 'isActive': True}]},
            '/api/combos': {'combos': [{'name': 'combo', 'models': [MODEL], 'secret': 'DO-NOT-EXPOSE'}]},
            '/api/pricing': {'claude': {MODEL: {'input': 5, 'output': 25}}},
            '/api/usage/conn': {'weekly': {'used_pct': 20}},
            '/api/usage/history': {'totalCompletionTokens': 100, 'totalRequests': 1, 'byApiKey': {'DO-NOT-EXPOSE': 2}},
        }[path])
        self.assertEqual(self.rt.manager.sync(), {})
        snap = self.rt.manager.snapshot()[0]
        self.assertFalse(snap['stale'])
        self.assertNotIn('DO-NOT-EXPOSE', json.dumps(snap))
        adapter.session.get.side_effect = RuntimeError('credential=DO-NOT-EXPOSE')
        self.assertTrue(self.rt.manager.sync())
        snap = self.rt.manager.snapshot()[0]
        self.assertTrue(snap['stale'])
        self.assertTrue(snap['connections'])
        self.assertNotIn('DO-NOT-EXPOSE', json.dumps(snap))


    def test_reconciliation_observes_time_and_volume_before_alerting(self):
        manager = self.rt.manager
        now = self.rt.clock()
        row = {'totals': {'totalCompletionTokens': 10, 'totalRequests': 1}}
        manager._reconcile('r1', row, now)
        self.assertEqual(row['reconciliation']['state'], 'collecting')
        row['totals']['totalCompletionTokens'] = 2010
        with patch.object(self.rt.notifier, 'alert') as alert:
            manager._reconcile('r1', row, now + 601)
            self.assertEqual(row['reconciliation']['state'], 'diverged')
            alert.assert_called_once()
        row['totals']['totalCompletionTokens'] = 1
        manager._reconcile('r1', row, now + 700)
        self.assertEqual(row['reconciliation']['state'], 'collecting')


def contend(settings, result, release):
    g = RedisGate(settings)
    ticket = g.admit(POOL, MODEL)
    result.put(not ticket.rejected)
    release.wait(5)
    g.finish(ticket)


@unittest.skipUnless(shutil.which('redis-server'), 'redis-server is needed for shared-state integration tests')
class RedisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import redis
        cls.temp = tempfile.TemporaryDirectory(prefix='lingqu-redis-')
        cls.socket = cls.temp.name + '/redis.sock'
        cls.server = subprocess.Popen(['redis-server', '--port', '0', '--unixsocket', cls.socket,
            '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.client = redis.Redis(unix_socket_path=cls.socket)
        for _ in range(100):
            try:
                cls.client.ping()
                break
            except redis.ConnectionError:
                time.sleep(.02)
        else:
            raise RuntimeError('test Redis did not start')

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(5)
        cls.temp.cleanup()

    def setUp(self):
        self.s = load(BASE + USER_CONFIG + '\nredis: {url: "unix://' + self.socket + '", namespace: test-' + uuid.uuid4().hex + '}')
        self.clock = FakeClock()
        self.a = RedisGate(self.s, clock=self.clock)
        self.b = RedisGate(copy.deepcopy(self.s), clock=self.clock)

    def tearDown(self):
        for key in self.client.scan_iter('{' + self.s.redis['namespace'] + '}:*'):
            self.client.delete(key)
        self.a.redis.close()
        self.b.redis.close()

    def test_shared_counters_sessions_users_and_idempotent_release(self):
        t = self.a.admit(POOL, MODEL, session='s', user='alice')
        self.assertFalse(t.rejected)
        self.assertTrue(self.b.admit('a/codex', 'gpt-x', user='alice').rejected)
        self.a.charge(POOL, MODEL, 1.5, user='alice')
        self.assertEqual(self.b.pool_status('@user/alice')['spent_1h'], 1.5)
        self.assertEqual(self.b.pool_status(POOL)['sessions']['warm'], 1)
        self.a.finish(t)
        next_ticket = self.b.admit(POOL, MODEL, user='alice')
        self.a.finish(t)
        self.assertEqual(self.a.pool_status('@user/alice')['in_flight'], 1)
        self.b.finish(next_ticket)

    def test_lease_expiry_and_renewal(self):
        first = self.a.admit(POOL, MODEL, user='alice')
        self.clock.advance(70)
        self.a.renew()
        self.clock.advance(30)
        self.assertEqual(self.b.pool_status(POOL)['in_flight'], 1)
        self.clock.advance(61)
        self.assertEqual(self.b.pool_status(POOL)['in_flight'], 0)
        replacement = self.b.admit(POOL, MODEL, user='alice')
        self.a.finish(first)
        self.assertEqual(self.b.pool_status(POOL)['in_flight'], 1)
        self.b.finish(replacement)

    def test_queue_across_instances(self):
        self.s.pools[POOL].rules = [Rule('concurrency', max=1)]
        self.a.reload(self.s)
        self.b.reload(copy.deepcopy(self.s))
        running = self.a.admit(POOL, MODEL)
        results = []
        th = threading.Thread(target=lambda: results.append(self.b.admit(POOL, MODEL)))
        th.start()
        deadline = time.monotonic() + 3
        while not self.a.pool_status(POOL)['held'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.a.pool_status(POOL)['held'], 1)
        self.a.finish(running)
        th.join(3)
        self.assertFalse(th.is_alive())
        self.assertFalse(results[0].rejected)
        self.assertEqual(self.a.pool_status(POOL)['held'], 0)
        self.b.finish(results[0])

    def test_configuration_mismatch_and_outage_fail_closed(self):
        self.a.charge(POOL, MODEL, 1)
        changed = copy.deepcopy(self.s)
        changed.pools[POOL].rules[0].limit = 99
        self.b.reload(changed)
        self.assertFalse(self.a.health()['ok'])
        self.assertTrue(self.a.admit(POOL, MODEL).rejected)
        self.a.reload(copy.deepcopy(changed))
        self.assertTrue(self.a.health()['ok'])
        with patch.object(self.a.redis, 'get', side_effect=ConnectionError('offline')):
            self.assertTrue(self.a.admit(POOL, MODEL).rejected)

    def test_fenced_writer_cannot_commit(self):
        with self.assertRaises(SharedStateError):
            with self.a.lock:
                self.client.delete(self.a.prefix + ':lock')
                self.a.counters.add(POOL, ['*'], self.clock(), 100)
        self.assertEqual(self.b.pool_status(POOL)['spent_1h'], 0)

    def test_shared_budget_quota_override_and_reload_user_slots(self):
        self.s.pools[POOL].rules.append(Rule('hourly_budget', cycle_limit=1680, h_min=2, h_max=100))
        self.a.reload(self.s)
        self.b.reload(copy.deepcopy(self.s))
        first = self.a.admit(POOL, MODEL, user='alice')
        self.a.reload(self.s)
        self.assertTrue(self.b.admit('a/codex', 'gpt-x', user='alice').rejected)
        self.a.finish(first)
        self.a.update_quota(POOL, 'connection', {'weekly': {'used_pct': 98, 'reset_at': self.clock() + 604800}})
        self.assertTrue(self.b.admit(POOL, MODEL).rejected)
        self.assertEqual(self.b.pool_status(POOL)['brake'], 'reject')
        self.a.set_override(POOL, 'limit', 3, self.clock() + 60)
        self.assertEqual(self.b.pool_status(POOL)['override']['value'], 3)
        self.assertTrue(self.b.pool_status(POOL)['budget'])

    def test_model_scope_added_with_another_process_in_flight(self):
        first = self.a.admit(POOL, MODEL)
        self.s.pools[POOL].rules.append(Rule('concurrency', max=1, scope_model='claude-*', on_exceed='reject'))
        self.a.reload(self.s)
        self.b.reload(copy.deepcopy(self.s))
        self.assertTrue(self.b.admit(POOL, MODEL).rejected)
        self.a.finish(first)
        self.assertFalse(self.b.admit(POOL, MODEL).rejected)

    def test_abandoned_queue_item_expires(self):
        from app.gate.hold import Ticket
        with self.a.lock:
            self.a.state(POOL).queue.append(Ticket(POOL, MODEL, self.clock(), lease_until=self.clock() + 10))
        self.assertEqual(self.b.pool_status(POOL)['held'], 1)
        self.clock.advance(11)
        self.assertEqual(self.b.pool_status(POOL)['held'], 0)

    def test_dashboard_login_failure_cooldown_shared_between_sessions(self):
        from app.pricing.ninerouter import Session
        first = Session('http://router', 'password', self.clock)
        second = Session('http://router', 'password', self.clock)
        first.shared = second.shared = self.s.redis
        response = Mock(status_code=401, cookies={})
        with patch('app.pricing.ninerouter.httpx.post', return_value=response) as login:
            for session in (first, second):
                with self.assertRaises(RuntimeError):
                    session.get('/api/providers')
            self.assertEqual(login.call_count, 1)
            self.clock.advance(1800)
            with self.assertRaises(RuntimeError):
                second.get('/api/providers')
            self.assertEqual(login.call_count, 2)

    def test_exposure_changes_visible_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = copy.deepcopy(self.s)
            settings.dsn = 'sqlite:///' + directory + '/shared.db'
            one = create_app('testing', settings)
            two = create_app('testing', copy.deepcopy(settings))
            with one.app_context():
                db.create_all()
            first, second = one.extensions['lingqu'].exposure, two.extensions['lingqu'].exposure
            first.open('r1', 'admin', seconds=10)
            self.assertTrue(second.is_open('r1'))
            second.close('r1', 'admin')
            self.assertFalse(first.is_open('r1'))
            self.assertEqual(first.close('r1', 'admin')['state'], 'closed')
            for app in (one, two):
                app.extensions['lingqu'].gate.redis.close()
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def test_atomic_admission_between_processes(self):
        import multiprocessing
        self.s.pools[POOL].rules = [Rule('concurrency', max=1, on_exceed='reject')]
        context = multiprocessing.get_context('spawn')
        result, release = context.Queue(), context.Event()
        children = [context.Process(target=contend, args=(self.s, result, release)) for _ in range(5)]
        for child in children:
            child.start()
        try:
            self.assertEqual(sum(result.get(timeout=8) for _ in children), 1)
        finally:
            release.set()
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
        self.assertTrue(all(c.exitcode == 0 for c in children))

class ProxyUserTest(unittest.TestCase):
    from tests.test_proxy import ProxyTestCase as _ProxyFixture
    setUp = _ProxyFixture.setUp
    tearDown = _ProxyFixture.tearDown
    post = _ProxyFixture.post
    rows = _ProxyFixture.rows

    def configure_users(self):
        self.rt.settings.users = load(BASE + USER_CONFIG).users
        self.rt.settings.keys.update(load(BASE + USER_CONFIG).keys)
        self.rt.gate.reload(self.rt.settings)

    def test_dashboard_proxy_keeps_local_auth_cookies_private(self):
        self.rt.exposure.open('r1', 'admin', seconds=30)
        self.router.reply = lambda *a: (200, [('Content-Type', 'text/plain'),
            ('Set-Cookie', 'lingqu_console_session=forged; Path=/')], [b'ok'], 0)
        from werkzeug.test import Client
        client = Client(self.disp, use_cookies=False)
        response = client.get('/dashboard', headers={'Cookie':
            'lingqu_console_session=private; lingqu_dash_token=adm; auth_token=router-cookie'},
            environ_overrides={'SERVER_PORT': '8101'})
        response.get_data()
        cookies = self.router.requests[-1]['headers'].get('cookie', '')
        self.assertNotIn('private', cookies)
        self.assertNotIn('adm', cookies)
        self.assertIn('auth_token=router-cookie', cookies)
        self.assertNotIn('forged', response.headers.get('Set-Cookie', ''))

    def test_body_cannot_change_user_and_cross_key_money_is_enforced(self):
        self.configure_users()
        self.rt.gate.charge('a/codex', 'gpt-x', 2, user='alice')
        response = self.post('/v1/messages', {'model': MODEL, 'user': 'bob'}, key='alice-one')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.router.requests, [])
        row = self.rows()[0]
        self.assertEqual(row.user_ref, 'alice')
        self.assertEqual(row.user_tag, 'bob')

    def test_identity_not_forwarded_and_usage_recorded(self):
        self.configure_users()
        raw = b'{"model":"claude-opus-5-5", "user":"bob", "stream":true}'
        response = self.post('/v1/messages', raw, key='alice-one')
        self.assertEqual(response.status_code, 200)
        response.get_data()
        response.close()
        self.assertEqual(self.router.requests[0]['body'], raw)
        row = self.rows()[0]
        self.assertEqual(row.user_ref, 'alice')
        self.assertGreater(self.rt.gate.pool_status('@user/alice')['spent_1h'], 0)
        self.assertEqual(self.rt.gate.pool_status('@user/bob')['spent_1h'], 0)
        self.assertEqual(self.rt.gate.pool_status('@user/alice')['in_flight'], 0)

    def test_alias_cost_is_priced_by_response_and_limited_by_request_scope(self):
        self.rt.settings.pools[POOL].rules = [Rule('hourly_cap', limit=.1,
            on_exceed='reject', scope_model='daily-*')]
        self.rt.gate.reload(self.rt.settings)
        response = self.post('/v1/messages', {'model': 'daily-combo', 'stream': True})
        response.get_data()
        response.close()
        row = self.rows()[0]
        self.assertEqual(row.model_req, 'daily-combo')
        self.assertEqual(row.model_actual, MODEL)
        self.assertIn(MODEL, row.price_ref)
        self.assertEqual(self.post('/v1/messages', {'model': 'daily-combo'}).status_code, 503)

    def test_shared_key_unmatched_never_reaches_upstream(self):
        from app.settings import KeyBinding
        self.rt.settings.pools[POOL].match = ['claude-*']
        self.rt.settings.keys['shared'] = KeyBinding('r1', pools=[POOL])
        response = self.post('/v1/messages', {'model': 'unmatched'}, key='shared')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.router.requests, [])
