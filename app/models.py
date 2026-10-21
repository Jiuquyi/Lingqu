"""Runtime data only (DESIGN §9): pools, keys, rules and prices come from the config file."""
from . import db


class Ledger(db.Model):
    """One row per request."""
    __tablename__ = "ledger"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Float, index=True, nullable=False)          # request arrival
    pool = db.Column(db.String(128), index=True, nullable=False)
    key_ref = db.Column(db.String(16))                             # last characters of the lq- key
    session_tag = db.Column(db.String(64), index=True)
    user_tag = db.Column(db.String(128))
    protocol = db.Column(db.String(32))
    endpoint = db.Column(db.String(128))
    model_req = db.Column(db.String(128))
    model_actual = db.Column(db.String(128))
    stream = db.Column(db.Boolean)
    status = db.Column(db.Integer)
    error_class = db.Column(db.String(32))
    input_tokens = db.Column(db.Integer, default=0)
    cache_read_tokens = db.Column(db.Integer, default=0)
    cache_write_5m_tokens = db.Column(db.Integer, default=0)
    cache_write_1h_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    reasoning_tokens = db.Column(db.Integer, default=0)
    usage_source = db.Column(db.String(16))                        # upstream | estimated | none
    eusd = db.Column(db.Float, default=0.0)
    eusd_input = db.Column(db.Float, default=0.0)                  # part counted at message_start
    counted_at = db.Column(db.Float)                               # when eusd_input entered the window
    price_ref = db.Column(db.String(160))
    held_ms = db.Column(db.Integer, default=0)
    ttfb_ms = db.Column(db.Integer)
    duration_ms = db.Column(db.Integer)
    gate_result = db.Column(db.String(32))
    cache_state = db.Column(db.String(8))                          # warm | cold | none, from the session's last start


class GateEvent(db.Model):
    __tablename__ = "gate_events"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Float, index=True, nullable=False)
    pool = db.Column(db.String(128), index=True)
    event = db.Column(db.String(32))
    ratio = db.Column(db.Float)
    spent_1h = db.Column(db.Float)
    limit_value = db.Column(db.Float)
    step = db.Column(db.JSON)
    detail = db.Column(db.JSON)


class PriceSnapshot(db.Model):
    __tablename__ = "price_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Float, index=True, nullable=False)
    source = db.Column(db.String(16))
    prices = db.Column(db.JSON)


class CounterSnapshot(db.Model):
    __tablename__ = "counter_snapshots"
    pool = db.Column(db.String(128), primary_key=True)
    scope_key = db.Column(db.String(160), primary_key=True)
    buckets = db.Column(db.JSON)                                   # {minute index: eUSD}
    saved_at = db.Column(db.Float)


class DashboardExposure(db.Model):
    __tablename__ = "dashboard_exposure"
    upstream = db.Column(db.String(128), primary_key=True)
    state = db.Column(db.String(16), default="closed")             # closed | temporary | persistent
    until = db.Column(db.Float)
    opened_by = db.Column(db.String(128))
    opened_at = db.Column(db.Float)


class ExposureEvent(db.Model):
    __tablename__ = "exposure_events"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Float, index=True, nullable=False)
    upstream = db.Column(db.String(128), index=True)
    action = db.Column(db.String(16))                              # open | extend | close | expire
    state = db.Column(db.String(16))
    until = db.Column(db.Float)
    actor = db.Column(db.String(128))
    source = db.Column(db.String(16))                              # page | api | cli | timer
