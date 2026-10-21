import unittest

from app.pricing import newapi, ninerouter
from app.pricing.table import PriceTable, Usage, candidates, cost


class PricingTestCase(unittest.TestCase):
    def test_newapi_ratios(self):
        p = newapi.to_prices([
            {"model_name": "claude-opus-5-5", "model_ratio": 2.5, "completion_ratio": 5, "cache_ratio": 0.1,
             "create_cache_ratio": 1.25, "quota_type": 0},
            {"model_name": "img", "model_price": 0.04, "quota_type": 1},
        ])
        self.assertAlmostEqual(p["claude-opus-5-5"]["input"], 5)
        self.assertAlmostEqual(p["claude-opus-5-5"]["output"], 25)
        self.assertAlmostEqual(p["claude-opus-5-5"]["cache_read"], 0.5)
        self.assertAlmostEqual(p["claude-opus-5-5"]["cache_write_5m"], 6.25)
        self.assertEqual(p["img"]["per_request"], 0.04)

    def test_ninerouter_prices(self):
        p = ninerouter.to_prices({"cc": {"claude-opus-5-5": {"input": 5, "output": 25, "cached": 0.5,
                                                             "cache_creation": 6.25}}})
        t = PriceTable(by_provider=p)
        price, name = t.lookup("cc/claude-opus-5-5", "claude", ["cc/"])
        self.assertEqual(name, "cc/claude-opus-5-5")
        self.assertEqual(price["cache_read"], 0.5)
        price, _ = t.lookup("claude-opus-5-5", "claude")
        self.assertIsNotNone(price)

    def test_normalization(self):
        self.assertEqual(candidates("cc/claude-opus-5-5[1m]", ["cc/"]),
                         ["cc/claude-opus-5-5[1m]", "claude-opus-5-5[1m]", "cc/claude-opus-5-5", "claude-opus-5-5"])
        t = PriceTable({"claude-opus-5-5": {"input": 5, "output": 25}})
        price, name = t.lookup("cc/claude-opus-5-5[1m]", "claude", ["cc/"])
        self.assertEqual(name, "claude-opus-5-5")
        self.assertAlmostEqual(price["cache_write_5m"], 6.25)
        self.assertAlmostEqual(price["cache_write_1h"], 10)
        price, name = t.lookup("os/cx/x/claude-opus-5-5", "claude", [])
        self.assertEqual(name, "claude-opus-5-5")

    def test_overrides_win(self):
        t = PriceTable({"m": {"input": 5, "output": 25}})
        price, _ = t.lookup("m", "claude", [], {"m": {"input": 1, "output": 2}})
        self.assertEqual(price["input"], 1)

    def test_cost(self):
        t = PriceTable({"m": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write_5m": 6.25}})
        price, _ = t.lookup("m")
        u = Usage(input=1000, cache_read=600_000, cache_write_5m=10_000, output=2000)
        expect = (1000 * 5 + 600_000 * 0.5 + 10_000 * 6.25 + 2000 * 25) / 1e6
        self.assertAlmostEqual(cost(price, u), expect)
        self.assertAlmostEqual(cost(price, u, input_only=True), expect - 2000 * 25 / 1e6)
        self.assertAlmostEqual(cost({**price, "multiplier": 2}, u), 2 * expect)
