import threading
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.gate.hold import Gate, Ticket
from app.settings import load as read_settings
from tests.helpers import BASE, FakeClock, load

POOL = "a/claude"
MODEL = "claude-opus-5-5"
OLD = "{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }"
BUDGET = "{ kind: hourly_budget, cycle_limit: 1680, weight_profile: flat, flex: 0, h_min: 1, h_max: 100 }"


def configured(rule=BUDGET):
    return load(BASE.replace(OLD, rule))


class BudgetTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 10, 19, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.gate = Gate(configured(), clock=self.clock)

    def budget(self, gate=None):
        return (gate or self.gate).pool_status(POOL)["budget"][0]

    def quota(self, pct, window="weekly", reset=None):
        self.gate.update_quota(POOL, "c1", {window: {"used_pct": pct,
                               "reset_at": reset if reset is not None else self.clock() + 604800}})

    def test_formula_hour_change_and_snapshot_triggers(self):
        rows = []
        self.gate.budgets.on_snapshot = rows.append
        self.assertEqual(self.budget()["h_value"], 10)
        self.gate.charge(POOL, MODEL, 168)
        self.assertEqual(self.budget()["h_value"], 10)  # held until an actual recompute trigger
        self.assertEqual(len(rows), 1)
        self.clock.advance(3600)
        self.assertAlmostEqual(self.budget()["h_value"], 1512 / 167)
        self.assertEqual(len(rows), 2)
        self.quota(50, reset=self.clock() + 167 * 3600)
        self.assertAlmostEqual(self.budget()["h_value"], 840 / 167)
        self.gate.set_override(POOL, "limit", 5, self.clock() + 120)
        self.budget()
        self.assertEqual(rows[-1]["trigger"], "override")

    def test_workday_weights_timezone_and_partial_reset(self):
        g = Gate(configured(BUDGET.replace("weight_profile: flat", "weight_profile: workday")), clock=self.clock)
        b = self.budget(g)
        self.assertEqual(b["weight_now"], 0.2)
        self.assertAlmostEqual(b["weight_rem"], 5 * (10 + 14 * .2) + 48 * .5)
        self.quota(0, reset=self.clock() + 5400)
        self.assertEqual(self.budget()["weight_rem"], 1.5)

    def test_upstream_percent_and_post_observation_cost(self):
        self.quota(50)
        self.clock.advance(1)
        self.gate.charge(POOL, MODEL, 10)
        self.assertEqual(self.budget()["remaining_r"], 830)
        self.clock.advance(361)
        self.assertEqual(self.budget()["remaining_r"], 1670)  # stale readings stop driving R

    def test_percent_accumulates_and_threshold_crossing_recomputes(self):
        end = self.clock() + 604800
        self.quota(10, reset=end)
        before = self.budget()["h_value"]
        self.quota(10.6, reset=end)
        self.assertEqual(self.budget()["h_value"], before)
        self.quota(11.2, reset=end)
        self.assertLess(self.budget()["h_value"], before)
        self.quota(89.9, reset=end)
        self.quota(90, reset=end)
        self.assertEqual(self.budget()["h_value"], 1)

    def test_emergency_limits_override_and_cannot_timeout(self):
        self.quota(90)
        self.assertEqual(self.budget()["h_value"], 1)
        self.gate.set_override(POOL, "limit", 90, self.clock() + 1000)
        self.assertEqual(self.gate.money(POOL, MODEL, self.clock())[2], 1)
        self.gate.set_override(POOL, "lift", None, self.clock() + 1000)
        self.assertEqual(self.gate.money(POOL, MODEL, self.clock())[2], 1)
        self.quota(98)
        t = Ticket(POOL, MODEL, self.clock() - 500)
        self.assertEqual(self.gate.decide(self.gate.state(POOL), t, self.clock(), MODEL)[0], "reject")
        self.assertTrue(self.gate.admit(POOL, MODEL).rejected)

    def test_expired_brake_clears_but_stale_high_is_kept(self):
        reset = self.clock() + 7200
        self.quota(98, reset=reset)
        self.clock.advance(500)
        self.assertTrue(self.gate.admit(POOL, MODEL).rejected)
        self.clock.t = reset
        self.assertFalse(self.gate.admit(POOL, MODEL).rejected)

    def test_short_limit_real_and_fallback(self):
        g = Gate(configured(BUDGET.replace("h_max: 100", "h_max: 100, short_limit: 20")), clock=self.clock)
        g.update_quota(POOL, "c1", {"short": {"used_pct": 80, "reset_at": self.clock() + 18000}})
        self.assertAlmostEqual(self.budget(g)["h_value"], 4)
        self.clock.advance(1)
        g.charge(POOL, MODEL, 4)
        self.assertTrue(g.admit(POOL, MODEL).rejected)
        self.clock.advance(18001)
        self.assertFalse(g.admit(POOL, MODEL).rejected)
        g.charge(POOL, MODEL, 20)
        self.assertTrue(g.admit(POOL, MODEL).rejected)

    def test_cycle_hard_cap_scope_boundary_and_reload(self):
        rules = BUDGET + ", { kind: cycle_cap, scope: { model: 'claude-opus-*' }, cycle_limit: 5, anchor: 'mon 00:00' }"
        g = Gate(configured(rules), clock=self.clock)
        g.charge(POOL, MODEL, 5)
        self.clock.advance(7200)
        g.set_override(POOL, "lift", None, self.clock() + 604800)
        self.assertTrue(g.admit(POOL, MODEL).rejected)
        self.assertFalse(g.admit(POOL, "claude-sonnet-5").rejected)
        g.reload(configured(rules))
        self.assertTrue(g.admit(POOL, MODEL).rejected)
        self.clock.advance(604800)
        self.assertFalse(g.admit(POOL, MODEL).rejected)

    def test_waiting_request_wakes_for_emergency(self):
        self.gate.charge(POOL, MODEL, 50)
        self.gate.state(POOL).last_departure = self.clock()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.gate.admit(POOL, MODEL)), daemon=True)
        thread.start()
        deadline = time.monotonic() + 1
        while not self.gate.held_total() and time.monotonic() < deadline:
            time.sleep(.01)
        self.quota(98)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result[0].rejected)

    def test_shadow_never_enforces_quota_or_cycle(self):
        g = Gate(configured(BUDGET.replace("h_max: 100", "h_max: 100, mode: shadow") +
                            ", {kind: cycle_cap, cycle_limit: 1, mode: shadow}"), clock=self.clock)
        g.charge(POOL, MODEL, 50)
        g.update_quota(POOL, "c1", {"weekly": {"used_pct": 99, "reset_at": self.clock() + 604800}})
        t = g.admit(POOL, MODEL)
        self.assertEqual(t.result, "shadow_would_hold")
        self.assertIsNone(g.pool_status(POOL)["limit"])
        self.assertEqual(self.budget(g)["mode"], "shadow")

    def test_validation_rejects_bad_budget_weights_and_numbers(self):
        for fragment in ("cycle_period: -7", "h_min: 50, h_max: 1", "flex: -1", "anchor: nonsense",
                         "timezone: Mars/Olympus", "cycle_limit: .nan", "short_limit: -2", "weight_profile: missing"):
            with self.subTest(fragment=fragment):
                rule = "{kind: hourly_budget, cycle_limit: 1680, " + fragment + "}"
                _, errors, _ = read_settings(text=BASE.replace(OLD, rule))
                self.assertTrue(errors)
        _, errors, _ = read_settings(text=BASE + "\nweight_profiles: {bad: [1, 2]}\n")
        self.assertTrue(errors)
        _, errors, _ = read_settings(text=BASE + '\nweight_profiles: {bad: {weekday: {"00-10": 1, "09-12": 2}}}\n')
        self.assertTrue(errors)

    def test_custom_profile_expands(self):
        s = load(BASE + '\nweight_profiles: {custom: {weekday: {"09-18": 1, default: 0.1}, weekend: {default: 0.4}}}\n')
        self.assertEqual(len(s.weight_profiles["custom"]), 168)
        self.assertEqual(s.weight_profiles["custom"][9], 1)
        self.assertEqual(s.weight_profiles["custom"][144], .4)

    def test_new_model_budget_scope_uses_existing_charges(self):
        self.gate.charge(POOL, MODEL, 12)
        scoped = BUDGET.replace("h_max: 100", "h_max: 100, scope: {model: 'claude-opus-*'}")
        self.gate.reload(configured(scoped))
        self.assertEqual(self.gate.money(POOL, MODEL, self.clock())[3], 12)

    def test_calibration_is_only_a_hint(self):
        end = self.clock() + 604800
        self.quota(10, reset=end)
        self.clock.advance(1)
        self.gate.charge(POOL, MODEL, 8)
        self.clock.advance(1)
        self.quota(12, reset=end)
        hint = self.gate.budgets.calibration[(POOL, "c1", "weekly")]
        self.assertEqual(hint["suggested_limit"], 400)
        self.assertEqual(self.gate.settings.pools[POOL].rules[0].cycle_limit, 1680)

    def test_idle_pools_get_hourly_snapshots(self):
        rows = []
        self.gate.budgets.on_snapshot = rows.append
        self.gate.tick()
        self.assertEqual(len(rows), 1)
        self.clock.advance(3600)
        self.gate.tick()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["trigger"], "hour")
