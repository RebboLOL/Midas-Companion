"""Downlink (SPEC 9.3/9.4): server prices -> Midas_Uplink\\Prices.lua.
Every test uses its own APPDATA (conftest) and a temporary game folder."""
import gzip
import hashlib
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import uploader as U

lupa = pytest.importorskip("lupa")

NOW = 1_800_000_000
DAY = 86400


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def seen_lua(entries):
    rows = []
    for e in entries:
        key = "|".join(e[k] for k in ("client", "region", "realm", "faction"))
        fields = "".join(f'["{k}"] = {lua_str(v)}, ' for k, v in e.items() if isinstance(v, str))
        rows.append(f'[{lua_str(key)}] = {{ {fields}["last"] = {e["last"]}, }},\n')
    return '["seen"] = {\n' + "".join(rows) + "},\n"


def make_game(root, flavor="_forever_", seen=(), addon=True, enabled=False, account="ACC1"):
    base = Path(root) / flavor
    sv = base / "WTF" / "Account" / account / "SavedVariables"
    sv.mkdir(parents=True, exist_ok=True)
    text = ("MidasUplinkDB = {\n" + f'["enabled"] = {"true" if enabled else "false"},\n'
            '["format"] = 1,\n["queue"] = {},\n' + seen_lua(seen) + "}\n")
    (sv / U.SV_NAME).write_bytes(text.encode("utf-8"))
    addon_dir = base / "Interface" / "AddOns" / "Midas_Uplink"
    if addon:
        addon_dir.mkdir(parents=True, exist_ok=True)
        (addon_dir / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    return addon_dir


def mk(realm, faction="Alliance", client="forever", region="EU", ruleset="PvE", last=NOW - 3600):
    d = {"client": client, "region": region, "realm": realm, "faction": faction, "last": last}
    if ruleset:
        d["ruleset"] = ruleset
    return d


def export(market, ts=NOW - 600, items=None):
    return {"v": 1, "market": market, "ts": ts, "day0": 1_798_848_000, "days": 14,
            "items": items if items is not None else {"2589": [[0, 1234, 56], [1, 1200, 60]],
                                                      "765": [[13, 5, 0]]}}


def lua_load(path):
    """Run the file in a real Lua 5.1 interpreter (WoW uses 5.1)."""
    try:
        from lupa import lua51 as L
    except ImportError:                                  # pragma: no cover
        L = lupa
    rt = L.LuaRuntime(unpack_returned_tuples=True, encoding="utf-8")
    rt.execute(Path(path).read_text(encoding="utf-8"))
    return rt.globals().MidasUplinkPrices


def lua_markets(t):
    out = []
    for i in range(1, len(t.markets) + 1):
        m = t.markets[i]
        match = [{k: m.match[j][k] for k in ("client", "region", "realm", "faction")}
                 for j in range(1, len(m.match) + 1)]
        items = {k: v for k, v in m["items"].items()}       # m.items is the Python method
        out.append({"market": m.market, "ts": m.ts, "day0": m.day0, "match": match, "items": items})
    return out


def tree(root):
    """{relative path: (size, mtime_ns)} of every file in the game folder."""
    out = {}
    for p in Path(root).rglob("*"):
        if p.is_file():
            s = p.stat()
            out[str(p.relative_to(root))] = (s.st_size, s.st_mtime_ns)
    return out


class ExportServer:
    """Fake GET /api/v1/export. `markets` maps (client, region, realm, faction)
    to an export body; strong ETag; If-None-Match -> 304; unknown -> 404."""

    def __init__(self):
        self.markets = {}
        self.fail = None          # forced status code
        self.raw = None           # forced raw body (200)
        self.gzip = False
        self.codes = {}           # realm -> forced status code
        self.drip = False
        self.drip_headers = False
        self.fail_headers = {}
        self.reject_days = False
        self.requests = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
                srv.requests.append({"path": u.path, "q": q,
                                     "inm": self.headers.get("If-None-Match"),
                                     "ae": self.headers.get("Accept-Encoding")})
                if srv.drip_headers:
                    return self.drip_header_reply()
                if srv.reject_days and "days" in q:          # server before 17.11.2026: days 14 only
                    return self.reply(422, b'{"detail":"days"}')
                if srv.fail:
                    return self.reply(srv.fail, b'{"detail":"x"}', srv.fail_headers)
                if q.get("realm") in srv.codes:
                    return self.reply(srv.codes[q["realm"]], b'{"detail":"x"}')
                if srv.drip:
                    return self.drip_reply()
                if u.path != "/api/v1/export":
                    return self.reply(404, b'{"detail":"Not Found"}')
                if srv.raw is not None:
                    return self.reply(200, srv.raw)
                body = srv.markets.get(tuple(q.get(k, "") for k in ("client", "region", "realm", "faction")))
                if body is None:
                    return self.reply(404, b'{"detail":"unknown market"}')
                data = json.dumps(body).encode()
                etag = '"' + hashlib.sha1(data).hexdigest() + '"'
                if self.headers.get("If-None-Match") == etag:
                    return self.reply(304, b"", {"ETag": etag})
                return self.reply(200, data, {"ETag": etag})

            def drip_header_reply(self):
                """Status line and headers one byte every 0.1 s (no body yet)."""
                head = b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 300 + b"\r\n\r\n"
                try:
                    for i in range(len(head)):
                        self.wfile.write(head[i:i + 1])
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass

            def drip_reply(self):
                """Valid headers, then one byte every 0.1 s: never finishes in time."""
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    for _ in range(200):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass

            def reply(self, code, data, headers=None):
                if srv.gzip and data and code == 200:
                    data = gzip.compress(data)
                    headers = dict(headers or {}, **{"Content-Encoding": "gzip"})
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if code != 304:
                    self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/snapshots"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def xs():
    s = ExportServer()
    yield s
    s.close()


def logger():
    lines = []
    return lines, lines.append


def run(roots, url, st=None, now=NOW):
    st = st if st is not None else U.load_state()
    lines, log = logger()
    res, summary = U.run_prices(roots, url, st, log, now=now)
    return res, st, lines


# --------------------------------------------------------------------------- #
# URL, quoting, merge
# --------------------------------------------------------------------------- #
def test_export_url_from_upload_url():
    assert U.export_url("https://midasmarket.app/api/v1/snapshots") == "https://midasmarket.app/api/v1/export"
    assert U.export_url("http://127.0.0.1:8000/api/v1/snapshots/") == "http://127.0.0.1:8000/api/v1/export"
    assert U.export_url("http://host:1") == "http://host:1/api/v1/export"
    assert U.export_url("http://host/other") is None
    assert U.export_url("unused") is None


def test_lua_quote_roundtrip_with_real_lua(tmp_path):
    nasty = 'Realm "Q" \\ back\\\\slash\nnext \x01ctrl \x7f end'
    f = tmp_path / "q.lua"
    f.write_text(f"X = {U.lua_quote(nasty)}\n", encoding="utf-8")
    rt = lupa.LuaRuntime(encoding="utf-8")
    rt.execute(f.read_text(encoding="utf-8"))
    assert rt.globals().X == 'Realm "Q" \\ back\\\\slash\nnext ctrl  end'


def test_merge_match_lists():
    a, b, c = mk("CAMWOW EU5 Realm 7 PvE"), mk("CAMWOW EU5 Realm 3 PvE"), mk("Other", faction="Horde")
    pve = U.clean_export(export("forever/EU/pve/alliance"))
    newer = dict(pve, ts=pve["ts"] + 60)
    horde = U.clean_export(export("forever/EU/pve/horde"))
    out = U.merge_markets([(a, pve), (c, horde), (b, newer), (a, pve)])
    assert [g["market"] for g in out] == ["forever/EU/pve/alliance", "forever/EU/pve/horde"]
    assert out[0]["ts"] == newer["ts"]
    assert out[0]["match"] == [{k: m[k] for k in U.SEEN_FIELDS} for m in (b, a)]     # sorted, no duplicates
    assert len(out[1]["match"]) == 1


def test_clean_export_drops_bad_points():
    d = U.clean_export(export("m", items={"1": [[0, 5, 1], [0, 3, 9], [1, 0, 1], [30, 5, 1], [2, 5, -1],
                                                [3, 5.0, 1], [4, True, 1]],
                                          "x": [[0, 1, 1]], "0": [[0, 1, 1]], "2": [], "3": "no"}))
    assert d["items"] == {"1": "0:3:9"}
    assert U.clean_export({"v": 2}) is None
    assert U.clean_export(dict(export("m"), ts="1")) is None


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #
def test_writes_prices_lua_with_real_lua_parser(tmp_path, xs):
    root = tmp_path / "wow"
    r7, r3 = mk("CAMWOW EU5 Realm 7 PvE"), mk("CAMWOW EU5 Realm 3 PvE")
    old = mk("Gone Realm", last=NOW - 31 * DAY)
    era = mk('Q"uo\\te Realm', faction="Horde", client="era", ruleset=None)
    addon = make_game(root, seen=[r7, r3, old, era])
    xs.markets[U.raw_key(r7)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(r3)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(era)] = export('era/EU/quo-te-realm/horde', items={"4": [[2, 77, 3]]})
    res, st, lines = run([root], xs.url)
    assert res == U.PASS_OK and st["prices_last"] == NOW
    # requests: only the three markets of the last 30 days, raw fields as query
    qs = sorted((r["q"]["realm"], r["q"].get("ruleset")) for r in xs.requests)
    assert qs == [("CAMWOW EU5 Realm 3 PvE", "PvE"), ("CAMWOW EU5 Realm 7 PvE", "PvE"), ('Q"uo\\te Realm', None)]
    assert all(r["path"] == "/api/v1/export" and set(r["q"]) <= {"client", "region", "realm", "faction", "ruleset", "days"}
               and r["q"]["days"] == "90" for r in xs.requests)
    t = lua_load(addon / "Prices.lua")
    assert t.v == 1 and t.written == NOW
    got = lua_markets(t)
    assert got[0]["market"] == "era/EU/quo-te-realm/horde"
    assert got[0]["match"] == [{"client": "era", "region": "EU", "realm": 'Q"uo\\te Realm', "faction": "Horde"}]
    assert got[0]["items"] == {4: "2:77:3"}
    assert got[1]["market"] == "forever/EU/pve/alliance" and got[1]["day0"] == 1_798_848_000
    assert [m["realm"] for m in got[1]["match"]] == ["CAMWOW EU5 Realm 3 PvE", "CAMWOW EU5 Realm 7 PvE"]
    assert got[1]["items"] == {765: "13:5:0", 2589: "0:1234:56;1:1200:60"}
    text = (addon / "Prices.lua").read_text(encoding="utf-8")
    assert text.startswith(f'MidasUplinkPrices = {{ v = 1, app = "{U.VERSION}", written = 1800000000, '
                           'checked = 1800000000, markets = {')
    assert 'match = { { client="forever", region="EU", realm="CAMWOW EU5 Realm 3 PvE", faction="Alliance" }, ' in text
    assert '[2589] = "0:1234:56;1:1200:60",' in text
    assert 'realm="Q\\"uo\\\\te Realm"' in text
    assert any("wrote" in m for m in lines)
    assert sorted(p.name for p in addon.iterdir()) == ["Prices.lua"]          # no tmp left


def test_seen_is_used_without_upload_opt_in(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r], enabled=False)
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    assert run([root], xs.url)[0] == U.PASS_OK
    assert lua_markets(lua_load(addon / "Prices.lua"))[0]["market"] == "forever/EU/pve/alliance"


def test_no_addon_folder_no_request_no_write(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    make_game(root, flavor="_classic_era_", seen=[r], addon=False)
    xs.markets[U.raw_key(r)] = export("era/EU/realm-a/alliance")
    before = tree(root)
    res, st, lines = run([root], xs.url)
    assert res == U.PASS_OK and xs.requests == []
    assert tree(root) == before
    assert not (root / "_classic_era_" / "Interface").exists()


def test_only_flavors_with_addon_and_only_own_folder(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, flavor="_forever_", seen=[r])
    make_game(root, flavor="_anniversary_", seen=[r], addon=False)
    make_game(root, flavor="_classic_era_ptr_", seen=[r])        # not a price flavor
    other = root / "_forever_" / "Interface" / "AddOns" / "Midas"
    other.mkdir(parents=True)
    (other / "Midas.toc").write_text("x", encoding="ascii")
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    before = tree(root)
    run([root], xs.url)
    after = tree(root)
    changed = {k for k in after if before.get(k) != after[k]} | (set(before) - set(after))
    assert changed == {str(addon.relative_to(root) / "Prices.lua")}
    assert len(xs.requests) == 1                                  # same market fetched once


def test_unchanged_data_no_write_and_304(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    first = (addon / "Prices.lua").stat().st_mtime_ns
    assert len(st["prices_etag"]) == 1
    writes = []
    real_replace = os.replace

    def spy(a, b):
        if Path(b).name == "Prices.lua":
            writes.append(b)
        return real_replace(a, b)

    monkeypatch.setattr(U.os, "replace", spy)
    res, st, _ = run([root], xs.url, st, now=NOW + 1800)              # 304 path
    assert res == U.PASS_OK and xs.requests[-1]["inm"] == next(iter(st["prices_etag"].values()))
    assert writes == [] and (addon / "Prices.lua").stat().st_mtime_ns == first
    assert not list(addon.glob("*.tmp"))
    # 200 with identical data but a fresh state (no ETag): still no write
    st2 = U.load_state()
    st2["prices_etag"] = {}
    run([root], xs.url, st2, now=NOW + 3600)
    assert xs.requests[-1]["inm"] is None and writes == []
    # changed data -> written again
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance", ts=NOW, items={"1": [[0, 9, 9]]})
    run([root], xs.url, st2, now=NOW + 5400)
    assert len(writes) == 1
    assert lua_load(addon / "Prices.lua").written == NOW + 5400


def test_304_uses_cache_and_merges(tmp_path, xs):
    root = tmp_path / "wow"
    a, b = mk("Realm A"), mk("Realm B", faction="Horde")
    addon = make_game(root, seen=[a])
    xs.markets[U.raw_key(a)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(b)] = export("forever/EU/pve/horde")
    st = U.load_state()
    run([root], xs.url, st)
    make_game(root, seen=[a, b])                      # second market appears; A answers 304
    run([root], xs.url, st, now=NOW + 100)
    assert [r["inm"] is not None for r in xs.requests] == [False, True, False]
    assert [g["market"] for g in lua_markets(lua_load(addon / "Prices.lua"))] == \
        ["forever/EU/pve/alliance", "forever/EU/pve/horde"]


def test_addon_update_placeholder_rewritten_on_304(tmp_path, xs):
    """A CurseForge/zip update puts the placeholder back (or the file is gone):
    the next pass rewrites it from the cached export even though the server
    answers 304."""
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    good = lua_markets(lua_load(addon / "Prices.lua"))
    (addon / "Prices.lua").write_text("-- shipped copy\nMidasUplinkPrices = nil\n", encoding="ascii")
    res, st, _ = run([root], xs.url, st, now=NOW + 1800)
    assert res == U.PASS_OK and xs.requests[-1]["inm"]          # answered 304
    assert lua_markets(lua_load(addon / "Prices.lua")) == good
    (addon / "Prices.lua").unlink()
    run([root], xs.url, st, now=NOW + 3600)
    assert lua_markets(lua_load(addon / "Prices.lua")) == good
    # 404 now (market dropped by the server): last known data still restores the file
    xs.markets.clear()
    (addon / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    run([root], xs.url, st, now=NOW + 5400)
    assert lua_markets(lua_load(addon / "Prices.lua")) == good


@pytest.mark.parametrize("code", [404, 500, 503, 429])
def test_errors_keep_old_file(tmp_path, xs, code):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    before = (addon / "Prices.lua").read_bytes()
    mtime = (addon / "Prices.lua").stat().st_mtime_ns
    xs.fail = code
    res, st, lines = run([root], xs.url, st, now=NOW + 1800)
    assert (addon / "Prices.lua").read_bytes() == before
    assert (addon / "Prices.lua").stat().st_mtime_ns == mtime
    if code == 404:
        assert res == U.PASS_OK                        # unknown now: last known prices stay
    else:
        assert res == U.PASS_RETRY and st["prices_last"] == NOW
        assert any("old files stay" in m for m in lines)


def test_404_without_data_writes_empty_file(tmp_path, xs):
    """Datalink 1.3.0: no prices -> markets = {} (the addon sees the app)."""
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Nobody Scans Here")])
    res, st, _ = run([root], xs.url)
    assert res == U.PASS_OK
    t = lua_load(addon / "Prices.lua")
    assert t.v == 1 and t.app == U.VERSION and len(t.markets) == 0 and t.checked == NOW     # never 0


def test_offline_keeps_old_file(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    before = (addon / "Prices.lua").read_bytes()
    res, st, lines = run([root], "http://127.0.0.1:9/api/v1/snapshots", st, now=NOW + 1800)
    assert res == U.PASS_RETRY and (addon / "Prices.lua").read_bytes() == before
    assert st["prices_last"] == NOW


def test_invalid_answer_keeps_old_file(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.raw = b"{not json"
    res, _, _ = run([root], xs.url)
    assert res == U.PASS_RETRY
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"
    xs.raw = json.dumps({"v": 1, "market": "m"}).encode()
    assert run([root], xs.url)[0] == U.PASS_RETRY


def test_gzip_answer(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.gzip = True
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    assert run([root], xs.url)[0] == U.PASS_OK
    assert xs.requests[0]["ae"] == "gzip"
    assert lua_markets(lua_load(addon / "Prices.lua"))[0]["items"][2589] == "0:1234:56;1:1200:60"


def test_atomic_write_failure_keeps_old_file(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    calls = []
    real = os.replace

    def locked(a, b):
        if Path(b).name == "Prices.lua":
            calls.append((Path(a), Path(b)))
            raise PermissionError("locked by the game")
        return real(a, b)

    monkeypatch.setattr(U.os, "replace", locked)
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    res, st, lines = run([root], xs.url)
    assert res == U.PASS_RETRY
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"
    assert sorted(p.name for p in addon.iterdir()) == ["Prices.lua"]          # tmp removed
    src, dst = calls[0]
    assert src.parent == dst.parent == addon and src.name != "Prices.lua"     # tmp in the same folder


def test_written_file_is_complete_when_replace_happens(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    seen = {}
    real = os.replace

    def check(a, b):
        if Path(b).name == "Prices.lua":
            seen["tmp"] = lua_load(a).v                 # tmp is valid Lua before it goes live
            seen["old"] = Path(b).read_text(encoding="ascii")
        return real(a, b)

    monkeypatch.setattr(U.os, "replace", check)
    run([root], xs.url)
    assert seen == {"tmp": 1, "old": "MidasUplinkPrices = nil\n"}


def test_unreadable_sv_skips_flavor(tmp_path, xs):
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    sv = next((root / "_forever_" / "WTF").rglob(U.SV_NAME))
    sv.write_bytes(b'MidasUplinkDB = { ["seen"] = { ')
    res, _, _ = run([root], xs.url)
    assert res == U.PASS_RETRY and xs.requests == []
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_two_accounts_union(tmp_path, xs):
    root = tmp_path / "wow"
    a, b = mk("Realm A"), mk("Realm B")
    addon = make_game(root, seen=[a], account="ACC1")
    make_game(root, seen=[b], account="ACC2")
    xs.markets[U.raw_key(a)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(b)] = export("forever/EU/pve/alliance")
    run([root], xs.url)
    got = lua_markets(lua_load(addon / "Prices.lua"))
    assert len(got) == 1 and [m["realm"] for m in got[0]["match"]] == ["Realm A", "Realm B"]


# --------------------------------------------------------------------------- #
# state, watcher, CLI, tray
# --------------------------------------------------------------------------- #
def test_state_defaults_and_repair(own_appdata):
    st = U.load_state()
    assert st["prices_enabled"] is True and st["prices_etag"] == {} and st["prices_last"] == 0
    d = own_appdata / "MidasUplink"
    (d).mkdir(exist_ok=True)
    (d / "state.json").write_text(json.dumps({"prices_enabled": "no", "prices_etag": [1],
                                              "prices_last": True}), encoding="utf-8")
    st = U.load_state()
    assert st["prices_enabled"] is True and st["prices_etag"] == {} and st["prices_last"] == 0
    st["prices_enabled"] = False
    U.save_state(st)
    assert U.load_state()["prices_enabled"] is False


def test_watcher_checks_after_pass_and_every_30_min(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A", last=int(time.time()) - 60)
    make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    clock = {"t": 1000.0}
    monkeypatch.setattr(U.time, "monotonic", lambda: clock["t"])
    w = U.Watcher([root], U.Sender(xs.url), logger()[1], interval=30)
    assert w.prices_every == 30 * 60
    w.step()                                           # start: upload pass + prices
    assert len(xs.requests) == 1
    clock["t"] += 30
    w.step()                                           # nothing changed, not due
    assert len(xs.requests) == 1
    clock["t"] += 30 * 60
    w.step()                                           # 30 min later
    assert len(xs.requests) == 2
    clock["t"] += 120
    w.step(force=True)                                 # after an upload pass
    assert len(xs.requests) == 3
    w.set_prices(False)
    assert w.prices_on() is False
    clock["t"] += 3600
    w.step(force=True)
    assert len(xs.requests) == 3 and U.load_state()["prices_enabled"] is False
    w.set_prices(True)
    w.step()
    assert len(xs.requests) == 4


def test_watcher_outbox_never_fetches(tmp_path, xs):
    root = tmp_path / "wow"
    make_game(root, seen=[mk("Realm A", last=int(time.time()))])
    w = U.Watcher([root], U.Sender(xs.url, tmp_path / "out"), logger()[1])
    w.step(force=True)
    assert xs.requests == []


def test_cli_prices_now_uses_env_url(tmp_path, xs, monkeypatch, capsys):
    root = tmp_path / "wow"
    r = mk("Realm A", last=int(time.time()) - 60)
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    monkeypatch.setattr(U, "wow_roots", lambda extra: [root])
    monkeypatch.setenv("MIDAS_UPLINK_URL", xs.url)
    assert U.main(["--prices-now"]) == 0
    assert "1 written" in capsys.readouterr().out
    assert lua_load(addon / "Prices.lua").v == 1
    xs.fail = 503
    assert U.main(["--prices-now"]) == 1


def test_tray_prices_switch_and_status(own_appdata):
    import tray as T
    app = T.App(U.DEFAULT_URL, [], 30)
    try:
        assert app.prices_enabled() is True
        assert app.prices_text() == app.t["plast"].format(app.t["never"])
        app.toggle_prices()
        assert U.load_state()["prices_enabled"] is False
        assert app.prices_text() == app.t["plast"].format(app.t["poff"])
        app.toggle_prices()
        st = U.load_state()
        st["prices_last"] = NOW
        U.save_state(st)
        assert time.strftime("%d.%m. %H:%M", time.localtime(NOW)) in app.prices_text()
        for lang in ("de", "en"):
            assert {"prices", "plast", "poff"} <= set(T.TEXT[lang])
        assert T.TEXT["de"]["prices"] == "Serverpreise laden"
    finally:
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


# --------------------------------------------------------------------------- #
# review round 2: skip codes, limits, validation, tmp files, siblings, CLI
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [400, 422, 404])
def test_400_422_skip_market_and_go_on(tmp_path, xs, code):
    root = tmp_path / "wow"
    bad, good = mk("Гордунни", client="era", ruleset=None), mk("Realm A")
    addon = make_game(root, seen=[bad, good])
    xs.codes["Гордунни"] = code
    xs.markets[U.raw_key(good)] = export("forever/EU/pve/alliance")
    res, st, lines = run([root], xs.url)
    # 422 on days=90 is asked once more without days (server before 0.4.1), then skipped
    assert res == U.PASS_OK and len(xs.requests) == (3 if code == 422 else 2)
    assert [g["market"] for g in lua_markets(lua_load(addon / "Prices.lua"))] == ["forever/EU/pve/alliance"]
    assert st["prices_last"] == NOW and not any("old files stay" in m for m in lines)


def test_too_many_items_skips_market(tmp_path, xs):
    root = tmp_path / "wow"
    big, good = mk("Big Realm"), mk("Realm A")
    addon = make_game(root, seen=[big, good])
    xs.markets[U.raw_key(big)] = export("forever/EU/big/alliance",
                                        items={str(i): [[0, 1, 1]] for i in range(1, U.PRICE_MAX_ITEMS + 2)})
    xs.markets[U.raw_key(good)] = export("forever/EU/pve/alliance")
    res, _, _ = run([root], xs.url)
    assert res == U.PASS_OK
    assert [g["market"] for g in lua_markets(lua_load(addon / "Prices.lua"))] == ["forever/EU/pve/alliance"]


@pytest.mark.parametrize("gz", [False, True])
def test_body_limit_32_mb_unpacked(tmp_path, xs, gz):
    assert U.PRICE_MAX_BYTES == 32 * 1024 * 1024 and U.PRICE_MAX_PACKED == 8 * 1024 * 1024
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    pad = b" " * (U.PRICE_MAX_BYTES + 10)                 # valid JSON whitespace, too long
    xs.raw = json.dumps(export("forever/EU/pve/alliance")).encode() + pad
    xs.gzip = gz                                          # gzip: tiny on the wire, big unpacked
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_RETRY and any("too large" in m for m in lines)
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_body_limit_8_mb_packed(tmp_path, xs):
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    xs.raw = os.urandom(U.PRICE_MAX_PACKED + 1024)       # incompressible: > 8 MB on the wire
    xs.gzip = True
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_RETRY and any("too large" in m for m in lines)
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_slow_drip_hits_deadline(tmp_path, xs, monkeypatch):
    assert U.PRICE_DEADLINE == 60
    monkeypatch.setattr(U, "PRICE_DEADLINE", 1)
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    xs.drip = True
    t0 = time.monotonic()
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_RETRY and time.monotonic() - t0 < 5
    assert any("within 1 s" in m for m in lines)
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_only_five_most_recent_markets_per_flavor(tmp_path, xs):
    root = tmp_path / "wow"
    seen = [mk(f"Realm {i}", last=NOW - i * 3600) for i in range(7)]
    make_game(root, seen=seen)
    for m in seen:
        xs.markets[U.raw_key(m)] = export("forever/EU/pve/alliance")
    run([root], xs.url)
    asked = {r["q"]["realm"] for r in xs.requests}
    assert asked == {f"Realm {i}" for i in range(5)} and len(xs.requests) == 5


def test_five_markets_of_30000_items_load_in_lua51(tmp_path):
    n = U.PRICE_MAX_ITEMS
    markets = []
    for m in range(U.PRICE_MAX_MARKETS):            # distinct IDs/strings: 300.000 constants in all
        items = {str(m * 100000 + i): f"0:{m * 100000 + i}:{m};13:{i + 7}:1" for i in range(1, n + 1)}
        markets.append({"market": f"forever/EU/r{m}/alliance", "ts": NOW, "day0": NOW - 13 * DAY,
                        "match": [{"client": "forever", "region": "EU", "realm": f"R{m}", "faction": "Alliance"}],
                        "items": items})
    text = U.render_prices(markets, NOW)
    f = tmp_path / "Prices.lua"
    f.write_text(text, encoding="utf-8")
    t = lua_load(f)
    assert len(t.markets) == 5
    assert t.markets[5]["items"][400000 + n] == f"0:{400000 + n}:4;13:{n + 7}:1"
    assert t.markets[1]["items"][1] == "0:1:0;13:8:1"
    # counter-check: the same data as one flat table exceeds the Lua 5.1 limit
    flat = tmp_path / "flat.lua"
    flat.write_text(text.replace("(function() return {", "{").replace("} } end)(),", "} },"), encoding="utf-8")
    with pytest.raises(Exception, match="constant table overflow"):      # lua51 error class
        lua_load(flat)


def test_strict_validation():
    base = export("forever/EU/pve/alliance", items={"12": [[0, 5, 1]]})
    assert U.clean_export(base)["items"] == {"12": "0:5:1"}
    for market in ("forever/EU/pve alliance", "a\nb", "", "x" * 201, "forever/ЕU/pve", 5):
        assert U.clean_export(dict(base, market=market)) is None
    for key in ("０１", "+1", " 1", "01", "1.0", "123456789", "١٢"):
        assert U.clean_export(dict(base, items={key: [[0, 5, 1]]}))["items"] == {}
    for pt in ([0, True, 1], [False, 5, 1], [0, 5, 1.0], [0, "5", 1]):
        assert U.clean_export(dict(base, items={"12": [pt]}))["items"] == {}
    assert U.clean_export(dict(base, ts=True)) is None
    assert U.clean_export(dict(base, v=True)) is None
    assert U._valid_cached({"market": "m", "ts": 1, "day0": 1, "items": {"1": "0:1:1"}})
    assert not U._valid_cached({"market": "m", "ts": 1, "day0": 1, "items": {"1": "٠:1:1"}})


def test_invalid_market_answer_aborts(tmp_path, xs):
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    xs.raw = json.dumps(export("forever/EU/pve alliance")).encode()
    assert run([root], xs.url)[0] == U.PASS_RETRY
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_render_error_in_one_folder_does_not_stop_others(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A")
    era = make_game(root, flavor="_classic_era_", seen=[r])
    forever = make_game(root, flavor="_forever_", seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    real = U.write_prices

    def flaky(addon_dir, markets, now=None):
        if "_classic_era_" in str(addon_dir):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "boom")
        return real(addon_dir, markets, now)

    monkeypatch.setattr(U, "write_prices", flaky)
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_RETRY
    assert (era / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"
    assert lua_load(forever / "Prices.lua").v == 1
    assert any("cannot write" in m for m in lines)


def test_tmp_via_mkstemp_and_old_leftovers_removed(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    old = addon / ".Prices.lua.abc123.tmp"
    new = addon / ".Prices.lua.def456.tmp"
    other = addon / "keep.tmp"
    for f in (old, new, other):
        f.write_bytes(b"x")
    two_h = time.time() - 7200
    os.utime(old, (two_h, two_h))
    os.utime(other, (two_h, two_h))
    elsewhere = root / "_forever_" / "Interface" / "AddOns" / "Other"
    elsewhere.mkdir()
    (elsewhere / ".Prices.lua.zzz.tmp").write_bytes(b"x")
    os.utime(elsewhere / ".Prices.lua.zzz.tmp", (two_h, two_h))
    made = []
    real = U.tempfile.mkstemp

    def spy(**kw):
        made.append(kw)
        return real(**kw)

    monkeypatch.setattr(U.tempfile, "mkstemp", spy)
    U.Watcher([root], U.Sender(xs.url), logger()[1])           # start: cleanup
    assert not old.exists() and new.exists() and other.exists()
    assert (elsewhere / ".Prices.lua.zzz.tmp").exists()          # not our folder
    xs.markets[U.raw_key(mk("Realm A"))] = export("forever/EU/pve/alliance")
    run([root], xs.url)
    assert made == [{"dir": addon, "prefix": ".Prices.lua.", "suffix": ".tmp"}]


def test_sibling_raw_market_fetched_once(tmp_path, xs):
    root = tmp_path / "wow"
    r3, r7 = mk("CAMWOW EU5 Realm 3 PvE"), mk("CAMWOW EU5 Realm 7 PvE")
    addon = make_game(root, seen=[r3, r7])
    xs.markets[U.raw_key(r3)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(r7)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    assert len(xs.requests) == 2                                  # mapping not known yet
    assert set(st["prices_market"].values()) == {"forever/EU/pve/alliance"}
    run([root], xs.url, st, now=NOW + 1800)
    assert len(xs.requests) == 3                                  # Realm 7 rides on Realm 3
    got = lua_markets(lua_load(addon / "Prices.lua"))
    assert [m["realm"] for m in got[0]["match"]] == ["CAMWOW EU5 Realm 3 PvE", "CAMWOW EU5 Realm 7 PvE"]
    assert U.load_state()["prices_market"] == st["prices_market"]


def test_cli_only_wow_replaces_detection(tmp_path, xs, monkeypatch, capsys):
    root = tmp_path / "wow"
    r = mk("Realm A", last=int(time.time()))
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")

    def no_detection(extra):
        raise AssertionError("auto-detection must not run")

    monkeypatch.setattr(U, "wow_roots", no_detection)
    assert U.main(["--only-wow", str(root), "--url", xs.url, "--prices-now"]) == 0
    assert lua_load(addon / "Prices.lua").v == 1
    assert U.main(["--only-wow", str(tmp_path / "missing"), "--prices-now", "--url", xs.url]) == 2


# --------------------------------------------------------------------------- #
# review round 3: daily recheck, header deadline, file size, Retry-After
# --------------------------------------------------------------------------- #
def test_sibling_shortcut_rechecked_every_24h(tmp_path, xs):
    root = tmp_path / "wow"
    r3, r7 = mk("CAMWOW EU5 Realm 3 PvE", last=NOW + 30 * 3600), mk("CAMWOW EU5 Realm 7 PvE", last=NOW + 30 * 3600)
    addon = make_game(root, seen=[r3, r7])
    xs.markets[U.raw_key(r3)] = export("forever/EU/pve/alliance")
    xs.markets[U.raw_key(r7)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    run([root], xs.url, st)
    run([root], xs.url, st, now=NOW + 23 * 3600)
    assert len(xs.requests) == 3                                  # Realm 7 rode along
    assert st["prices_checked"][U.query_key(U.export_url(xs.url), r7)] == NOW
    # server splits the key later: after 24 h Realm 7 is asked itself again
    xs.markets[U.raw_key(r7)] = export("forever/EU/pve7/alliance", items={"9": [[0, 3, 1]]})
    run([root], xs.url, st, now=NOW + 25 * 3600)
    assert len(xs.requests) == 5
    got = lua_markets(lua_load(addon / "Prices.lua"))
    assert [g["market"] for g in got] == ["forever/EU/pve/alliance", "forever/EU/pve7/alliance"]
    assert [m["realm"] for m in got[1]["match"]] == ["CAMWOW EU5 Realm 7 PvE"]


def test_slow_drip_headers_hit_deadline(tmp_path, xs, monkeypatch):
    monkeypatch.setattr(U, "PRICE_DEADLINE", 1)
    root = tmp_path / "wow"
    addon = make_game(root, seen=[mk("Realm A")])
    xs.drip_headers = True                       # 300+ header bytes, one every 0.1 s
    t0 = time.monotonic()
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_RETRY and time.monotonic() - t0 < 5
    assert any("within 1 s" in m for m in lines)
    assert (addon / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_file_limit_drops_markets_seen_longest_ago(tmp_path, xs, monkeypatch):
    assert U.PRICE_FILE_MAX == 32 * 1024 * 1024
    root = tmp_path / "wow"
    a, b, c = mk("Realm A", last=NOW - 100), mk("Realm B", last=NOW - 9000), mk("Realm C", last=NOW - 50)
    addon = make_game(root, seen=[a, b, c])
    big = {str(i): [[0, i, 1], [1, i + 1, 2]] for i in range(1, 2001)}
    for m, key in ((a, "a"), (b, "b"), (c, "c")):
        xs.markets[U.raw_key(m)] = export(f"forever/EU/{key}/alliance", items=big)
    one = len(U.render_prices([{"market": "forever/EU/a/alliance", "ts": 1, "day0": 1, "match": [],
                                "items": U.clean_export(export("x", items=big))["items"]}], 0))
    monkeypatch.setattr(U, "PRICE_FILE_MAX", int(one * 2.5))        # room for two markets
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_OK
    got = [g["market"] for g in lua_markets(lua_load(addon / "Prices.lua"))]
    assert got == ["forever/EU/a/alliance", "forever/EU/c/alliance"]   # B was seen longest ago
    assert len((addon / "Prices.lua").read_bytes()) <= U.PRICE_FILE_MAX
    assert any("left out" in m and "forever/EU/b/alliance" in m for m in lines)


def test_retry_after_over_30_min_is_respected(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=NOW)
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    xs.fail, xs.fail_headers = 429, {"Retry-After": "7200"}
    st = U.load_state()
    res, st, lines = run([root], xs.url, st)
    assert res == U.PASS_RETRY and st["prices_not_before"] == NOW + 7200
    assert U.load_state()["prices_not_before"] == NOW + 7200       # survives a restart
    xs.fail = None
    res, st, _ = run([root], xs.url, st, now=NOW + 3600)            # regular tick: no request
    assert res == U.PASS_RETRY and len(xs.requests) == 1
    res, st, _ = run([root], xs.url, st, now=NOW + 7201)
    assert res == U.PASS_OK and len(xs.requests) == 2
    assert lua_load(addon / "Prices.lua").v == 1


def test_short_retry_after_keeps_normal_tick(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=NOW)
    make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    xs.fail, xs.fail_headers = 429, {"Retry-After": "120"}
    st = U.load_state()
    assert run([root], xs.url, st)[0] == U.PASS_RETRY and st["prices_not_before"] == 0
    xs.fail = None
    assert run([root], xs.url, st, now=NOW + 1800)[0] == U.PASS_OK and len(xs.requests) == 2


def test_retry_after_parsing():
    assert U.retry_after_seconds("3600") == 3600
    assert U.retry_after_seconds("Thu, 01 Jan 2027 01:00:00 GMT", now=1798761600) == 3600
    assert U.retry_after_seconds("soon") is None and U.retry_after_seconds(None) is None
    assert U.retry_after_seconds("Thu, 01 Jan 2027 01:00:00 GMT", now=1798761600 + 7200) == 0


# --------------------------------------------------------------------------- #
# 90 days (uploader 0.4.1, server export days=90)
# --------------------------------------------------------------------------- #
def export90(market, items, ts=NOW - 600):
    return {"v": 1, "market": market, "ts": ts, "day0": 1_798_848_000 - 76 * DAY, "days": 90, "items": items}


def test_clean_export_offsets_by_period():
    d = U.clean_export(export90("m", {"1": [[0, 5, 1], [89, 6, 2], [90, 7, 3]]}))
    assert d["items"] == {"1": "0:5:1;89:6:2"}                     # days=90: off 0..89
    no_days = export90("m", {"1": [[0, 5, 1], [119, 6, 2], [120, 7, 3]]})
    del no_days["days"]
    assert U.clean_export(no_days)["items"] == {"1": "0:5:1;119:6:2"}   # tolerance 0..119
    odd = dict(export90("m", {"1": [[100, 5, 1]]}), days=True)       # unusable days -> tolerance
    assert U.clean_export(odd)["items"] == {"1": "100:5:1"}
    assert U.clean_export(export("m", items={"1": [[13, 5, 1], [14, 6, 1]]}))["items"] == {"1": "13:5:1"}


def test_asks_for_90_days_and_writes_valid_lua(tmp_path, xs):
    """Forever size: 7,100 items x 90 days, gzip answer, real Lua 5.1 parser."""
    import random
    rnd = random.Random(90)
    root = tmp_path / "wow"
    r = mk("CAMWOW EU5 Realm 7 PvE")
    addon = make_game(root, seen=[r])
    items = {str(i): [[o, rnd.randint(1, 5_000_000), rnd.randint(0, 3000)] for o in range(90)]
             for i in range(1, 7101)}
    xs.markets[U.raw_key(r)] = export90("forever/EU/pve/alliance", items)
    xs.gzip = True
    res, st, lines = run([root], xs.url)
    assert res == U.PASS_OK, lines
    assert [q["q"]["days"] for q in xs.requests] == ["90"]
    size = (addon / "Prices.lua").stat().st_size
    assert size <= U.PRICE_FILE_MAX
    got = lua_markets(lua_load(addon / "Prices.lua"))
    assert len(got) == 1 and len(got[0]["items"]) == 7100
    assert got[0]["day0"] == 1_798_848_000 - 76 * DAY
    one = got[0]["items"][1].split(";")
    assert len(one) == 90 and one[0].startswith("0:") and one[-1].startswith("89:")
    print(f"\nPrices.lua 7100 x 90: {size / 1024 / 1024:.1f} MiB")
    # unchanged answer: 304 -> no write
    before = (addon / "Prices.lua").stat().st_mtime_ns
    res, st, _ = run([root], xs.url, st, now=NOW + 3600)
    assert res == U.PASS_OK and xs.requests[-1]["inm"] and (addon / "Prices.lua").stat().st_mtime_ns == before


def test_old_server_without_days_90_falls_back_to_14(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A")
    addon = make_game(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    xs.reject_days = True
    res, _, lines = run([root], xs.url)
    assert res == U.PASS_OK, lines
    assert [q["q"].get("days") for q in xs.requests] == ["90", None]
    assert lua_markets(lua_load(addon / "Prices.lua"))[0]["items"] == {765: "13:5:0", 2589: "0:1234:56;1:1200:60"}


def test_invalid_market_422_still_skipped(tmp_path, xs):
    root = tmp_path / "wow"
    bad, good = mk("Bad Realm"), mk("Realm A")
    addon = make_game(root, seen=[bad, good])
    xs.codes["Bad Realm"] = 422
    xs.markets[U.raw_key(good)] = export("forever/EU/pve/alliance")
    res, _, _ = run([root], xs.url)
    assert res == U.PASS_OK
    assert [q["q"].get("days") for q in xs.requests if q["q"]["realm"] == "Bad Realm"] == ["90", None]
    assert [g["market"] for g in lua_markets(lua_load(addon / "Prices.lua"))] == ["forever/EU/pve/alliance"]

def test_fallback_shares_one_deadline(monkeypatch):
    """Rueckfall nach 422 bekommt keine neue 60-s-Frist: beide Abrufe teilen dasselbe Ende."""
    t = [1000.0]
    monkeypatch.setattr(U, "_clock", lambda: t[0])
    calls = []

    def fake(base, mk_, etag, timeout, deadline, end, days):
        calls.append((days, end))
        if days:
            t[0] += 25                                    # erster Abruf braucht 25 s
            return U.P_NONE, None, None, "HTTP 422 (no prices for this market)", None
        return U.P_OK, {"market": "m", "ts": 1, "day0": 0, "items": {}}, None, "HTTP 200", None

    monkeypatch.setattr(U, "_fetch_export", fake)
    res = U.fetch_export("http://127.0.0.1:9/api/v1/export", mk("Realm A"))
    assert res[0] == U.P_OK
    assert calls == [(90, 1060.0), (None, 1060.0)]
    # Frist schon verbraucht: kein zweiter Abruf
    calls.clear()
    t[0] = 1000.0

    def slow(base, mk_, etag, timeout, deadline, end, days):
        calls.append(days)
        t[0] += 61
        return U.P_NONE, None, None, "HTTP 422 (no prices for this market)", None

    monkeypatch.setattr(U, "_fetch_export", slow)
    res = U.fetch_export("http://127.0.0.1:9/api/v1/export", mk("Realm A"))
    assert res[0] == U.P_ERROR and "within 60 s" in res[3] and calls == [90]