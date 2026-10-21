"""One process, several listening ports (DESIGN §11.2): the port a request arrived on decides
which application sees it, so the proxy port serves only /v1/*, the status port only status and
admin, and each dashboard gate port only its own 9router."""
from werkzeug.exceptions import NotFound

from .dashboard.gateway import DashboardGateway

PROXY_PREFIXES = ("/v1/",)
STATUS_BLOCKED_PREFIXES = ("/v1/",)


class PortDispatcher:
    def __init__(self, app):
        self.app = app
        self.rt = app.extensions["lingqu"]
        self.gateways = {}

    def gateway(self, upstream):
        g = self.gateways.get(upstream)
        if g is None:
            g = self.gateways[upstream] = DashboardGateway(self.rt, upstream)
        return g

    def route(self, environ):
        """'proxy', 'status', ('dashboard', upstream) or None."""
        s = self.rt.settings
        try:
            port = int(environ.get("SERVER_PORT") or 0)
        except ValueError:
            port = 0
        if port == s.proxy_port:
            return "proxy"
        if port == s.status_port:
            return "status"
        host = (environ.get("HTTP_HOST") or "").split(":")[0].lower()
        for u in s.upstreams.values():
            d = u.dashboard
            if not d:
                continue
            if d.host and d.host.lower() == host and (not d.port or d.port == port):
                return ("dashboard", u.name)
        for u in s.upstreams.values():
            d = u.dashboard
            if d and d.port == port and not d.host:
                return ("dashboard", u.name)
        return None

    def __call__(self, environ, start_response):
        where = self.route(environ)
        path = environ.get("PATH_INFO") or "/"
        if where == "proxy":
            if not path.startswith(PROXY_PREFIXES):
                return NotFound()(environ, start_response)
            return self.app.wsgi_app(environ, start_response)
        if where == "status":
            if path.startswith(STATUS_BLOCKED_PREFIXES):
                return NotFound()(environ, start_response)
            return self.app.wsgi_app(environ, start_response)
        if isinstance(where, tuple):
            return self.gateway(where[1])(environ, start_response)
        return NotFound()(environ, start_response)
