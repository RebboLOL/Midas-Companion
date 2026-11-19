# -*- coding: utf-8 -*-
"""
Midas Companion window: a small HTTP server on 127.0.0.1 (random port) that
serves the app page and a JSON API. The page is shown in an Edge app window
(msedge --app=<url>), otherwise in the default browser.

Protection (other local web pages and services, DNS rebinding, CSRF):
- bound to 127.0.0.1 only, random port
- everything lives under a secret path /s/<random>/ (404 elsewhere)
- Host header must be 127.0.0.1:<port> or localhost:<port>
- the URL handed to the browser carries a one-time ticket (?ticket=..., valid
  2 minutes, single use). It is exchanged for a session cookie (random, HttpOnly,
  SameSite=Strict, Path=/s/<random>/) and the URL is cleaned by a redirect; every
  request needs that cookie. Cookies are not bound to a port, hence the secret
  path and a cookie that alone does not allow any action
- actions are POST with a separate page token in X-Midas-Token (only inside the
  page, never in a cookie or URL), a JSON body (<= 4 KB) and, if sent, an Origin
  equal to the page origin; Sec-Fetch-Site must be same-origin/none
- the request body is always read before an answer (no TCP reset on Windows)
- no CORS headers, OPTIONS is refused, CSP allows only own files

Only the standard library is used.
"""
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BODY_MAX = 4096
DRAIN_MAX = 1024 * 1024                 # bigger bodies are not read: connection is closed
TICKET_TTL = 120
STATIC = {"app.css": ("app.css", "text/css; charset=utf-8"),
          "app.js": ("app.js", "text/javascript; charset=utf-8"),
          "logo.png": ("logo.png", "image/png")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
ACTIONS = ("send", "autostart", "prices", "folder", "pick", "open", "market_add", "market_del")


def ui_dir():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "ui"


def _eq(a, b):
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


class UIServer:
    """backend: object with status() -> dict, log_since(n) -> (last_seq, [lines]) and
    action(name, data) -> dict (see tray.App)."""

    def __init__(self, backend, lang="en", static_dir=None):
        self.backend = backend
        self.lang = lang if lang in ("de", "en") else "en"
        self.static_dir = Path(static_dir) if static_dir else ui_dir()
        self.prefix = f"/s/{secrets.token_urlsafe(18)}/"
        self.session = secrets.token_urlsafe(32)      # cookie value
        self.page_token = secrets.token_urlsafe(32)   # X-Midas-Token, only inside the page
        self._tickets = {}
        self._tlock = threading.Lock()
        srv = self

        class Handler(_Handler):
            server_ref = srv

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.cookie = f"mc{self.port}"
        self.thread = None

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def new_url(self):
        """URL for one window: secret path + one-time ticket."""
        t = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._tlock:
            self._tickets = {k: v for k, v in self._tickets.items() if v > now}
            self._tickets[t] = now + TICKET_TTL
        return f"{self.origin}{self.prefix}?ticket={t}"

    def take_ticket(self, t):
        now = time.monotonic()
        with self._tlock:
            for k, exp in list(self._tickets.items()):
                if _eq(k, t):
                    del self._tickets[k]
                    return exp > now
        return False

    def hosts(self):
        return (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ui", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def page(self):
        html = (self.static_dir / "index.html").read_text(encoding="utf-8")
        return (html.replace("{{LANG}}", self.lang).replace("{{TOKEN}}", self.page_token)
                .encode("utf-8"))


class _Handler(BaseHTTPRequestHandler):
    server_ref = None                      # set per server
    timeout = 15                           # idle connections do not pile up
    protocol_version = "HTTP/1.0"          # one request per connection
    server_version = "MidasCompanion"
    sys_version = ""

    def log_message(self, *a):             # no access log (URLs carry tickets)
        pass

    # ---- helpers --------------------------------------------------------- #
    def _read_body(self):
        """Read the whole request body first (an unread body makes Windows reset the
        connection before the client sees the answer). -> bytes, or None when it is
        too large / malformed (then the connection is closed after the answer)."""
        raw = self.headers.get("Content-Length")
        try:
            n = int(raw) if raw else 0
        except ValueError:
            self.close_connection = True
            return None
        if n < 0 or n > DRAIN_MAX:
            self.close_connection = True
            return None
        data, left = [], n
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            data.append(chunk)
            left -= len(chunk)
        body = b"".join(data)
        return body if n <= BODY_MAX else None

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _deny(self, code=403, why="forbidden"):
        self._json(code, {"error": why})

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in self.server_ref.hosts()

    def _fetch_site_ok(self):
        site = self.headers.get("Sec-Fetch-Site")
        return site is None or site in ("same-origin", "none")

    def _cookie_ok(self):
        srv = self.server_ref
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == srv.cookie and _eq(v, srv.session):
                return True
        return False

    def _sub(self):
        """Path below the secret prefix ('' = page) or None."""
        u = urllib.parse.urlsplit(self.path)
        p = self.server_ref.prefix
        if not u.path.startswith(p):
            return None, u
        return u.path[len(p):], u

    def _common_checks(self):
        if not self._host_ok():
            self._deny(421, "host")
            return False
        if not self._fetch_site_ok():
            self._deny(403, "site")
            return False
        return True

    # ---- GET ------------------------------------------------------------- #
    def do_GET(self):
        srv = self.server_ref
        self._read_body()
        if not self._common_checks():
            return
        sub, u = self._sub()
        if sub is None:
            return self._deny(404, "not found")
        if sub == "" and u.query:
            t = urllib.parse.parse_qs(u.query).get("ticket", [""])[0]
            if t and srv.take_ticket(t):
                cookie = f"{srv.cookie}={srv.session}; Path={srv.prefix}; HttpOnly; SameSite=Strict"
                return self._send(303, b"", "text/plain", {"Location": srv.prefix, "Set-Cookie": cookie})
            if self._cookie_ok():               # reload of a used ticket URL: already signed in
                return self._send(303, b"", "text/plain", {"Location": srv.prefix})
            return self._deny(403, "ticket")
        if not self._cookie_ok():
            return self._deny(403, "session")
        try:
            if sub == "":
                return self._send(200, srv.page(), "text/html; charset=utf-8")
            if sub in STATIC:
                name, ctype = STATIC[sub]
                return self._send(200, (srv.static_dir / name).read_bytes(), ctype)
            if sub == "api/status":
                return self._json(200, srv.backend.status())
            if sub == "api/log":
                q = urllib.parse.parse_qs(u.query)
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                last, lines = srv.backend.log_since(after)
                return self._json(200, {"last": last, "lines": lines})
        except OSError:
            return self._deny(500, "io")
        return self._deny(404, "not found")

    do_HEAD = do_GET

    # ---- POST ------------------------------------------------------------ #
    def do_POST(self):
        srv = self.server_ref
        body = self._read_body()
        if not self._common_checks():
            return
        sub, _u = self._sub()
        if sub is None:
            return self._deny(404, "not found")
        if not self._cookie_ok():
            return self._deny(403, "session")
        if not _eq(self.headers.get("X-Midas-Token") or "", srv.page_token):
            return self._deny(403, "token")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://{h}" for h in srv.hosts()):
            return self._deny(403, "origin")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._deny(415, "json")
        if body is None:
            return self._deny(413, "too large")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return self._deny(400, "json")
        if not isinstance(data, dict):
            return self._deny(400, "json")
        name = sub[len("api/"):] if sub.startswith("api/") else ""
        if name not in ACTIONS:
            return self._deny(404, "not found")
        try:
            res = srv.backend.action(name, data)
        except Exception as e:  # noqa: BLE001 -- report, keep the server alive
            return self._json(500, {"ok": False, "error": str(e)[:200]})
        return self._json(200, res if isinstance(res, dict) else {"ok": bool(res)})

    def do_OPTIONS(self):                   # no CORS, ever
        self._read_body()
        self._deny(403, "cors")

    def _no(self):
        self._read_body()
        self._deny(405, "method")

    do_PUT = do_DELETE = do_PATCH = _no
