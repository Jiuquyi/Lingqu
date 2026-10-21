"""Model name normalization and usage -> eUSD (DESIGN §3.1, §3.3).

A price is a dict in $ / 1M tokens with keys input, output, cache_read, cache_write_5m,
cache_write_1h and optionally per_request (a fixed amount per call) and multiplier.
"""
from dataclasses import dataclass

LONG_CONTEXT_SUFFIX = "[1m]"


@dataclass
class Usage:
    """The four kinds of tokens both protocols reduce to (DESIGN §3.1)."""
    input: int = 0             # uncached input
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0            # includes reasoning / thinking
    reasoning: int = 0         # informational only, already inside output

    @property
    def context(self):
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def as_dict(self):
        return {"input": self.input, "cache_read": self.cache_read, "cache_write_5m": self.cache_write_5m,
                "cache_write_1h": self.cache_write_1h, "output": self.output, "reasoning": self.reasoning}


def complete(price):
    """Fill the cache prices a source left out from the input price."""
    p = {k: float(v) for k, v in price.items() if v is not None}
    p.setdefault("input", 0.0)
    p.setdefault("output", p["input"])
    p.setdefault("cache_read", 0.1 * p["input"])
    p.setdefault("cache_write_5m", 1.25 * p["input"])
    p.setdefault("cache_write_1h", 2.0 * p["input"])
    return p


def cost(price, usage, input_only=False):
    """eUSD for a usage. input_only: everything except output (what message_start carries)."""
    m = price.get("multiplier", 1.0)
    if price.get("per_request") is not None:
        return 0.0 if input_only else float(price["per_request"]) * m
    total = (usage.input * price["input"] + usage.cache_read * price["cache_read"]
             + usage.cache_write_5m * price["cache_write_5m"] + usage.cache_write_1h * price["cache_write_1h"])
    if not input_only:
        total += usage.output * price["output"]
    return total / 1_000_000 * m


def candidates(model, strip_prefixes):
    """Names to look a model up by, most specific first (DESIGN §3.3)."""
    out = []

    def add(name):
        if name and name not in out:
            out.append(name)

    names = [model]
    for p in strip_prefixes:
        if model.startswith(p):
            names.append(model[len(p):])
    if "/" in model:
        names.append(model.rsplit("/", 1)[1])
    for n in names:
        add(n)
    for n in names:
        if n.endswith(LONG_CONTEXT_SUFFIX):
            add(n[:-len(LONG_CONTEXT_SUFFIX)])
    return out


class PriceTable:
    """Immutable snapshot; the sync thread swaps in a new one."""

    def __init__(self, prices=None, by_provider=None, source="static", synced_at=None):
        self.prices = {k: complete(v) for k, v in (prices or {}).items()}
        # 9router source: provider alias (cc, cx, claude, ...) -> model -> price
        self.by_provider = {pv: {m: complete(v) for m, v in models.items()}
                            for pv, models in (by_provider or {}).items()}
        self.source, self.synced_at = source, synced_at

    def lookup(self, model, kind=None, strip_prefixes=(), overrides=None):
        """(price, matched name) or (None, None)."""
        if not model:
            return None, None
        names = candidates(model, strip_prefixes)
        for n in names:
            if overrides and n in overrides:
                return complete(overrides[n]), n
        provider = model.split("/", 1)[0] if "/" in model else None
        providers = [provider] if provider else []
        providers += {"claude": ["cc", "claude", "anthropic"], "codex": ["cx", "codex", "openai"]}.get(kind, [])
        for n in names:
            if n in self.prices:
                return self.prices[n], n
            for pv in providers:
                if n in self.by_provider.get(pv, {}):
                    return self.by_provider[pv][n], f"{pv}/{n}"
        return None, None

    def to_json(self):
        return {"prices": self.prices, "by_provider": self.by_provider}
