"""Keeps the live price table (DESIGN §3.2): periodic sync, last good table kept on failure,
latest snapshot loaded on start."""
import logging
import threading
import time

from .. import db
from ..models import PriceSnapshot
from . import newapi, ninerouter
from .table import PriceTable, complete

STALE_SEC = 24 * 3600
KEEP_SNAPSHOTS = 50
log = logging.getLogger("lingqu")


class Pricing:
    def __init__(self, app, settings, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.Lock()
        self.table = PriceTable()
        self.unpriced = {}          # model -> first seen
        self.last_error = ""
        self.last_attempt = 0.0
        self.session = None
        self.on_unpriced = lambda model, kind: None
        self.configure(settings)

    def configure(self, settings):
        self.settings = settings
        p = settings.pricing
        if p["source"] == "static":
            self.table = PriceTable(p["static"], source="static", synced_at=self.clock())
        if p["source"] == "ninerouter":
            nr = p["ninerouter"]
            base = nr.get("base_url") or settings.upstreams[nr["upstream"]].base_url
            if not self.session or self.session.base_url != base.rstrip("/") or self.session.password != (nr.get("password") or ""):
                self.session = ninerouter.Session(base, nr.get("password") or "", clock=self.clock)

    def load_snapshot(self):
        if self.settings.pricing["source"] == "static":
            return
        with self.app.app_context():
            snap = PriceSnapshot.query.filter_by(source=self.settings.pricing["source"]) \
                .order_by(PriceSnapshot.ts.desc()).first()
        if snap:
            self.table = PriceTable(snap.prices.get("prices"), snap.prices.get("by_provider"),
                                    source=snap.source, synced_at=snap.ts)

    def sync(self):
        p = self.settings.pricing
        self.last_attempt = self.clock()
        if p["source"] == "static":
            return self.table
        try:
            if p["source"] == "newapi":
                table = PriceTable(newapi.fetch(p["newapi"]["base_url"], p["newapi"].get("token") or ""),
                                   source="newapi", synced_at=self.clock())
            else:
                table = PriceTable(by_provider=ninerouter.fetch(self.session), source="ninerouter",
                                   synced_at=self.clock())
        except Exception as e:
            self.last_error = str(e)
            log.warning("price sync failed: %s", e)
            raise
        self.table, self.last_error = table, ""
        with self.app.app_context():
            db.session.add(PriceSnapshot(ts=table.synced_at, source=table.source, prices=table.to_json()))
            old = PriceSnapshot.query.order_by(PriceSnapshot.ts.desc()).offset(KEEP_SNAPSHOTS).all()
            for o in old:
                db.session.delete(o)
            db.session.commit()
        return table

    def due(self):
        return self.clock() - self.last_attempt >= self.settings.pricing["refresh"]

    def stale(self):
        t = self.table.synced_at
        return self.settings.pricing["source"] != "static" and (t is None or self.clock() - t > STALE_SEC)

    def price(self, model, kind):
        """(price, ref) for a model; falls back to the kind's default price and records it."""
        p = self.settings.pricing
        price, name = self.table.lookup(model, kind, p["strip_prefixes"], p["overrides"])
        if price is not None:
            return price, f"{self.table.source}:{name}"
        with self.lock:
            new = model not in self.unpriced
            self.unpriced.setdefault(model, self.clock())
        if new:
            self.on_unpriced(model, kind)
        return complete(self.settings.default_price[kind]), f"default:{kind}"
