import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
    SESSION_COOKIE_NAME = "lingqu_console_session"
    SESSION_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_SECURE = os.environ.get("LINGQU_SECURE_COOKIES", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 8 * 3600
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 1M-context requests carry several MB of JSON; align with New API's MAX_REQUEST_BODY_MB.
    MAX_CONTENT_LENGTH = int(os.environ.get("LINGQU_MAX_BODY_MB", "128")) * 1024 * 1024
    LINGQU_CONFIG = os.environ.get("LINGQU_CONFIG") or os.path.join(basedir, "lingqu.yaml")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or \
        "sqlite:///" + os.path.join(basedir, "lingqu.db")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    @staticmethod
    def init_app(app):
        pass


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL") or "sqlite://"


class ProductionConfig(Config):
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        import logging
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        for name in ("lingqu", app.logger.name):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            logger.propagate = False


config = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
