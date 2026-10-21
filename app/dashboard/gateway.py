"""Reverse proxy on a dashboard gate port or host (DESIGN §2.4). Plain WSGI, no Flask: nothing
here may add a session cookie, a redirect or a buffering layer."""
import html

import httpx

from ..proxy.forward import HOP_BY_HOP, stream_body

# 9router's LLM API prefixes (dashboardGuard.js PUBLIC_PREFIXES): never through this gate.
LLM_PREFIXES = ("/v1", "/v1beta", "/api/v1", "/api/v1beta", "/codex", "/responses")
# Headers a client could use to look local or proxied to 9router; never forwarded.
DROP = HOP_BY_HOP | {"host", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
                     "x-real-ip", "forwarded", "x-9r-real-ip", "x-9r-peer-token", "x-9r-via-proxy",
                     "x-9r-cli-token"}


def is_llm_path(path):
    return any(path == p or path.startswith(p + "/") for p in LLM_PREFIXES)


def _page(status, title, body):
    text = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
            "<body style=\"font-family:sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem\">"
            f"<h1>{html.escape(title)}</h1>{body}</body>").encode()
    return status, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(text))),
                    ("Cache-Control", "no-store")], [text]


class DashboardGateway:
    def __init__(self, runtime, upstream):
        self.rt, self.upstream = runtime, upstream

    def __call__(self, environ, start_response):
        status, headers, body = self.handle(environ)
        start_response(status, headers)
        return body

    def handle(self, environ):
        rt = self.rt
        u = rt.settings.upstreams.get(self.upstream)
        path = environ.get("PATH_INFO") or "/"
        if u is None or not u.dashboard:
            return _page("404 Not Found", "Not found", "")
        if is_llm_path(path):
            return _page("404 Not Found", "Not found", "<p>LLM API paths are not served on the dashboard gate.</p>")
        if not rt.exposure.is_open(self.upstream):
            where = f"http://{environ.get('HTTP_HOST', '').split(':')[0]}:{rt.settings.status_port}/dashboards"
            return _page("503 Service Unavailable", "Dashboard 入口已关闭",
                         f"<p>{html.escape(self.upstream)} 的 9router Dashboard 入口目前是关闭的。</p>"
                         f"<p>请到 <a href=\"{html.escape(where)}\">{html.escape(where)}</a> 用你的 token 打开。</p>")
        return self.forward(environ, u, path)

    def forward(self, environ, u, path):
        query = environ.get("QUERY_STRING")
        url = u.base_url + path + (f"?{query}" if query else "")
        headers = []
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                name = k[5:].replace("_", "-").lower()
                if name not in DROP:
                    headers.append((name, v))
        for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(k):
                headers.append((k.replace("_", "-").lower(), environ[k]))
        length = int(environ.get("CONTENT_LENGTH") or 0)
        data = environ["wsgi.input"].read(length) if length else b""
        client = self.rt.client(u.name)
        try:
            req = client.build_request(environ["REQUEST_METHOD"], url, headers=headers, content=data)
            resp = client.send(req, stream=True)
        except httpx.HTTPError as e:
            return _page("502 Bad Gateway", "9router unreachable", f"<p>{html.escape(str(e))}</p>")
        out = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in HOP_BY_HOP]
        return f"{resp.status_code} {resp.reason_phrase}", out, stream_body(resp)
