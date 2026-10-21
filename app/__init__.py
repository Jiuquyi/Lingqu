from flask import Flask
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from config import config

db = SQLAlchemy()
migrate = Migrate()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(conn, _record):
    # WAL lets /stats read while the ledger thread writes.
    if type(conn).__module__.startswith("sqlite3"):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def create_app(config_name, settings=None):
    """settings: an already loaded app.settings.Settings (tests); else read LINGQU_CONFIG."""
    from . import settings as settings_mod
    from .runtime import Runtime

    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    if settings is None:
        settings, errors, warnings = settings_mod.load(app.config["LINGQU_CONFIG"])
        for w in warnings:
            app.logger.warning("config: %s", w)
        if errors:
            raise SystemExit("lingqu: invalid config %s:\n  " % app.config["LINGQU_CONFIG"] + "\n  ".join(errors))
    if settings.dsn:
        app.config["SQLALCHEMY_DATABASE_URI"] = settings.dsn

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    from .proxy import proxy as proxy_blueprint
    from .status import status as status_blueprint
    app.register_blueprint(proxy_blueprint)
    app.register_blueprint(status_blueprint)

    app.extensions["lingqu"] = Runtime(app, settings)
    return app
