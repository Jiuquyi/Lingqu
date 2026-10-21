"""gunicorn settings (DESIGN §11.2): one worker by default. Extra workers require Redis
to coordinate windows, departure times and hold queues. Ports come from lingqu.yaml."""
import os
import signal

worker_class = "gthread"
workers = int(os.environ.get("LINGQU_WORKERS", "1"))
threads = int(os.environ.get("LINGQU_THREADS", "64"))
graceful_timeout = 600
timeout = 0  # gthread heartbeats from its main loop; long streams must not trip it
keepalive = 75
accesslog = "-"
errorlog = "-"


def _binds():
    from app import settings as settings_mod
    path = os.environ.get("LINGQU_CONFIG") or os.path.join(os.path.dirname(__file__), "lingqu.yaml")
    s, errors, _ = settings_mod.load(path)
    if errors:
        raise SystemExit("lingqu: invalid config %s:\n  " % path + "\n  ".join(errors))
    if workers > 1 and not s.redis:
        raise SystemExit("LINGQU_WORKERS > 1 requires redis configuration")
    host = os.environ.get("LINGQU_BIND_HOST", "0.0.0.0")
    return [f"{host}:{p}" for p in [s.proxy_port, s.status_port] + s.dashboard_ports()]


bind = _binds()


def post_worker_init(worker):
    """Start the background threads and make SIGTERM answer held requests with a 503 at once."""
    import atexit
    rt = worker.wsgi.extensions["lingqu"]
    rt.start()
    atexit.register(rt.exit)
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(sig, frame):
        rt.shutdown()
        if callable(previous):
            previous(sig, frame)
    signal.signal(signal.SIGTERM, on_term)
    signal.siginterrupt(signal.SIGTERM, False)
