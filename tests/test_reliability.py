"""Metering and ledger under failure: a client hanging up at any chunk, a gate that cannot take a
charge, a database that cannot be written, and recovery that must not count anything twice."""
import json
import time
import unittest
from unittest.mock import patch

from werkzeug.test import Client

from app import create_app, db
from app.gate.hold import Gate
from app.gate.shared import CommitUnconfirmed, SharedStateError
from app.ledger import LedgerWriter, prune_meters, restore_counters
from app.models import Ledger, MeterEvent
from app.ports import PortDispatcher
from tests.fake_router import FakeRouter, anthropic_stream
from tests.helpers import BASE, FakeClock, load

SSE = [("Content-Type", "text/event-stream")]
POOL, MODEL = "a/claude", "claude-opus-5-5"
INPUT = (10 * 5 + 6_000_000 * 0.5) / 1e6      # $3.00005: the input side of anthropic_stream(cache_read=6M)
TOTAL = INPUT + 500 * 25 / 1e6


class ProxyFailureTest(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()
        self.chunks = anthropic_stream(cache_read=6_000_000)
        # A pause between chunks keeps each one a read of its own.
        self.router.reply = lambda *a: (200, SSE, self.chunks, 0.02)
        s = load(BASE.replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url)
                 .replace("limit: 20", "limit: 1000"))
        self.app = create_app("testing", settings=s)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]
        self.proxy = Client(PortDispatcher(self.app))

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.router.close()

    def post(self):
        return self.proxy.post("/v1/messages", data=json.dumps({"model": MODEL, "stream": True}),
                               headers={"x-api-key": "lq-a-claude"}, content_type="application/json",
                               environ_overrides={"SERVER_PORT": "8090"})

    def rows(self):
        self.rt.ledger.flush()
        return Ledger.query.order_by(Ledger.id).all()

    def spent(self):
        return self.rt.gate.counters.spent(POOL, "*", time.time())

    def test_hanging_up_at_any_chunk_keeps_what_was_read(self):
        for k in range(len(self.chunks) + 1):
            with self.subTest(chunks_read=k):
                self.rt.gate.counters.reset(POOL)
                before = len(self.rows())
                r = self.post()
                it = iter(r.response)
                got = b"".join(next(it) for _ in range(k))
                r.close()
                rows = self.rows()
                self.assertEqual(len(rows), before + 1)
                row = rows[-1]
                self.assertEqual(got, b"".join(self.chunks[:k]))      # what the client got is untouched
                # The test client takes the first chunk before it returns, so with k = 0 too
                # message_start was handed over: its usage is kept, in the row and in the window.
                self.assertEqual(row.cache_read_tokens, 6_000_000)
                self.assertAlmostEqual(row.eusd_input, INPUT, places=6)
                self.assertGreaterEqual(self.spent(), INPUT - 1e-9)
                self.assertAlmostEqual(self.spent(), row.eusd, places=6)
                if k == len(self.chunks):
                    self.assertEqual((row.usage_source, row.error_class), ("upstream", None))
                    self.assertAlmostEqual(row.eusd, TOTAL, places=6)
                else:
                    self.assertEqual((row.usage_source, row.error_class), ("estimated", "client_closed"))
                self.assertEqual(self.rt.gate.state(POOL).counts.in_flight["*"], 0)

    def test_body_never_started_still_writes_its_row(self):
        from werkzeug.test import EnvironBuilder
        env = EnvironBuilder(path="/v1/messages", method="POST", data=json.dumps({"model": MODEL, "stream": True}),
                             headers={"x-api-key": "lq-a-claude"}, content_type="application/json",
                             environ_overrides={"SERVER_PORT": "8090"}).get_environ()
        app_iter = PortDispatcher(self.app)(env, lambda status, headers, exc_info=None: None)
        app_iter.close()                                          # the client left before any byte
        row = self.rows()[0]
        self.assertEqual((row.eusd, row.error_class, row.usage_source), (0, "client_closed", "estimated"))
        self.assertEqual(self.rt.gate.state(POOL).counts.in_flight["*"], 0)

    def test_client_closed_does_not_touch_the_error_streak(self):
        self.rt.last_errors[POOL] = {"class": "account_auth", "at": time.time(), "count": 1, "streak": 1,
                                     "since": time.time(), "detail": "", "alerted": False}
        r = self.post()
        next(iter(r.response))
        r.close()
        self.assertEqual(self.rt.last_errors[POOL]["streak"], 1)

    def test_gate_down_keeps_the_stream_and_the_row(self):
        with patch.object(self.rt.gate, "charge", side_effect=SharedStateError("shared gate is busy")):
            body = self.post().get_data()
        self.assertEqual(body, b"".join(self.chunks))
        row = self.rows()[0]
        self.assertAlmostEqual(row.eusd, TOTAL, places=6)
        self.assertAlmostEqual(row.eusd_pending, TOTAL, places=6)  # observed, not in the window yet
        self.assertEqual(self.spent(), 0)
        event = MeterEvent.query.one()                               # the input side, for recovery
        self.assertEqual((event.request_id, event.seq), (row.request_id, 1))
        self.assertAlmostEqual(event.amount, INPUT, places=6)
        self.assertEqual(self.rt.metering_status()["pending"], 2)
        self.rt.retry_charges()
        self.rt.retry_charges()
        self.assertAlmostEqual(self.spent(), TOTAL, places=6)
        status = self.rt.metering_status()
        self.assertEqual((status["pending"], status["charges"]), (0, {"failed": 2, "recovered": 2}))

    def test_unconfirmed_commit_is_not_counted_twice(self):
        real = self.rt.gate.charge

        def lost_answer(*a, **kw):  # the commit takes effect, its answer never comes back
            real(*a, **kw)
            raise CommitUnconfirmed("shared gate commit unconfirmed")
        with patch.object(self.rt.gate, "charge", side_effect=lost_answer):
            self.post().get_data()
        self.assertAlmostEqual(self.spent(), TOTAL, places=6)
        self.rt.retry_charges()
        self.assertAlmostEqual(self.spent(), TOTAL, places=6)
        self.assertEqual(self.rt.metering_status()["pending"], 0)

    def test_unconfirmed_charge_too_old_to_retry_is_reported_lost(self):
        with patch.object(self.rt.gate, "charge", side_effect=CommitUnconfirmed("unconfirmed")):
            self.post().get_data()
        for ev in self.rt.unsettled:
            ev["ts"] -= 3600
        self.rt.retry_charges()
        self.assertEqual(self.rt.metering_status()["charges"]["lost"], 2)
        self.rt.ledger.flush()
        from app.models import GateEvent
        self.assertEqual(GateEvent.query.filter_by(event="charge_lost").count(), 2)

    def test_parser_failure_does_not_cut_the_stream(self):
        with patch("app.protocol.anthropic_messages.AnthropicTracker.on_event", side_effect=ValueError("bug")):
            body = self.post().get_data()
        self.assertEqual(body, b"".join(self.chunks))
        self.assertEqual(self.rows()[0].usage_source, "estimated")

    def test_usage_quality_in_status(self):
        self.post().get_data()
        q = self.rt.metering_status()["quality_1h"][0]
        self.assertEqual((q["pool"], q["protocol"], q["sources"]), (POOL, "anthropic", {"upstream": 1}))
        self.assertEqual(q["default_price"], 0)


class LedgerFailureTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing", load())
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.clock = FakeClock()
        self.rt = self.app.extensions["lingqu"]
        self.writer = self.rt.ledger = LedgerWriter(self.app, self.clock)
        self.rt.clock = self.clock

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def row(self, rid="r1", **kw):
        return dict({"request_id": rid, "ts": self.clock(), "pool": POOL, "eusd": 1.0}, **kw)

    def failing_for(self, seconds):
        with patch("app.ledger.insert_once", side_effect=RuntimeError("database unavailable")):
            for _ in range(seconds):
                self.clock.advance(1)
                with self.assertRaises(RuntimeError):
                    self.writer.flush()

    def test_backlog_age_survives_failed_writes(self):
        self.writer.add(self.row())
        self.failing_for(60)
        self.assertEqual(self.writer.lag(), 60)
        self.assertTrue(self.rt.health()["ok"])
        self.failing_for(120)
        self.assertEqual(self.writer.lag(), 180)
        m = self.writer.metrics()
        self.assertEqual((m["queued"]["rows"], m["failures"], m["failing_sec"]), (1, 180, 179))
        self.assertIn("database unavailable", m["last_error"])
        self.assertFalse(self.rt.health()["ok"])
        self.writer.flush()
        m = self.writer.metrics()
        self.assertEqual((m["lag_sec"], m["failures"], m["failing_sec"], m["last_success"]), (0, 0, 0, self.clock()))
        self.assertEqual(Ledger.query.count(), 1)

    def test_newer_entries_do_not_hide_an_old_backlog(self):
        self.writer.add(self.row())
        self.failing_for(100)
        self.writer.add_event({"ts": self.clock(), "pool": POOL, "event": "throttle_start"})
        self.assertEqual(self.writer.lag(), 100)

    def test_every_queue_has_the_same_bound(self):
        with patch("app.ledger.MAX_PENDING", 3):
            for i in range(5):
                self.writer.add(self.row(f"r{i}"))
                self.writer.add_event({"ts": self.clock(), "pool": POOL, "event": "e"})
                self.writer.add_meter({"request_id": f"r{i}", "seq": 1, "ts": self.clock(), "pool": POOL, "amount": 1})
            self.assertEqual(self.writer.metrics()["dropped"], {"rows": 2, "events": 2, "meters": 2})
            self.writer.add(self.row("late"))
            self.failing_for(1)                       # the failed batch goes back within the bound
            self.assertEqual({k: len(q) for k, q in self.writer.queues.items()},
                             {"rows": 3, "events": 3, "observations": 0, "meters": 3})
        self.assertEqual(self.writer.dropped, 7)

    def test_a_batch_written_twice_is_stored_once(self):
        row = self.row()
        meter = {"request_id": "r1", "seq": 1, "ts": self.clock(), "pool": POOL, "amount": .5}
        for _ in range(2):  # a commit that went through, answered with an error, then retried
            self.writer.add(dict(row))
            self.writer.add_meter(dict(meter))
            self.writer.flush()
        self.assertEqual((Ledger.query.count(), MeterEvent.query.count()), (1, 1))

    def test_rows_without_request_id_are_still_written(self):
        self.writer.add({"ts": self.clock(), "pool": POOL})
        self.writer.add({"ts": self.clock(), "pool": POOL, "status": 503})
        self.writer.flush()
        self.assertEqual(Ledger.query.count(), 2)
        self.assertEqual(Ledger.query.first().input_tokens, 0)   # column defaults still apply


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.settings = load()
        self.app = create_app("testing", self.settings)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.clock = FakeClock()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def restored(self):
        g = Gate(self.settings, clock=self.clock)
        restore_counters(self.app, g, self.clock)
        return g

    def test_running_request_input_is_recovered_once(self):
        now = self.clock()
        # Still running at the crash: only its input side was written.
        db.session.add(MeterEvent(request_id="run", seq=1, ts=now - 30, pool=POOL, model=MODEL, user_ref="alice", amount=3))
        # Settled: its row has the input too; the event must not add it again.
        db.session.add(MeterEvent(request_id="done", seq=1, ts=now - 20, pool=POOL, model=MODEL, amount=2))
        db.session.add(Ledger(request_id="done", ts=now - 25, pool=POOL, model_req=MODEL, eusd=5, eusd_input=2,
                              counted_at=now - 20, accounted_at=now - 10))
        db.session.commit()
        g = self.restored()
        self.assertAlmostEqual(g.counters.spent(POOL, "*", now), 8)
        self.assertAlmostEqual(g.counters.spent("@user/alice", "*", now), 3)
        self.assertAlmostEqual(g.counters.total_since(POOL, None, now - 86400, now), 8)
        self.assertAlmostEqual(self.restored().counters.spent(POOL, "*", now), 8)   # replaying again changes nothing

    def test_settled_events_are_pruned_and_orphans_kept(self):
        now = self.clock()
        db.session.add(MeterEvent(request_id="old-done", seq=1, ts=now - 3 * 3600, pool=POOL, amount=1))
        db.session.add(Ledger(request_id="old-done", ts=now - 3 * 3600, pool=POOL, eusd=1))
        db.session.add(MeterEvent(request_id="old-orphan", seq=1, ts=now - 3 * 3600, pool=POOL, amount=1))
        db.session.add(MeterEvent(request_id="new-done", seq=1, ts=now - 60, pool=POOL, amount=1))
        db.session.add(Ledger(request_id="new-done", ts=now - 60, pool=POOL, eusd=1))
        db.session.commit()
        self.assertEqual(prune_meters(self.app, self.clock), 1)
        self.assertEqual(sorted(e.request_id for e in MeterEvent.query), ["new-done", "old-orphan"])

    def test_charge_key_is_idempotent_and_forgotten_in_time(self):
        g = Gate(self.settings, clock=self.clock)
        first = g.charge(POOL, MODEL, 1, key="r:1")
        self.clock.advance(5)
        self.assertEqual(g.charge(POOL, MODEL, 1, key="r:1"), first)
        self.assertEqual(g.counters.spent(POOL, "*", self.clock()), 1)
        self.clock.advance(1000)
        g.charge(POOL, MODEL, 1, key="other:1")
        self.assertNotIn("r:1", g.applied)


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing", load())
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_failed_recovery_is_not_ready_and_retried(self):
        db.session.add(Ledger(request_id="x", ts=time.time() - 60, pool=POOL, model_req=MODEL, eusd=4, eusd_input=1,
                              counted_at=time.time() - 60, accounted_at=time.time() - 50))
        db.session.commit()
        with patch("app.runtime.restore_counters", side_effect=RuntimeError("database is locked")):
            self.assertFalse(self.rt.recover())
        self.assertFalse(self.rt.health()["ok"])
        self.assertEqual(self.rt.health()["recovery"]["state"], "failed")
        t = self.rt.gate.admit(POOL, MODEL)
        self.assertTrue(t.rejected)
        self.assertIn("rebuilding", t.message)
        self.rt._gate_tick()                                   # the gate thread retries
        self.assertTrue(self.rt.health()["ok"])
        self.assertEqual(self.rt.recovery["how"], "restored")
        self.assertAlmostEqual(self.rt.gate.counters.spent(POOL, "*", time.time()), 4)
        self.rt._gate_tick()
        self.assertAlmostEqual(self.rt.gate.counters.spent(POOL, "*", time.time()), 4)   # not again

    def test_partial_recovery_is_not_added_twice(self):
        db.session.add(Ledger(request_id="x", ts=time.time() - 60, pool=POOL, model_req=MODEL, eusd=4,
                              accounted_at=time.time() - 50))
        db.session.commit()
        with patch.object(self.rt.quota, "load", side_effect=RuntimeError("quota table missing")):
            self.assertFalse(self.rt.recover())
        self.assertTrue(self.rt.recover())
        self.assertAlmostEqual(self.rt.gate.counters.spent(POOL, "*", time.time()), 4)


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()
        self.router.reply = lambda *a: (200, SSE, anthropic_stream(), 0.01)
        s = load(BASE.replace("http://r1:20128", self.router.url).replace("http://r2:20128", self.router.url)
                 .replace("server: { admin_token: adm, fail_mode: open }",
                          "server: { admin_token: adm, fail_mode: open, max_active: 1, max_body_in_flight_mb: 1 }"))
        self.app = create_app("testing", settings=s)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.rt = self.app.extensions["lingqu"]
        self.disp = PortDispatcher(self.app)
        self.proxy = Client(self.disp)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.router.close()

    def post(self, body=None, key="lq-a-claude"):
        return self.proxy.post("/v1/messages", data=body or json.dumps({"model": MODEL, "stream": True}),
                               headers={"x-api-key": key}, content_type="application/json",
                               environ_overrides={"SERVER_PORT": "8090"})

    def test_full_process_answers_503_and_frees_on_close(self):
        first = self.post()
        it = iter(first.response)
        next(it)                                                   # still streaming: holds the only slot
        second = self.post()
        self.assertEqual(second.status_code, 503)
        self.assertIn(b"busy", second.get_data())
        self.assertEqual(len(self.router.requests), 1)            # turned away before going upstream
        status = Client(self.disp).get("/healthz", environ_overrides={"SERVER_PORT": "8091"})
        self.assertEqual(status.get_json()["capacity"]["active"], 1)
        first.close()
        self.assertEqual(self.rt.capacity.status()["active"], 0)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.rt.capacity.status()["rejected_1h"], {"active": 1})

    def test_unknown_key_and_short_answers_do_not_keep_a_slot(self):
        self.assertEqual(self.post(key="nope").status_code, 401)
        self.router.reply = lambda *a: (200, [("Content-Type", "application/json")], [b'{"input_tokens": 5}'], 0)
        r = self.proxy.post("/v1/messages/count_tokens", data=b'{"model":"m"}', headers={"x-api-key": "lq-a-claude"},
                            content_type="application/json", environ_overrides={"SERVER_PORT": "8090"})
        r.get_data()
        r.close()                                                   # what every WSGI server does
        self.assertEqual(self.rt.capacity.status()["active"], 0)

    def test_body_bytes_are_bounded(self):
        cap = self.rt.capacity
        cap.max_active = 10
        big = json.dumps({"model": MODEL, "stream": True, "pad": "x" * 700_000}).encode()
        first = self.post(big)
        next(iter(first.response))
        self.assertEqual(self.post(big).status_code, 503)        # 2 x 0.7 MB > 1 MiB
        self.assertEqual(cap.status()["rejected_1h"], {"body": 1})
        first.close()
        self.assertEqual(cap.status()["body_mb"], 0)


@unittest.skipUnless(__import__("shutil").which("redis-server"), "redis-server is needed for shared-state tests")
class SharedRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        import redis
        cls.temp = tempfile.TemporaryDirectory(prefix="lingqu-redis-")
        cls.socket = cls.temp.name + "/redis.sock"
        cls.server = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", cls.socket, "--save", "",
                                       "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.client = redis.Redis(unix_socket_path=cls.socket)
        for _ in range(100):
            try:
                cls.client.ping()
                break
            except redis.ConnectionError:
                time.sleep(.02)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(5)
        cls.temp.cleanup()

    def setUp(self):
        import uuid
        self.ns = "test-" + uuid.uuid4().hex
        self.settings = load(BASE + '\nredis: {url: "unix://' + self.socket + '", namespace: ' + self.ns + '}')
        self.app = create_app("testing", self.settings)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.a = self.app.extensions["lingqu"]
        import copy
        from app.runtime import Runtime
        self.b = Runtime(self.app, copy.deepcopy(self.settings))
        now = time.time()
        db.session.add(Ledger(request_id="old", ts=now - 100, pool=POOL, model_req=MODEL, eusd=5, eusd_input=2,
                              counted_at=now - 100, accounted_at=now - 90))
        db.session.add(MeterEvent(request_id="running", seq=1, ts=now - 30, pool=POOL, model=MODEL, amount=3))
        db.session.commit()

    def tearDown(self):
        for key in self.client.scan_iter("{" + self.ns + "}:*"):
            self.client.delete(key)
        self.a.gate.redis.close()
        self.b.gate.redis.close()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def spent(self, rt):
        return rt.gate.pool_status(POOL)["spent_1h"]

    def test_first_start_builds_from_the_ledger_once(self):
        self.assertTrue(self.a.recover())
        self.assertTrue(self.b.recover())
        self.assertEqual((self.a.recovery["how"], self.b.recovery["how"]), ("built", "present"))
        self.assertAlmostEqual(self.spent(self.b), 8)
        self.assertTrue(self.a.health()["ok"])

    def test_lost_state_is_refused_then_rebuilt(self):
        self.a.recover()
        self.b.recover()
        self.a.gate.charge(POOL, MODEL, 1, key="live:1")
        self.client.delete("{" + self.ns + "}:gate:state")          # Redis restarted without persistence
        self.assertTrue(self.a.gate.admit(POOL, MODEL).rejected)
        self.assertFalse(self.b.health()["ok"])
        self.b._gate_tick()
        self.assertEqual(self.b.recovery["how"], "rebuilt")
        self.a._gate_tick()                                            # nothing to do: its next transaction
        self.assertAlmostEqual(self.spent(self.a), 8)                 # reads the rebuilt state
        self.assertFalse(self.a.gate.admit(POOL, MODEL).rejected)

    def test_unconfirmed_commit_retried_on_another_instance_counts_once(self):
        self.a.recover()
        self.b.recover()
        real = self.a.gate.redis.eval

        def lost_answer(*args):
            real(*args)
            import redis
            raise redis.ConnectionError("connection reset after the write")
        with patch.object(self.a.gate.redis, "eval", side_effect=lost_answer):
            with self.assertRaises(CommitUnconfirmed):
                self.a.gate.charge(POOL, MODEL, 1, key="req:2")
        self.assertAlmostEqual(self.spent(self.b), 9)
        self.b.gate.charge(POOL, MODEL, 1, key="req:2")
        self.assertAlmostEqual(self.spent(self.a), 9)

    def test_health_needs_no_transaction_and_sees_a_config_mismatch(self):
        self.a.recover()
        self.b.recover()
        before = self.a.gate.lock.metrics()["transactions"]
        self.assertTrue(self.a.health()["ok"])
        self.assertEqual(self.a.gate.lock.metrics()["transactions"], before)
        self.b.settings.max_queue = 3
        self.b.gate.reload(self.b.settings)                           # one instance reloaded, not the other
        self.assertFalse(self.a.health()["ok"])
        self.assertIn("differs", self.a.health()["coordination"]["error"])


class ShadowComparisonTest(unittest.TestCase):
    """Phase 3: shadow rules to compare before anything enforces."""

    def gate(self, rules):
        s = load(BASE.replace("rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]", "rules: " + rules))
        self.clock = FakeClock()
        return Gate(s, clock=self.clock)

    def test_two_concurrency_limits_side_by_side(self):
        g = self.gate("[{ kind: concurrency, max: 2, mode: shadow }, { kind: concurrency, max: 3, mode: shadow }]")
        tickets = [g.admit(POOL, MODEL) for _ in range(4)]
        self.assertEqual([t.result for t in tickets], ["pass", "pass", "shadow_would_hold", "shadow_would_hold"])
        c = g.pool_status(POOL)["concurrency"]
        self.assertEqual(c["would_hold_by_rule_1h"], {"concurrency<=2": 2, "concurrency<=3": 1})
        self.assertEqual(c["would_hold_1h"], {"concurrency": 3})

    def test_reservation_would_hold_and_its_error(self):
        g = self.gate("[{ kind: hourly_cap, limit: 10, reserve: shadow }]")
        for _ in range(10):
            g.reservations.observe(POOL, "none", 3.0)
        g.charge(POOL, MODEL, 5)
        first = g.admit(POOL, MODEL)            # 5 + 3 reserved < 10
        second = g.admit(POOL, MODEL)           # 5 + 3 + 3 >= 10: a reservation would have held it
        self.assertEqual((first.result, second.result), ("pass", "pass"))  # shadow only
        r = g.pool_status(POOL)["reservation"]
        self.assertEqual((r["reserved"], r["in_flight"], r["would_hold_1h"]), (6.0, 2, 1))
        g.charge(POOL, MODEL, 2.0, key=f"{first.id}:1")          # its message_start: 1 less than estimated
        g.finish(second)                                          # ended before any input usage
        r = g.pool_status(POOL)["reservation"]
        self.assertEqual((r["reserved"], r["in_flight"]), (0, 0))
        self.assertEqual((r["error_1h"]["n"], r["error_1h"]["bias"], r["error_1h"]["under_share"]), (1, 1.0, 0.0))

    def test_no_estimate_without_enough_samples(self):
        g = self.gate("[{ kind: hourly_cap, limit: 10, reserve: shadow }]")
        g.reservations.observe(POOL, "warm", 3.0)
        g.admit(POOL, MODEL)
        self.assertEqual(g.pool_status(POOL)["reservation"]["in_flight"], 0)

    def test_reserve_settings(self):
        from app import settings as settings_mod
        for rule, error in (("{ kind: hourly_cap, limit: 1, reserve: enforce }", "not implemented"),
                            ("{ kind: concurrency, max: 1, reserve: shadow }", "hourly_cap and hourly_budget only"),
                            ("{ kind: hourly_cap, limit: 1, reserve: shadow, reserve_quantile: 2 }", "(0, 1]")):
            text = BASE.replace("rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]", f"rules: [{rule}]")
            _, errors, _ = settings_mod.load(text=__import__("textwrap").dedent(text))
            self.assertTrue(any(error in e for e in errors), (rule, errors))

    def test_settled_rows_feed_the_estimates(self):
        app = create_app("testing", load(BASE.replace("limit: 20, on_exceed", "limit: 20, reserve: shadow, on_exceed")))
        with app.app_context():
            db.create_all()
            rt = app.extensions["lingqu"]
            for i in range(12):
                db.session.add(Ledger(request_id=f"r{i}", ts=time.time() - 60, pool=POOL, cache_state="warm",
                                      usage_source="upstream", eusd=2, eusd_input=1.0 + i / 10))
            db.session.commit()
            rt.recover()
            self.assertAlmostEqual(rt.gate.reservations.estimate(POOL, "warm", .75), 1.9)
            rt.metered({"pool": POOL, "cache_state": "cold", "usage_source": "upstream", "eusd_input": 5.0})
            self.assertEqual(list(rt.gate.reservations.samples[POOL, "cold"]), [5.0])
            db.session.remove()
            db.drop_all()
