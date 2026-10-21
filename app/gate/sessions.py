"""Sessions and their prompt caches (DESIGN §4.4).

Claude Code puts the session id in metadata.user_id; Codex sends a prompt_cache_key per
conversation. The gate remembers when each session last sent a request upstream: the prompt
cache lives `cache_ttl` (5 minutes) from then, so a request that comes later, or a session's
first, rebuilds the cache however long it waits (cold), and one that comes sooner hits it as
long as it leaves before the cache expires (warm). The session-aware queue (§4.5) uses this."""
import collections
import json
import re

_SESSION = re.compile(r"session_([0-9a-fA-F-]{8,})")

CACHE_MARGIN = 15       # a warm request leaves at the latest this many seconds before its cache expires
MAX_SESSIONS = 20000    # sessions remembered per instance; the oldest are forgotten first

WARM, COLD, NONE = "warm", "cold", "none"


def session_tag(user_id):
    """The session part of a metadata.user_id; '' when there is none."""
    if not isinstance(user_id, str) or not user_id:
        return ""
    if user_id.lstrip().startswith("{"):
        # Newer Claude Code versions send a JSON object as a string.
        try:
            obj = json.loads(user_id)
            return str(obj.get("session_id") or obj.get("sessionId") or "")[:64]
        except (ValueError, AttributeError):
            return ""
    m = _SESSION.search(user_id)
    return m.group(1)[:64] if m else ""


def request_session(fields):
    """The session a request belongs to: Claude Code's session id, else the prompt_cache_key."""
    return session_tag(fields.get("user_id")) or (fields.get("prompt_cache_key") or "")[:64]


class SessionClock:
    """When each (pool, session) last sent a request upstream. Not thread-safe: the gate holds
    its lock around every call."""

    def __init__(self, keep=MAX_SESSIONS):
        self.keep = keep
        self.last = collections.OrderedDict()   # (pool, session) -> start time, oldest first

    def since(self, pool, session, now):
        """Seconds since the session's last start; None for a session not seen (or no session)."""
        t = self.last.get((pool, session)) if session else None
        return None if t is None else max(0.0, now - t)

    def classify(self, pool, session, now, ttl):
        """(warm | cold | none, seconds since the last start or None)."""
        if not session:
            return NONE, None
        since = self.since(pool, session, now)
        return (COLD if since is None or since > ttl else WARM), since

    def started(self, pool, session, t):
        if not session:
            return
        key = (pool, session)
        if self.last.get(key, t) > t:       # restoring out of order: keep the later start
            return
        self.last[key] = t
        self.last.move_to_end(key)
        while len(self.last) > self.keep:
            self.last.popitem(last=False)

    def active(self, pool, now, ttl):
        """Sessions of a pool whose cache is still warm."""
        return sum(1 for (p, _), t in self.last.items() if p == pool and now - t <= ttl)
