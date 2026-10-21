"""Whether each 9router dashboard is reachable: closed (default), temporary until a time, or
persistent until closed. Kept in the database so a restart keeps it (DESIGN §2.4)."""
import threading
import time

from .. import db
from ..models import DashboardExposure, ExposureEvent


class Exposure:
    def __init__(self, app, settings, clock=time.time, on_change=None):
        self.app, self.clock = app, clock
        self.lock = threading.Lock()
        self.state = {}  # upstream -> {"state", "until", "opened_by", "opened_at"}
        self.on_change = on_change or (lambda **kw: None)
        self.configure(settings)

    def configure(self, settings):
        self.settings = settings
        with self.lock:
            for name, u in settings.upstreams.items():
                if u.dashboard:
                    self.state.setdefault(name, {"state": "closed", "until": None, "opened_by": None, "opened_at": None})
            for name in [n for n in self.state if n not in settings.upstreams or not settings.upstreams[n].dashboard]:
                del self.state[name]
            for name, st in self.state.items():
                if st["state"] == "persistent" and not settings.upstreams[name].dashboard.allow_persistent:
                    st.update(state="closed", until=None)

    def load(self):
        with self.app.app_context():
            rows = {r.upstream: r for r in DashboardExposure.query.all()}
        with self.lock:
            for name, st in self.state.items():
                r = rows.get(name)
                if r and r.state in ("temporary", "persistent"):
                    st.update(state=r.state, until=r.until, opened_by=r.opened_by, opened_at=r.opened_at)
        self.expire()

    def manages(self, token):
        """Upstreams a token may open or close: all for the admin token, else those listing it."""
        if not token:
            return []
        if self.settings.admin_token and token == self.settings.admin_token:
            return sorted(self.state)
        return sorted(n for n in self.state
                      if any(t == token for _, t in self.settings.upstreams[n].dashboard.openers))

    def actor(self, token):
        if self.settings.admin_token and token == self.settings.admin_token:
            return "admin"
        for u in self.settings.upstreams.values():
            for name, t in (u.dashboard.openers if u.dashboard else []):
                if t == token:
                    return name
        return "?"

    def is_open(self, upstream):
        with self.lock:
            st = self.state.get(upstream)
            if not st:
                return False
            if st["state"] == "temporary" and st["until"] and self.clock() >= st["until"]:
                return False
            return st["state"] in ("temporary", "persistent")

    def get(self, upstream):
        with self.lock:
            return dict(self.state.get(upstream) or {})

    def _save(self, upstream, action, actor, source):
        st = self.state[upstream]
        now = self.clock()
        with self.app.app_context():
            row = db.session.get(DashboardExposure, upstream) or DashboardExposure(upstream=upstream)
            row.state, row.until, row.opened_by, row.opened_at = st["state"], st["until"], st["opened_by"], st["opened_at"]
            db.session.add(row)
            db.session.add(ExposureEvent(ts=now, upstream=upstream, action=action, state=st["state"],
                                         until=st["until"], actor=actor, source=source))
            db.session.commit()
        self.on_change(upstream=upstream, action=action, state=st["state"], until=st["until"], actor=actor,
                       source=source)

    def open(self, upstream, actor, seconds=None, persistent=False, source="api"):
        d = self.settings.upstreams[upstream].dashboard
        now = self.clock()
        with self.lock:
            st = self.state[upstream]
            was_open = st["state"] == "persistent" or (st["state"] == "temporary" and (st["until"] or 0) > now)
            if persistent:
                if not d.allow_persistent:
                    raise PermissionError(f"{upstream}: keeping the dashboard open is not allowed")
                st.update(state="persistent", until=None)
            else:
                seconds = d.default_ttl if seconds is None else seconds
                if seconds <= 0:
                    raise ValueError("duration must be positive")
                st.update(state="temporary", until=now + min(seconds, d.max_ttl))
            st.update(opened_by=actor, opened_at=now)
            action = "extend" if was_open and not persistent else "open"
        self._save(upstream, action, actor, source)
        return self.get(upstream)

    def close(self, upstream, actor, source="api", action="close"):
        with self.lock:
            st = self.state[upstream]
            if st["state"] == "closed":
                return self.get(upstream)
            st.update(state="closed", until=None)
        self._save(upstream, action, actor, source)
        return self.get(upstream)

    def expire(self):
        now = self.clock()
        with self.lock:
            due = [n for n, st in self.state.items()
                   if st["state"] == "temporary" and st["until"] and now >= st["until"]]
        for name in due:
            self.close(name, "timer", source="timer", action="expire")

    def snapshot(self):
        with self.lock:
            return {n: dict(st) for n, st in self.state.items()}
