import textwrap

from app import settings as settings_mod

BASE = """
server: { admin_token: adm, fail_mode: open }
pricing:
  source: static
  static:
    claude-opus-5-5: { input: 5, output: 25, cache_read: 0.5, cache_write_5m: 6.25 }
    gpt-6.1-sol: { input: 4, output: 20, cache_read: 0.4 }
defaults: { max_hold: 150s, max_queue: 8, hold_cap_cached: 120s }
upstreams:
  - name: r1
    base_url: http://r1:20128
    api_key: k1
    dashboard: { port: 8101, openers: [{ name: team-a, token: op1 }] }
  - { name: r2, base_url: "http://r2:20128", api_key: k2, dashboard: { port: 8102, allow_persistent: false } }
pools:
  - name: a/claude
    kind: claude
    upstream: r1
    keys: [lq-a-claude]
    rules: [{ kind: hourly_cap, limit: 20, on_exceed: hold, release: fifo }]
  - { name: a/codex, kind: codex, upstream: r1, keys: [lq-a-codex], rules: [] }
  - { name: b/claude, kind: claude, upstream: r2, keys: [lq-b-claude], rules: [] }
"""


def load(text=BASE, **replace):
    t = textwrap.dedent(text)
    for k, v in replace.items():
        t = t.replace(k, v)
    s, errors, warnings = settings_mod.load(text=t)
    if errors:
        raise AssertionError(errors)
    return s


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
