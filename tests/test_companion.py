"""Companion App 1.3.0 (Datalink 1.3.0): Midas.lua extraction, Prices.lua targets,
head fields, window server (token, host check, endpoints), status for the window.
Every test uses its own APPDATA (conftest) and a temporary game folder."""
import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

import tray as T
import ui_server as UI
import uploader as U
from test_prices import ExportServer, export, lua_load, mk, seen_lua
from test_uploader import ITEMS_A, logger, snap_lua

NOW = 1_800_000_000


@pytest.fixture
def xs():
    s = ExportServer()
    yield s
    s.close()


def uplink_block(seen=(), enabled=False, snaps=(), addon="1.3.0"):
    return ("MidasUplinkDB = {\n" f'["enabled"] = {"true" if enabled else "false"},\n'
            f'["format"] = 1,\n["addon"] = "{addon}",\n["queue"] = {{\n' + "".join(snaps) + "},\n"
            + seen_lua(seen) + "}\n")


def midasdb_block(n_lines=50, pad=""):
    rows = "".join(f'\t\t["k{i}"] = "v{i}{pad}",\n' for i in range(n_lines))
    return 'MidasDB = {\n\t["realms"] = {\n' + rows + "\t},\n}\n"


def make_midas(root, flavor="_forever_", seen=(), enabled=False, snaps=(), first=True,
               toc_prices=True, version="1.3.0", account="ACC1", old_addon=False):
    """Game folder with Midas 1.3.0 (Midas.lua holds MidasDB + MidasUplinkDB)."""
    base = Path(root) / flavor
    sv = base / "WTF" / "Account" / account / "SavedVariables"
    sv.mkdir(parents=True, exist_ok=True)
    up, db = uplink_block(seen, enabled, snaps), midasdb_block()
    (sv / U.SV_MIDAS).write_bytes(((db + up) if first else (up + db)).encode("utf-8"))
    midas = base / "Interface" / "AddOns" / "Midas"
    midas.mkdir(parents=True, exist_ok=True)
    files = "Prices.lua\nMidas.lua\n" if toc_prices else "Midas.lua\n"
    (midas / "Midas.toc").write_text(f"## Interface: 11509\n## Title: Midas\n## Version: {version}\n"
                                     f"## SavedVariables: MidasDB, MidasUplinkDB\n\n{files}", encoding="utf-8")
    (midas / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    old = base / "Interface" / "AddOns" / "Midas_Uplink"
    if old_addon:
        old.mkdir(parents=True, exist_ok=True)
        (old / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    return midas, old


# --------------------------------------------------------------------------- #
# Midas.lua: cut out MidasUplinkDB
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("first", [True, False])
def test_extract_uplink_db_before_and_after_midasdb(first):
    up = uplink_block([mk("Realm A")], enabled=True)
    db = midasdb_block()
    data = ((db + up) if first else (up + db)).encode("utf-8")
    v = U.extract_global(data, "MidasUplinkDB")
    assert v["enabled"] is True and v["addon"] == "1.3.0"
    assert U.seen_of_db(v)[0]["realm"] == "Realm A"
    assert U.extract_global(data, "MidasDB")["realms"]["k3"] == "v3"


def test_extract_missing_nil_and_cut_off():
    assert U.extract_global(midasdb_block().encode(), "MidasUplinkDB") is None
    assert U.extract_global(b"MidasDB = {\n}\nMidasUplinkDB = nil\n") is None
    with pytest.raises(U.LuaParseError):
        U.extract_global(b'MidasDB = {\n}\nMidasUplinkDB = {\n["enabled"] = true,\n["queue"] = { {')
    # CRLF and a file that starts with the block
    v = U.extract_global(b'MidasUplinkDB = {\r\n["enabled"] = true,\r\n}\r\nMidasDB = {\r\n}\r\n')
    assert v == {"enabled": True}


def test_extract_ignores_lookalikes_inside_strings_and_tables():
    # a string with an escaped line break followed by the global name, a nested key
    tricky = ('MidasDB = {\n\t["note"] = "line\\\nMidasUplinkDB = { [\\"enabled\\"] = true }",\n'
              '\t["MidasUplinkDB"] = 5,\n}\n')
    assert U.extract_global(tricky.encode()) is None
    # a "global" inside a string of the block does not end the block
    inner = 'MidasUplinkDB = {\n["note"] = "a\\\nMidasDB = 1",\n["enabled"] = true,\n}\n'
    assert U.extract_global(inner.encode())["enabled"] is True


def test_extract_big_file_fast(tmp_path):
    """Midas.lua ~50 MB with MidasDB first: only the small block is parsed."""
    db = midasdb_block(n_lines=760_000, pad="x" * 40)
    data = (db + uplink_block([mk("Realm A")], enabled=True)).encode("utf-8")
    assert len(data) > 45 * 1024 * 1024
    f = tmp_path / U.SV_MIDAS
    f.write_bytes(data)
    t0 = time.perf_counter()
    v = U.load_uplink_db(f)
    assert time.perf_counter() - t0 < 3.0
    assert v["enabled"] is True
    # and the other order: block first, MidasDB after (bounded by the next global)
    f.write_bytes((uplink_block([mk("Realm A")]) + db).encode("utf-8"))
    t0 = time.perf_counter()
    assert U.load_uplink_db(f)["enabled"] is False
    assert time.perf_counter() - t0 < 3.0


def test_sv_files_reads_both_names(tmp_path):
    root = tmp_path / "wow"
    make_midas(root)
    old = root / "_forever_" / "WTF" / "Account" / "ACC2" / "SavedVariables"
    old.mkdir(parents=True)
    (old / U.SV_NAME).write_text(uplink_block(), encoding="utf-8")
    names = sorted(p.name for p in U.sv_files([root]))
    assert names == [U.SV_MIDAS, U.SV_NAME]


def test_snapshots_sent_from_midas_lua(tmp_path, server):
    root = tmp_path / "wow"
    make_midas(root, enabled=True, snaps=[snap_lua(100, ITEMS_A)], first=True)
    make_midas(root, flavor="_classic_era_", enabled=False, snaps=[snap_lua(200, ITEMS_A)])
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert [b["snapshot"]["ts"] for b in server.bodies] == [100]          # opt-in respected
    um = st["upload_markets"]
    assert list(um) == ["forever|EU|Test Realm|Alliance"] and um["forever|EU|Test Realm|Alliance"]["ts"] == 100
    assert U.load_state()["upload_markets"] == um


def _old_sv(root, enabled, flavor="_forever_", age=0):
    """Leftover Midas_Uplink.lua next to Midas.lua (WoW never deletes it)."""
    d = Path(root) / flavor / "WTF" / "Account" / "ACC1" / "SavedVariables"
    f = d / U.SV_NAME
    f.write_text(uplink_block([mk("Old Realm", last=int(time.time()))], enabled=enabled,
                              snaps=[snap_lua(300, ITEMS_A, realm="Old Realm")]), encoding="utf-8")
    m = (d / U.SV_MIDAS).stat().st_mtime
    os.utime(f, (m - age, m - age))
    return f


@pytest.mark.parametrize("old_addon, age, counts", [
    (False, 0, False),          # Midas_Uplink removed: leftover file never counts
    (True, 3600, False),        # folder still there, but not loaded any more (file is old)
    (True, 0, True),            # transition: old addon loaded and written at the last logout
])
def test_leftover_old_sv_cannot_override_sharing_off(tmp_path, server, xs, old_addon, age, counts):
    root = tmp_path / "wow"
    make_midas(root, enabled=False, seen=[mk("New Realm", last=int(time.time()))], old_addon=old_addon)
    old = _old_sv(root, enabled=True, age=age)
    acc = old.parent
    assert U.authoritative_sv(acc, old_addon) == (old if counts else acc / U.SV_MIDAS)
    assert U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1]) == U.PASS_OK
    assert [b["snapshot"]["ts"] for b in server.bodies] == ([300] if counts else [])
    s = T.build_status([root], U.load_state(), xs.url, T.SvCache())
    assert s["share_on"] is counts
    assert {m["realm"] for m in s["markets"]} == ({"Old Realm"} if counts else {"New Realm"})
    targets, _ = U.price_targets([root])
    assert {m["realm"] for _d, mks in targets for m in mks} == ({"Old Realm"} if counts else {"New Realm"})


def test_only_old_sv_counts_only_with_its_addon_folder(tmp_path):
    d = tmp_path / "_forever_" / "WTF" / "Account" / "A" / "SavedVariables"
    d.mkdir(parents=True)
    (d / U.SV_NAME).write_text(uplink_block(enabled=True), encoding="utf-8")
    assert list(U.sv_sources([tmp_path])) == []                 # Midas.lua missing, addon removed
    assert U.authoritative_sv(d, False) is None
    (tmp_path / "_forever_" / "Interface" / "AddOns" / "Midas_Uplink").mkdir(parents=True)
    assert list(U.sv_sources([tmp_path])) == [d / U.SV_NAME]
    assert U.authoritative_sv(tmp_path / "missing", True) is None


def test_midas_lua_without_uplink_block_is_empty(tmp_path, server):
    root = tmp_path / "wow"
    d = root / "_forever_" / "WTF" / "Account" / "A" / "SavedVariables"
    d.mkdir(parents=True)
    (d / U.SV_MIDAS).write_text(midasdb_block(), encoding="utf-8")          # Midas 1.2.x
    assert U.read_sv(d / U.SV_MIDAS)["queue"] == [] and U.parse_seen(d / U.SV_MIDAS) == []
    assert U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1]) == U.PASS_OK


# --------------------------------------------------------------------------- #
# Prices.lua targets + head fields
# --------------------------------------------------------------------------- #
def test_toc_and_version_helpers(tmp_path):
    midas, _ = make_midas(tmp_path / "wow")
    toc = U.read_toc(midas, "Midas")
    assert toc["version"] == "1.3.0" and "prices.lua" in toc["files"]
    assert U.read_toc(tmp_path, "Nope") is None
    assert U.parse_version("1.2.28") == (1, 2, 28) < U.MIDAS_MIN <= U.parse_version("1.3.0")
    assert U.parse_version("v1.10") == (1, 10) and U.parse_version("x") is None
    assert U.midas_lists_prices(midas)


def test_prices_go_to_midas_and_old_folder(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=NOW - 60)
    midas, old = make_midas(root, seen=[r], old_addon=True)
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    lines, log = logger()
    res, _ = U.run_prices([root], xs.url, U.load_state(), log, now=NOW)
    assert res == U.PASS_OK and len(xs.requests) == 1                   # fetched once for both
    for d in (midas, old):
        t = lua_load(d / "Prices.lua")
        assert t.app == U.VERSION and t.checked == NOW and t.written == NOW
        assert t.markets[1].market == "forever/EU/pve/alliance"
    assert U.load_state()["prices_info"] == {"forever/EU/pve/alliance": {"ts": NOW - 600, "items": 2}}


def test_midas_without_prices_in_toc_is_not_written(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=NOW - 60)
    midas, _ = make_midas(root, seen=[r], toc_prices=False)
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    res, summary = U.run_prices([root], xs.url, U.load_state(), logger()[1], now=NOW)
    assert res == U.PASS_OK and xs.requests == [] and "no Midas" in summary
    assert (midas / "Prices.lua").read_text(encoding="ascii") == "MidasUplinkPrices = nil\n"


def test_file_written_without_prices_and_kept_with_prices(tmp_path, xs):
    root = tmp_path / "wow"
    midas, _ = make_midas(root, seen=[])                                  # no market seen yet
    st = U.load_state()
    assert U.run_prices([root], xs.url, st, logger()[1], now=NOW)[0] == U.PASS_OK
    t = lua_load(midas / "Prices.lua")
    assert t.app == U.VERSION and len(t.markets) == 0 and t.checked == NOW
    # with prices once, a later "nothing known" pass keeps them
    r = mk("Realm A", last=NOW)
    make_midas(root, seen=[r])
    (midas / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    U.run_prices([root], xs.url, st, logger()[1], now=NOW + 100)
    assert len(lua_load(midas / "Prices.lua").markets) == 1
    sv = root / "_forever_" / "WTF" / "Account" / "ACC1" / "SavedVariables" / U.SV_MIDAS
    sv.write_text(midasdb_block() + uplink_block([]), encoding="utf-8")       # market gone
    U.run_prices([root], xs.url, st, logger()[1], now=NOW + 200)
    assert len(lua_load(midas / "Prices.lua").markets) == 1


def test_placeholder_after_cf_update_rewritten(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=NOW)
    midas, _ = make_midas(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    U.run_prices([root], xs.url, st, logger()[1], now=NOW)
    (midas / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")    # CF update
    U.run_prices([root], xs.url, st, logger()[1], now=NOW + 1800)                      # 304
    assert xs.requests[-1]["inm"] and len(lua_load(midas / "Prices.lua").markets) == 1


def test_checked_refreshed_at_most_every_3_hours(tmp_path):
    d = tmp_path / "Midas"
    d.mkdir()
    g = [{"market": "m", "ts": 1, "day0": 1, "match": [], "items": {"1": "0:1:1"}}]
    assert U.write_prices(d, g, NOW) == "written"
    assert U.write_prices(d, g, NOW + 3600) == "unchanged"
    assert lua_load(d / "Prices.lua").checked == NOW
    assert U.write_prices(d, g, NOW + U.PRICE_CHECKED_STEP) == "written"
    assert lua_load(d / "Prices.lua").checked == NOW + U.PRICE_CHECKED_STEP
    # old 0.4.x file without app/checked -> rewritten once
    (d / "Prices.lua").write_text(U.render_prices(g, NOW).replace(f'app = "{U.VERSION}", ', ""), encoding="utf-8")
    assert U.write_prices(d, g, NOW + U.PRICE_CHECKED_STEP) == "written"


def test_watcher_rewrites_placeholder_without_waiting(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    r = mk("Realm A", last=int(time.time()))
    midas, _ = make_midas(root, seen=[r])
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    clock = {"t": 1000.0}
    monkeypatch.setattr(U.time, "monotonic", lambda: clock["t"])
    w = U.Watcher([root], U.Sender(xs.url), logger()[1], interval=30)
    w.step()
    assert len(xs.requests) == 1
    clock["t"] += 120
    w.step()
    assert len(xs.requests) == 1                         # nothing changed
    (midas / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    clock["t"] += 30
    w.step()
    assert len(xs.requests) == 1 and len(lua_load(midas / "Prices.lua").markets) == 1   # from the cache


def test_normalize_wow_dir(tmp_path):
    root = tmp_path / "World of Warcraft"
    (root / "_classic_era_" / "Interface").mkdir(parents=True)
    assert U.normalize_wow_dir(root) == root.resolve()
    assert U.normalize_wow_dir(root / "_classic_era_") == root.resolve()
    assert U.normalize_wow_dir(root / "_classic_era_" / "Interface") == root.resolve()
    assert U.normalize_wow_dir(tmp_path / "nothing") is None
    (tmp_path / "empty").mkdir()
    assert U.normalize_wow_dir(tmp_path / "empty") is None


def test_state_fields_type_checked(own_appdata):
    d = own_appdata / "MidasUplink"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({
        "upload_markets": {"a": {"ts": 1, "at": 2}, "b": {"ts": "x", "at": 1}, "c": 5},
        "prices_info": {"m": {"ts": 1, "items": 3}, "n": [1]},
        "wow_dirs": ["C:\\WoW", 5, ""], "window_shown": "yes"}), encoding="utf-8")
    st = U.load_state()
    assert st["upload_markets"] == {"a": {"ts": 1, "at": 2, "kind": "full"}}   # 1.3.1 (19.11.): kind
    assert st["prices_info"] == {"m": {"ts": 1, "items": 3}}
    assert st["wow_dirs"] == ["C:\\WoW"] and st["window_shown"] is False


# --------------------------------------------------------------------------- #
# window server
# --------------------------------------------------------------------------- #
class FakeBackend:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"ok": True, "app": {"version": U.VERSION}}

    def log_since(self, after):
        return 7, [{"n": 7, "t": 1, "m": "hello"}] if after < 7 else []

    def action(self, name, data):
        self.calls.append((name, data))
        return {"ok": True}


@pytest.fixture
def ui():
    be = FakeBackend()
    srv = UI.UIServer(be, "de").start()
    srv.be = be
    yield srv
    srv.close()


def req(srv, method, path, headers=None, body=None, host=None):
    """Plain HTTP client; path relative to the secret prefix unless it starts with '/'."""
    if not path.startswith("/"):
        path = srv.prefix + path
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": host if host is not None else f"127.0.0.1:{srv.port}"}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


def login(srv):
    url = srv.new_url()
    path = url[len(srv.origin):]
    code, h, _ = req(srv, "GET", path)
    assert code == 303 and h["Location"] == srv.prefix
    cookie = h["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Domain" not in cookie
    assert f"Path={srv.prefix}" in cookie
    return {"Cookie": cookie.split(";")[0]}, path


def post_headers(srv, ck):
    return dict(ck, **{"X-Midas-Token": srv.page_token, "Content-Type": "application/json"})


def test_ui_binds_loopback_random_port_path_and_secrets():
    a, b = UI.UIServer(FakeBackend()), UI.UIServer(FakeBackend())
    try:
        assert a.httpd.server_address[0] == "127.0.0.1" and a.port != b.port
        assert a.prefix != b.prefix and len(a.prefix) > 20
        assert a.session != b.session and a.session != a.page_token and len(a.page_token) >= 40
        u1, u2 = a.new_url(), a.new_url()
        assert u1.startswith(f"http://127.0.0.1:{a.port}{a.prefix}?ticket=") and u1 != u2
        assert a.session not in u1 and a.page_token not in u1
    finally:
        a.httpd.server_close()
        b.httpd.server_close()


def test_ui_ticket_is_one_time_and_expires(ui, monkeypatch):
    ck, path = login(ui)
    assert req(ui, "GET", path)[0] == 403                                  # second use, no cookie
    code, h, _ = req(ui, "GET", path, ck)                                  # reload of the old URL
    assert code == 303 and h["Location"] == ui.prefix and "Set-Cookie" not in h
    assert req(ui, "GET", "?ticket=wrong", ck)[0] == 303
    assert req(ui, "GET", "?ticket=wrong")[0] == 403
    t = ui.new_url()[len(ui.origin):]
    now = time.monotonic()
    monkeypatch.setattr(UI.time, "monotonic", lambda: now + UI.TICKET_TTL + 1)
    assert req(ui, "GET", t)[0] == 403                                     # expired
    monkeypatch.undo()
    code, h, body = req(ui, "GET", "", ck)
    assert code == 200 and h["Content-Type"].startswith("text/html")
    assert ui.page_token.encode() in body and ui.session.encode() not in body and b'lang="de"' in body
    assert "script-src 'self'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"
    assert h["Cache-Control"] == "no-store"


def test_ui_needs_cookie_and_secret_path(ui):
    assert req(ui, "GET", "")[0] == 403                                    # no cookie
    assert req(ui, "GET", "api/status")[0] == 403
    assert req(ui, "GET", "", {"Cookie": f"{ui.cookie}=nope"})[0] == 403
    ck, _ = login(ui)
    assert req(ui, "GET", "/", ck)[0] == 404                               # outside the secret path
    assert req(ui, "GET", "/api/status", ck)[0] == 404
    assert req(ui, "GET", "/s/guess/api/status", ck)[0] == 404


def test_ui_cookie_alone_allows_no_action_and_is_server_bound(ui):
    """Cookies are not bound to a port: a service on another port that grabbed the
    cookie must not trigger anything (no page token), and another instance does not
    accept it at all."""
    ck, _ = login(ui)
    stolen_value = ck["Cookie"].split("=", 1)[1]
    for tok in (stolen_value, "", ui.cookie):
        h = dict(ck, **{"X-Midas-Token": tok, "Content-Type": "application/json"})
        assert req(ui, "POST", "api/send", h, "{}")[0] == 403
    other = UI.UIServer(FakeBackend()).start()
    try:
        forged = {"Cookie": f"{other.cookie}={stolen_value}"}
        assert req(other, "GET", "api/status", forged)[0] == 403
        assert req(other, "POST", "api/send", dict(forged, **{"X-Midas-Token": ui.page_token,
                                                              "Content-Type": "application/json"}), "{}")[0] == 403
    finally:
        other.close()
    assert ui.be.calls == []


def test_ui_host_check_blocks_dns_rebinding(ui):
    ck, _ = login(ui)
    for host in ("evil.example", f"evil.example:{ui.port}", f"127.0.0.1:{ui.port + 1}", ""):
        assert req(ui, "GET", "api/status", ck, host=host)[0] == 421
    assert req(ui, "GET", ui.new_url()[len(ui.origin):], host="evil.example")[0] == 421
    assert req(ui, "GET", "api/status", ck, host=f"localhost:{ui.port}")[0] == 200


def test_ui_endpoints(ui):
    ck, _ = login(ui)
    code, h, body = req(ui, "GET", "api/status", ck)
    assert code == 200 and json.loads(body)["app"]["version"] == U.VERSION
    assert json.loads(req(ui, "GET", "api/log?after=0", ck)[2]) == {"last": 7, "lines": [{"n": 7, "t": 1, "m": "hello"}]}
    assert json.loads(req(ui, "GET", "api/log?after=x", ck)[2])["last"] == 7
    for path, ctype in (("app.js", "text/javascript"), ("app.css", "text/css"), ("logo.png", "image/png")):
        code, h, body = req(ui, "GET", path, ck)
        assert code == 200 and h["Content-Type"].startswith(ctype) and body
    assert req(ui, "GET", "../uploader.py", ck)[0] == 404
    assert req(ui, "GET", "api/nothing", ck)[0] == 404


def test_ui_post_needs_token_header_origin_and_json(ui):
    ck, _ = login(ui)
    good = post_headers(ui, ck)
    body = json.dumps({"on": True})
    assert req(ui, "POST", "api/prices", good, body)[0] == 200
    assert ui.be.calls == [("prices", {"on": True})]
    # cross-site form post: no custom header
    assert req(ui, "POST", "api/send", dict(ck, **{"Content-Type": "application/json"}), "{}")[0] == 403
    assert req(ui, "POST", "api/send", dict(good, **{"X-Midas-Token": "x"}), "{}")[0] == 403
    assert req(ui, "POST", "api/send", {"X-Midas-Token": ui.page_token, "Content-Type": "application/json"}, "{}")[0] == 403
    assert req(ui, "POST", "api/send", dict(good, Origin="http://evil.example"), "{}")[0] == 403
    assert req(ui, "POST", "api/send", dict(good, Origin=f"http://127.0.0.1:{ui.port}"), "{}")[0] == 200
    assert req(ui, "POST", "api/send", dict(good, **{"Sec-Fetch-Site": "cross-site"}), "{}")[0] == 403
    assert req(ui, "POST", "api/send", dict(good, **{"Content-Type": "text/plain"}), "{}")[0] == 415
    assert req(ui, "POST", "api/send", good, "[1]")[0] == 400
    assert req(ui, "POST", "api/send", good, "x" * (UI.BODY_MAX + 1))[0] == 413
    assert req(ui, "POST", "api/nope", good, "{}")[0] == 404
    assert req(ui, "OPTIONS", "api/send", good)[0] == 403
    assert req(ui, "GET", "api/status", dict(ck, **{"Sec-Fetch-Site": "cross-site"}))[0] == 403
    assert [c[0] for c in ui.be.calls] == ["prices", "send"]


def test_ui_denials_with_body_never_reset(ui):
    """Every refusal reads the body first: the client always gets the status
    (an unread body made Windows reset the connection)."""
    ck, _ = login(ui)
    big = "y" * 3000
    cases = [({}, 403), (ck, 403), (dict(ck, **{"Content-Type": "application/json"}), 403),
             (dict(post_headers(ui, ck), Origin="http://evil.example"), 403),
             (dict(post_headers(ui, ck), **{"Content-Type": "text/plain"}), 415)]
    for _ in range(25):
        for h, code in cases:
            assert req(ui, "POST", "api/send", h, big)[0] == code
        assert req(ui, "POST", "api/send", post_headers(ui, ck), "z" * 60000)[0] == 413
        assert req(ui, "POST", "api/send", ck, big, host="evil.example")[0] == 421
        assert req(ui, "POST", "/elsewhere", ck, big)[0] == 404
    assert ui.be.calls == []


# --------------------------------------------------------------------------- #
# status for the window, tray actions
# --------------------------------------------------------------------------- #
def test_build_status_checklist(tmp_path, xs):
    root = tmp_path / "wow"
    r = mk("Realm A", last=int(time.time()) - 60)
    make_midas(root, seen=[r], enabled=True)
    make_midas(root, flavor="_classic_era_", version="1.2.28", toc_prices=False, old_addon=True)
    xs.markets[U.raw_key(r)] = export("forever/EU/pve/alliance")
    st = U.load_state()
    U.run_prices([root], xs.url, st, logger()[1])
    s = T.build_status([root], st, xs.url, T.SvCache())
    assert s["app"] == {"name": "Midas Companion", "version": U.VERSION}
    assert s["wow"]["found"] and s["midas_ok"] and s["share_on"]
    fl = {f["flavor"]: f for f in s["flavors"]}
    assert fl["_forever_"]["midas_ok"] and fl["_forever_"]["label"] == "Forever"
    assert fl["_classic_era_"]["midas"] == "1.2.28" and not fl["_classic_era_"]["midas_ok"]
    assert fl["_classic_era_"]["old_uplink"] is None                   # folder without .toc
    assert s["prices_written"] and s["prices_with_data"]
    assert s["markets"][0]["realm"] == "Realm A" and s["markets"][0]["server"] == "forever/EU/pve/alliance"
    assert s["markets"][0]["items"] == 2
    json.dumps(s)
    empty = T.build_status([], U.load_state(), xs.url, T.SvCache())
    assert not empty["wow"]["found"] and not empty["share_on"] and empty["markets"] == []


def test_app_actions_and_folder(own_appdata, tmp_path, monkeypatch):
    app = T.App(U.DEFAULT_URL, [], 30, lang="de")
    try:
        assert app.action("folder", {"path": str(tmp_path / "nope")}) == {"ok": False, "error": "no_wow"}
        root = tmp_path / "World of Warcraft"
        (root / "_anniversary_").mkdir(parents=True)
        r = app.action("folder", {"path": str(root / "_anniversary_")})
        assert r["ok"] and r["root"] == str(root.resolve())
        monkeypatch.setattr(U, "wow_roots", lambda extra: [Path(e) for e in extra])
        app._apply_dir_requests()
        assert U.load_state()["wow_dirs"] == [str(root.resolve())]
        assert app.find_roots() == [root.resolve()]
        assert app.action("open", {"what": "elsewhere"})["ok"] is False
        assert app.action("bogus", {})["ok"] is False
        app.action("prices", {"on": False})
        assert U.load_state()["prices_enabled"] is False
        assert app.first_start() is True and app.first_start() is False
        s = app.status_data()
        assert s["lang"] == "de" and s["autostart"] is False
        assert s["client_id"] == U.load_state()["client_id"] and len(s["client_id"]) == 32
    finally:
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


def test_only_wow_skips_detection(own_appdata, tmp_path, monkeypatch):
    def boom(extra):
        raise AssertionError("auto-detection must not run")
    monkeypatch.setattr(U, "wow_roots", boom)
    app = T.App(U.DEFAULT_URL, [], 30, only_wow=[str(tmp_path)])
    try:
        assert app.find_roots() == [tmp_path]
    finally:
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


def _close_log(app):
    for h in list(app.log.handlers):
        h.close()
        app.log.removeHandler(h)


def test_wow_roots_skips_a_drive_that_cannot_be_read(tmp_path, monkeypatch):
    """23.11.2026 (user report): one drive letter that answers with an error
    (locked, unreadable, lost network drive) must not end the folder search."""
    root = tmp_path / "World of Warcraft"
    (root / "_forever_").mkdir(parents=True)
    real_stat, real_isdir = os.stat, os.path.isdir

    def stat(p, *a, **kw):                  # what pathlib's exists()/is_dir() run into
        if str(p).upper().startswith("Q:"):
            raise OSError(22, "drive cannot be read", str(p), 1005)
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(U, "local_drives", lambda: ["Q"])
    with pytest.raises(OSError):
        Path("Q:\\").exists()               # the call the search used before
    assert U.wow_roots([str(root), "Q:\\World of Warcraft", str(tmp_path / "nope")]) == [root]

    def isdir(p):                           # and should the check itself ever raise
        if str(p).upper().startswith("Q:"):
            raise OSError(22, "drive cannot be read", str(p), 1005)
        return real_isdir(p)

    monkeypatch.setattr(os.path, "isdir", isdir)
    assert U.is_dir("Q:\\") is False
    assert U.wow_roots([str(root), "Q:\\World of Warcraft"]) == [root]


def test_local_drives_are_letters_of_existing_drives(monkeypatch):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    drives = U.local_drives()
    assert drives and all(len(d) == 1 and d in letters for d in drives)
    assert Path(os.environ["SystemRoot"]).drive[0].upper() in drives
    import ctypes

    def no_dll(name):
        raise OSError("kernel32 not available")
    monkeypatch.setattr(ctypes, "WinDLL", no_dll)
    assert U.local_drives() == list(letters)        # fallback: every letter


def test_state_file_that_is_not_utf8_is_moved_aside(own_appdata):
    """Review 23.11.2026: such a file raised UnicodeDecodeError on every load."""
    st = U.load_state()
    U.save_state(st)
    f = U.state_file()
    f.write_bytes(b'{"client_id": "\xff\xfe"}')
    fresh = U.load_state()
    assert len(fresh["client_id"]) == 32 and fresh["wow_dirs"] == []
    assert not f.exists() and list(f.parent.glob("state_corrupt_*.json"))


def test_wow_roots_usual_places_on_a_drive(tmp_path, monkeypatch):
    """Auto-detection without any state: <drive>:\\Program Files (x86)\\World of Warcraft."""
    real = os.path.isdir
    fake = tmp_path / "driveq"
    (fake / "Program Files (x86)" / "World of Warcraft" / "_forever_").mkdir(parents=True)

    def isdir(p):
        s = str(p)
        return real(str(fake) + s[2:]) if s.upper().startswith("Q:") else real(p)

    monkeypatch.setattr(os.path, "isdir", isdir)
    monkeypatch.setattr(U, "local_drives", lambda: ["Q"])
    assert U.wow_roots([]) == [Path("Q:\\Program Files (x86)\\World of Warcraft")]


def test_loop_survives_a_failing_folder_search(own_appdata, tmp_path, monkeypatch):
    """The background thread used to die when the folder search raised: the window
    stayed on "folder not found", a folder chosen by hand was confirmed ("Added")
    but never taken over or saved."""
    root = tmp_path / "World of Warcraft"
    (root / "_forever_").mkdir(parents=True)
    monkeypatch.setattr(U, "local_drives", lambda: [])
    real, calls = U.wow_roots, []

    def flaky(extra):
        calls.append(list(extra))
        if len(calls) == 1:
            raise OSError(22, "drive cannot be read", "Q:\\", 1005)
        return real(extra)

    monkeypatch.setattr(U, "wow_roots", flaky)
    app = T.App("http://127.0.0.1:9/api/v1/snapshots", [], 30, lang="en")
    th = threading.Thread(target=app.loop, daemon=True)
    th.start()
    try:
        end = time.time() + 10
        while not calls and time.time() < end:
            time.sleep(0.02)
        time.sleep(0.2)
        assert th.is_alive() and app.roots == []
        r = app.action("folder", {"path": str(root)})
        assert r == {"ok": True, "root": str(root.resolve())}
        while app.roots != [root.resolve()] and time.time() < end:
            time.sleep(0.02)
        assert app.roots == [root.resolve()]
        assert app.status_data()["wow"]["found"] is True
        assert U.load_state()["wow_dirs"] == [str(root.resolve())]
    finally:
        app.stop.set()
        app.wake.set()
        th.join(timeout=10)
        _close_log(app)
    assert not th.is_alive()


def test_picked_folder_counts_even_when_the_state_cannot_be_saved(own_appdata, tmp_path, monkeypatch):
    root = tmp_path / "World of Warcraft"
    (root / "_classic_era_").mkdir(parents=True)
    monkeypatch.setattr(U, "local_drives", lambda: [])
    app = T.App(U.DEFAULT_URL, [], 30, lang="en")
    try:
        assert app.action("folder", {"path": str(root)})["ok"]

        def no_save(st):
            raise OSError(13, "state is locked")
        monkeypatch.setattr(U, "save_state", no_save)
        with pytest.raises(OSError):
            app._apply_dir_requests()
        assert app.watcher is None and app.find_roots() == [root.resolve()]
        # locked state file (save_state -> False): the choice rides along in the
        # watcher's state and is written with its next save
        monkeypatch.setattr(U, "save_state", lambda st: False)
        monkeypatch.setattr(U.Watcher, "step", lambda self, force=False: 30)
        assert U.load_state()["wow_dirs"] == []
        assert app._pass() == 30
        assert app.roots == [root.resolve()] and app.watcher.st["wow_dirs"] == [str(root.resolve())]
    finally:
        _close_log(app)


def test_ring_log_and_texts(own_appdata):
    lg = T.setup_log()
    seq0, _ = T.RING.since(0)
    lg.info("ring one")
    lg.info("ring two")
    last, lines = T.RING.since(seq0)
    assert last == seq0 + 2 and [x["m"] for x in lines] == ["ring one", "ring two"]
    assert T.RING.since(last)[1] == []
    for lang in ("de", "en"):
        assert {"open", "pick"} <= set(T.TEXT[lang])
    assert T.TEXT["de"]["open"] == "Öffnen"
    assert T.APP_NAME == "Midas Companion" and T.LNK_NAME == "Midas Uplink.lnk"
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


def test_open_event_name_isolated_for_start_tests(own_appdata, monkeypatch):
    assert T.open_event_name() == T.OPEN_EVENT
    monkeypatch.setenv("MIDAS_UPLINK_SECOND_INSTANCE", "1")
    assert T.open_event_name().startswith("Local\\MidasUplinkOpen_") and T.open_event_name() != T.OPEN_EVENT
    assert T.signal_running_instance() is False                           # nobody listens


def test_window_opens_on_first_start_not_on_autostart(own_appdata, monkeypatch):
    class A:
        n = 0

        def first_start(self):
            A.n += 1
            return A.n == 1

    class Args:
        no_window = False
        autostart = True

    monkeypatch.setattr(T, "system_uptime", lambda: 10_000)
    assert T.should_open_window(A(), Args) is True                        # first start, even via autostart
    assert T.should_open_window(A(), Args) is False                       # autostart later
    Args.autostart = False
    assert T.should_open_window(A(), Args) is True                        # started by hand
    monkeypatch.setattr(T, "system_uptime", lambda: 60)
    assert T.should_open_window(A(), Args) is False                       # right after logon
    Args.no_window = True
    assert T.should_open_window(A(), Args) is False


def test_ui_files_exist_and_carry_texts():
    d = UI.ui_dir()
    html = (d / "index.html").read_text(encoding="utf-8")
    js = (d / "app.js").read_text(encoding="utf-8")
    assert "{{TOKEN}}" in html and "{{LANG}}" in html and "<script src=\"app.js\">" in html
    assert "<script>" not in html and "style=" not in html                # CSP: own files only
    assert "Begleit-App zum World-of-Warcraft-Addon Midas" in js
    assert ("Midas Market ist ein Fanprojekt und nicht mit Blizzard Entertainment verbunden. "
            "World of Warcraft ist eine Marke von Blizzard Entertainment.") in js
    assert "innerHTML" not in js
    assert "Für Auskunft oder Löschung diese Kennung an support@midasmarket.app senden" in js
    assert "send this ID to support@midasmarket.app" in js and 'id="cid"' in html and "s.client_id" in js
    assert "/midas datalink on" in js and "share on" not in js and "Share my scans" in js
    assert (d / "logo.png").stat().st_size < 64 * 1024
