import copy
import json
import re
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from werkzeug.test import Client
from werkzeug.wrappers import Response

from app import create_app, db
from app.ports import PortDispatcher
from app.settings import load as read_settings
from app.usage.service import NewAPI
from tests.helpers import BASE, FakeClock, load

CONFIG = '''
usage:
  enabled: true
  newapi: {base_url: 'http://new-api.test', token: 'TEST-ACCESS-TOKEN', user_id: 1}
  scope: managed
  timezone: Asia/Shanghai
'''
CHANNELS = [
    {'name': 'a', 'type': 14, 'other_info': '{"machgate_router":"http://r1:20128"}'},
    {'name': 'a-gpt', 'type': 1, 'other_info': '{"machgate_router":"http://r1:20128"}'},
    {'name': 'b', 'type': 14, 'other_info': '{"machgate_router":"http://r2:20128"}'},
    {'name': 'manual', 'type': 1, 'group': 'default', 'base_url': 'http://untrusted.test'},
]


class UsageSettingsTest(unittest.TestCase):
    def test_opt_in_validation_and_no_secrets_in_errors(self):
        self.assertEqual(load(BASE).usage, {})
        good = load(BASE + CONFIG).usage
        self.assertEqual((good['retention_days'], good['max_records'], good['scope']), (90, 100000, 'managed'))
        variants = [('enabled: true', 'enabled: yesplease'), ('scope: managed', 'scope: bogus'),
                    ('user_id: 1', 'user_id: false'), ('token: \'TEST-ACCESS-TOKEN\'', "token: ''"),
                    ('timezone: Asia/Shanghai', 'timezone: Not/AZone'),
                    ('http://new-api.test', 'http://secret:password@new-api.test')]
        for old, new in variants:
            with self.subTest(new=new):
                _, errors, _ = read_settings(text=BASE + CONFIG.replace(old, new))
                self.assertTrue(errors)
                self.assertNotIn('TEST-ACCESS-TOKEN', str(errors))
                self.assertNotIn('password@', str(errors))
        for key, value in [('max_records', 0), ('max_records', True), ('retention_days', 91)]:
            _, errors, _ = read_settings(text=BASE + CONFIG + f'  {key}: {json.dumps(value)}\n')
            self.assertTrue(errors)
        _, errors, _ = read_settings(text=BASE.replace('admin_token: adm, ', '') + CONFIG)
        self.assertIn('usage.enabled needs server.admin_token', errors)


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing', load(BASE + CONFIG))
        self.rt = self.app.extensions['lingqu']
        self.service = self.rt.usage
        self.clock = FakeClock(1800000000)
        self.rt.clock = self.service.clock = self.clock
        self.rt.gate.clock = self.clock
        self.client = self.app.test_client()
        self.logs = {2: [], 5: []}
        self.channels = copy.deepcopy(CHANNELS)
        self.calls = []
        self.failure = None
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def api(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.assertFalse(kwargs['follow_redirects'])
        self.assertFalse(kwargs['trust_env'])
        self.assertEqual(kwargs['headers']['New-Api-User'], '1')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer TEST-ACCESS-TOKEN')
        if self.failure:
            return httpx.Response(self.failure, json={'message':'TEST-ACCESS-TOKEN never expose this'})
        parsed = urlsplit(url)
        if parsed.path == '/api/channel/':
            data = {'items': self.channels, 'total':len(self.channels)}
        elif parsed.path == '/api/status':
            data = {'quota_per_unit': 500000}
        elif parsed.path == '/api/log/':
            query = parse_qs(parsed.query)
            since = int(query['start_timestamp'][0])
            items = [item for item in self.logs[int(query['type'][0])] if item['created_at'] >= since]
            data = {'items': items, 'total':len(items)}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json={'success':True,'data':data})

    def row(self, identifier=1, channel='a', **kwargs):
        return {'id':identifier, 'created_at':int(self.clock())-10, 'channel_name':channel,
                'username':'alice', 'token_name':'work', 'model_name':'claude-opus-5-5' if channel=='a' else 'gpt-6.1-sol',
                'prompt_tokens':100, 'completion_tokens':20, 'quota':1250000, 'use_time':2,
                'other':'{"cache_tokens":60,"cache_creation_tokens":10}', **kwargs}

    def sync(self):
        with patch('app.usage.service.httpx.get', side_effect=self.api):
            self.service.tick()

    def admin(self, path):
        return self.client.get(path, headers={'Authorization':'Bearer adm'})

    def test_auth_is_required_even_with_public_status_and_navigation_deduplicates(self):
        response = self.client.get('/usage/?period=7d')
        self.assertEqual(response.status_code,302)
        self.assertIn('/console/login?next=',response.location)
        for path in ('/usage/api/stats', '/usage/api/quotas','/usage/healthz'):
            self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.get('/usage/',headers={'Authorization':'Bearer wrong'}).status_code,401)
        page = self.admin('/usage/').get_data(as_text=True)
        self.assertIn('lingqu.css',page)
        self.assertIn('usage.js',page)
        self.assertNotIn('TEST-ACCESS-TOKEN',page)
        self.assertEqual(page.count('href="/usage/"'),1)
        self.assertIn('aria-current="page">用量分析',page)
        self.rt.settings.links = [{'name':'旧用量','url':'/usage/'}]
        self.assertEqual(self.admin('/usage/').get_data(as_text=True).count('href="/usage/"'),1)
        page = self.client.get('/console/login?next=/usage/').get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"',page)[1]
        response = self.client.post('/console/login',data={'token':'adm','csrf_token':csrf,'next':'/usage/'})
        self.assertEqual(response.location,'/usage/')
        response = self.client.get('/usage/')
        self.assertEqual((response.status_code,response.headers['Cache-Control']),(200,'no-store'))
        self.assertEqual(self.client.get('/usage?period=7d').location,'/usage/?period=7d')

    def test_optional_and_not_exposed_on_proxy_port(self):
        app = create_app('testing',load(BASE))
        self.assertEqual(app.test_client().get('/usage/').status_code,404)
        client = Client(PortDispatcher(self.app),Response)
        self.assertEqual(client.get('/usage/',environ_overrides={'SERVER_PORT':'8090'}).status_code,404)
        self.assertEqual(self.admin('/usage/healthz').status_code,503)
        for query in ('period=week','provider=bogus','group='+'x'*129):
            self.assertEqual(self.admin('/usage/api/stats?'+query).status_code,400)

    def test_billing_cost_cache_semantics_filters_and_overlap_dedup(self):
        self.logs[2] = [self.row(), self.row(2,'a-gpt'), self.row(3,'manual'),
                        self.row(4,other='{"cache_tokens":60,"cache_creation_tokens":10,"usage_semantic":"anthropic"}')]
        self.logs[5] = [self.row(5,content='rate limit')]
        self.sync()
        body = self.admin('/usage/api/stats?period=24h').json
        self.assertEqual(body['totals']['requests'],3)
        self.assertEqual(body['totals']['errors'],1)
        self.assertEqual(body['totals']['cost'],7.5)
        self.assertEqual(body['totals']['input'],160)  # 30 + 30 + 100
        self.assertEqual(body['totals']['cache_read'],180)
        self.assertEqual(body['cost_source'],'newapi_billed')
        self.assertEqual(sum(sum(s['cost']) for s in body['series'].values()),7.5)
        filtered = self.admin('/usage/api/stats?period=24h&provider=codex&group=a').json
        self.assertEqual((filtered['totals']['requests'],filtered['totals']['cost']),(1,2.5))
        self.assertEqual(len(filtered['tables']['key']),1)
        self.clock.advance(20)
        self.sync()
        self.assertEqual(self.service.stats('24h','all','all')['totals']['requests'],3)
        self.assertTrue(any('start_timestamp=' in url for url,_ in self.calls))
        self.assertEqual(self.admin('/usage/healthz').status_code,200)
        self.assertNotIn('TEST-ACCESS-TOKEN',json.dumps(body))

    def test_state_is_per_app_and_http_reads_do_not_collect(self):
        self.logs[2] = [self.row()]
        self.sync()
        other = create_app('testing',load(BASE + CONFIG)).extensions['lingqu'].usage
        self.assertFalse(other.state.consume)
        with patch('app.usage.service.httpx.get', side_effect=AssertionError('page caused network traffic')):
            self.assertEqual(self.admin('/usage/api/stats').status_code,200)
            self.assertEqual(self.admin('/usage/api/quotas').status_code,200)
        count = len(self.calls)
        self.sync()
        self.assertEqual(len(self.calls),count)  # polling interval
        self.rt.stop.set()
        self.clock.advance(30)
        self.sync()
        self.assertEqual(len(self.calls),count)

    def test_prefix_aware_login_and_page_configuration(self):
        env = {'SCRIPT_NAME':'/lingqu'}
        response = self.client.get('/usage/',environ_overrides=env)
        self.assertTrue(response.location.startswith('/lingqu/console/login?next='))
        self.assertIn('/lingqu/usage/',response.location)
        page = self.client.get('/usage/',headers={'Authorization':'Bearer adm'},environ_overrides=env).get_data(as_text=True)
        self.assertIn('href="/lingqu/usage/" aria-current="page"',page)
        self.assertIn('/lingqu/usage/api/stats',page)

    def test_invalid_numeric_records_cannot_poison_the_stats_cache(self):
        self.logs[2] = [self.row()]
        self.sync()
        self.logs[2] = [self.row(2,quota=float('nan'))]
        self.clock.advance(20)
        self.sync()
        result = self.service.stats('24h','all','all')
        self.assertTrue(result['sync_error'])
        self.assertEqual(result['totals']['cost'],2.5)

    def test_failure_keeps_records_and_does_not_expose_upstream_body(self):
        self.logs[2] = [self.row()]
        self.sync()
        self.clock.advance(20)
        self.failure = 401
        self.sync()
        body = self.admin('/usage/api/stats').json
        self.assertEqual(body['totals']['requests'],1)
        self.assertTrue(body['sync_error'])
        self.assertNotIn('TEST-ACCESS-TOKEN',json.dumps(body))
        self.assertEqual(self.admin('/usage/healthz').status_code,503)
        self.clock.advance(20)
        self.failure = None
        self.sync()
        self.assertFalse(self.service.state.sync_error)

    def test_unknown_first_response_time_and_failed_backfill_retry(self):
        recent = self.row(1)
        older = self.row(2,created_at=int(self.clock())-86400,quota=float('nan'))
        self.logs[2] = [recent,older]
        self.sync()
        self.assertTrue(self.service.state.sync_error)
        self.assertEqual(self.service.state.watermarks['consume'],0)
        older['quota'] = 1250000
        older['other'] = '{"frt":-1,"cache_tokens":60}'
        self.clock.advance(20)
        self.sync()
        self.assertFalse(self.service.state.sync_error)
        self.assertEqual(self.service.stats('7d','all','all')['totals']['requests'],2)
        self.assertEqual(self.service.state.consume[2]['frt'],-1)
        starts = [int(parse_qs(urlsplit(url).query)['start_timestamp'][0]) for url,_ in self.calls
                  if '/api/log/' in url and 'type=2' in url]
        self.assertLess(starts[-1],older['created_at'])

    def test_retention_and_record_bound_are_visible(self):
        self.service.config['max_records'] = 2
        self.logs[2] = [self.row(1),self.row(2),self.row(3)]
        self.sync()
        result = self.service.stats('90d','all','all')
        self.assertEqual(result['totals']['requests'],2)
        self.assertTrue(result['sync_warning'])
        self.service.state.consume[9] = self.service.record(self.row(9,created_at=int(self.clock())-91*86400))
        with self.service.state.lock:
            self.service._prune(self.clock())
        self.assertNotIn(9,self.service.state.consume)

    def test_today_uses_configured_timezone_and_future_rows_are_excluded(self):
        self.sync()
        date = datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai'))
        midnight = int(date.replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        self.logs[2] = [self.row(1,created_at=midnight-1), self.row(2,created_at=midnight+1),
                        self.row(3,created_at=int(self.clock())+60)]
        self.clock.advance(20)
        self.sync()
        result = self.service.stats('today','all','all')
        self.assertEqual(result['start'],midnight)
        self.assertEqual(result['totals']['requests'],1)

    def test_quota_and_limits_reuse_runtime_snapshots_without_self_http(self):
        self.sync()
        self.rt.gate.update_quota('a/claude','conn1', {'short':{'used_pct':42,'reset_at':self.clock()+3600},
                                                    'weekly':{'used_pct':21,'reset_at':self.clock()+604800}})
        self.rt.manager.rows['r1'] = {'connections':[{'id':'conn1','name':'Main Claude'}]}
        self.rt.gate.charge('a/claude','claude-opus-5-5',5)
        with patch('app.usage.service.httpx.get',side_effect=AssertionError('unexpected request')):
            body = self.admin('/usage/api/quotas').json
        a = next(g for g in body['groups'] if g['group']=='a')
        self.assertEqual(a['limits'][0]['spent'],5)
        account = next(v for v in a['accounts'] if v['kind']=='claude')
        self.assertEqual(account['name'],'Main Claude')
        self.assertEqual(account['quotas'][0]['used'],42)
        self.assertIn('short',a['calibration']['claude'])
        self.clock.advance(400)
        body = self.admin('/usage/api/quotas').json
        a = next(g for g in body['groups'] if g['group']=='a')
        self.assertTrue(next(v for v in a['accounts'] if v['kind']=='claude')['note'])

    def test_billed_amount_does_not_drive_throttle_counts(self):
        from app.models import GateEvent
        self.logs[2] = [self.row(quota=500000000)]  # $1000 billed, independent of the $20 eUSD cap
        self.sync()
        self.assertEqual(self.service.stats('24h','all','all')['throttles'],[])
        db.session.add(GateEvent(ts=self.clock()-5,pool='a/claude',event='throttle_start',limit_value=20))
        db.session.commit()
        self.assertEqual(self.service.stats('24h','all','all')['throttles'],[
            {'group':'a','provider':'claude','limit':20,'count':1}])
        self.assertEqual(self.service.stats('24h','codex','all')['throttles'],[])

    def test_shared_account_windows_use_the_latest_snapshot_once(self):
        self.sync()
        extra = copy.deepcopy(self.rt.settings.pools['a/claude'])
        extra.name = 'a/claude-extra'
        self.rt.settings.pools[extra.name] = extra
        self.rt.gate.update_quota('a/claude','shared',{'short':{'used_pct':40,'reset_at':self.clock()+3600}})
        self.clock.advance(1)
        self.rt.gate.update_quota(extra.name,'shared',{'short':{'used_pct':42,'reset_at':self.clock()+3600}})
        group = next(g for g in self.service.quotas()['groups'] if g['group']=='a')
        account = next(a for a in group['accounts'] if a['kind']=='claude')
        self.assertEqual(len(account['quotas']),1)
        self.assertEqual(account['quotas'][0]['used'],42)

    def test_newapi_group_scope_and_removed_channels(self):
        self.service.config['scope'] = 'groups'
        for c in self.channels:
            c['group'] = 'team' if c['name'] != 'manual' else 'default'
        self.logs[2] = [self.row(group='team'), self.row(2,'manual',group='default')]
        self.sync()
        result = self.service.stats('24h','all','all')
        self.assertEqual(result['groups'],['team'])
        self.assertEqual(result['totals']['requests'],1)
        self.channels = []
        self.clock.advance(300)
        self.sync()
        self.assertEqual(self.service.stats('24h','all','all')['totals']['requests'],0)

    def test_runtime_lifecycle_and_usage_config_reload_boundary(self):
        with patch.object(self.rt,'restore'), patch.object(self.rt,'_loop') as loop:
            self.rt.start()
        self.assertIn('usage',[call.args[0] for call in loop.call_args_list])
        disabled = create_app('testing',load(BASE)).extensions['lingqu']
        with patch.object(disabled,'restore'), patch.object(disabled,'_loop') as loop:
            disabled.start()
        self.assertNotIn('usage',[call.args[0] for call in loop.call_args_list])
        replacement = load(BASE + CONFIG.replace('scope: managed','scope: groups'))
        with patch('app.runtime.settings_mod.load',return_value=(replacement,[],[])):
            ok,errors,_ = self.rt.reload()
        self.assertFalse(ok)
        self.assertIn('usage settings require a restart',errors)


class NewAPIPagingTest(unittest.TestCase):
    def test_pagination_and_stop_are_bounded(self):
        stopped = False
        calls = []
        api = NewAPI({},lambda:None,lambda:stopped)
        def get(path):
            calls.append(path)
            page = int(parse_qs(urlsplit(path).query)['p'][0])
            return {'items':[{'id':page}], 'total':3}
        api.get = get
        self.assertEqual(list(api.items('/api/log/?type=2',2)),[{'id':1},{'id':2}])
        self.assertTrue(api.truncated)
        stopped = True
        self.assertEqual(list(api.items('/api/log/',3)),[])
        self.assertEqual(len(calls),2)
