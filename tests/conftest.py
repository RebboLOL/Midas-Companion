import gzip
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


collect_ignore_glob = ["*_backup_*"]


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    """Tests never reach the real server: only loopback URLs may be opened."""
    import urllib.parse
    import uploader as U
    real_open = U._opener.open

    def guarded(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = urllib.parse.urlsplit(url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"test tried to reach {url}")
        return real_open(req, *a, **kw)

    monkeypatch.setattr(U._opener, "open", guarded)


@pytest.fixture(autouse=True)
def own_appdata(tmp_path, monkeypatch):
    """Every test gets its own APPDATA -- the real state is never touched."""
    import logging
    d = tmp_path / "appdata"
    d.mkdir()
    monkeypatch.setenv("APPDATA", str(d))
    monkeypatch.delenv("MIDAS_UPLINK_HOME", raising=False)      # restored after the test
    monkeypatch.delenv("MIDAS_UPLINK_SECOND_INSTANCE", raising=False)
    monkeypatch.delenv("MIDAS_UPLINK_URL", raising=False)
    yield d
    # hard cleanup: open log files, cached autostart state, module state
    lg = logging.getLogger("midasuplink")
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)
    tray = sys.modules.get("tray")
    if tray is not None:
        tray._task_state.clear()
    os.environ.pop("MIDAS_UPLINK_HOME", None)


class FakeServer:
    """Answers POSTs with the next status of `script` (last one repeats)."""

    def __init__(self):
        self.script = [(201, {"status": "ok"})]
        self.bodies = []
        self.requests = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                srv.bodies.append(json.loads(raw))
                srv.requests.append(("POST", self.path))
                entry = srv.script.pop(0) if len(srv.script) > 1 else srv.script[0]
                code, body = entry[0], entry[1]
                headers = entry[2] if len(entry) > 2 else {}
                data = json.dumps(body).encode()
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                srv.requests.append(("GET", self.path))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/snapshots"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()
