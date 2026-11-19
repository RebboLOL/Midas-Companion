"""Companion 1.3.1, Datalink addendum 2 of 18.11.2026: Forever before the first
logout. The realm folders only carry numbers, so the Forever markets of the region
come from the server's market list and are fetched without a realm name; Prices.lua
gets them with match = {} and the server's market key.
Every test uses its own APPDATA (conftest), a temporary game folder and a fake server."""
import json
import time

import pytest

import tray as T
import ui_server as UI
import uploader as U
from test_companion import make_midas
from test_prices import ExportServer, export, lua_load, lua_markets, mk
from test_uploader import logger
from test_wtf_markets import config, new_user, realm_dir

HOUR = 3600


@pytest.fixture
def xs():
    s = ExportServer()
    yield s
    s.close()


def entry(region="EU", ruleset="PvE", faction="alliance", last_ts=1_800_000_000, client="forever", **kw):
    key = (f"forever/{region}/{ruleset.lower()}/{faction}" if client == "forever"
           else f"era/{region}/some-realm/{faction}")
    return dict({"key": key, "label": "x", "client": client, "region": region, "ruleset": ruleset,
                 "realm": "Any Realm 7 " + ruleset, "faction": faction, "last_ts": last_ts,
                 "scanners_24h": 1, "items": 10}, **kw)


def forever_user(root, numbered=("93",), portal='SET portal "EU"\n'):
    return new_user(root, flavor="_forever_", realms=tuple((n, 0) for n in numbered), portal=portal)


def paths(xs):
    return [r["path"] for r in xs.requests]


def exports(xs):
    return [r["q"] for r in xs.requests if r["path"] == "/api/v1/export"]


def serve(xs, *entries):
    xs.market_list = list(entries)
    for e in entries:
        if e["client"] == "forever":
            xs.markets[("forever", e["region"], "", e["faction"].capitalize())] = export(e["key"])


# --------------------------------------------------------------------------- #
# market list: validation
# --------------------------------------------------------------------------- #
def test_market_list_validation():
    good = entry()
    lst = U.clean_market_list([
        good, dict(good),                                           # duplicate key
        entry(client="era"), entry(region="XX"), entry(ruleset="Pv E"), entry(ruleset=""),
        entry(faction="neutral"), entry(last_ts=-1), entry(last_ts="5"), entry(last_ts=True),
        entry(key="forever/EU/pvp/alliance"),                       # key does not fit the fields
        entry(key="forever/EU/pve/alliance\nx"), "x", None, 5,
        entry(region="US", faction="horde", ruleset="RPPvP", last_ts=1_800_000_500),
    ])
    assert lst == [
        {"key": "forever/US/rppvp/horde", "client": "forever", "region": "US", "ruleset": "RPPvP", "faction": "Horde",
         "last_ts": 1_800_000_500},
        {"key": "forever/EU/pve/alliance", "client": "forever", "region": "EU", "ruleset": "PvE", "faction": "Alliance",
         "last_ts": 1_800_000_000}]
    assert U.clean_market_list({"markets": []}) is None and U.clean_market_list(None) is None
    assert U.clean_market_list([]) == []
    many = [entry(ruleset="R" + "abcdefghij"[i % 10] + "abcdefghij"[i // 10 % 10] + "abcdefghij"[i // 100],
                  last_ts=i) for i in range(300)]
    assert len(U.clean_market_list(many)) == U.LIST_KEEP
    assert U.markets_url("https://x/api/v1/export") == "https://x/api/v1/markets"
    assert U.markets_url("https://x/api/v1/other") is None


# --------------------------------------------------------------------------- #
# downlink
# --------------------------------------------------------------------------- #
def test_forever_prices_from_server_list_without_realm(tmp_path, xs):
    root = tmp_path / "wow"
    base, midas = forever_user(root)
    assert not list(base.rglob("SavedVariables"))
    xs.gzip = True
    serve(xs, entry(faction="alliance", last_ts=1_800_000_100), entry(faction="horde"),
          entry(region="US"), entry(client="era"))
    st = U.load_state()
    lines, log = logger()
    stats = {}
    res, summary = U.run_prices([root], xs.url, st, log, stats=stats)
    assert res == U.PASS_OK, summary
    assert paths(xs)[0] == "/api/v1/markets" and paths(xs).count("/api/v1/markets") == 1
    asked = exports(xs)
    assert all("realm" not in q and q["client"] == "forever" and q["region"] == "EU"
               and q["ruleset"] == "PvE" and q["days"] == "90" for q in asked)
    assert sorted(q["faction"] for q in asked) == ["Alliance", "Horde"]
    assert stats["requests"] == 3                                   # list + 2 exports
    text = (midas / "Prices.lua").read_text(encoding="utf-8")
    assert text.count("match = { },") == 2
    m = lua_markets(lua_load(midas / "Prices.lua"))
    assert [(x["market"], x["match"]) for x in m] == [("forever/EU/pve/alliance", []),
                                                      ("forever/EU/pve/horde", [])]
    assert m[0]["items"][2589] == "0:1234:56;1:1200:60"
    assert not any("error" in x.lower() or "fail" in x.lower() for x in lines)
    assert [e["key"] for e in U.load_state()["markets_list"]["forever"]] == [
        "forever/EU/pve/alliance", "forever/EU/pve/horde", "forever/US/pve/alliance"]


def test_newest_four_of_the_region(tmp_path, xs):
    root = tmp_path / "wow"
    forever_user(root)
    rules = ["PvE", "PvP", "RP", "RPPvP", "Hardcore", "Normal"]
    serve(xs, *[entry(ruleset=r, last_ts=1_800_000_000 + i) for i, r in enumerate(rules)],
          entry(region="US", last_ts=1_900_000_000))
    U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert sorted(q["ruleset"] for q in exports(xs)) == sorted(rules[2:])
    assert U.LIST_MAX_MARKETS == 4


def test_list_asked_at_most_every_6_hours_and_kept_on_failure(tmp_path, xs):
    root = tmp_path / "wow"
    _, midas = forever_user(root)
    serve(xs, entry())
    st, t0 = U.load_state(), int(time.time())
    U.run_prices([root], xs.url, st, logger()[1], now=t0)
    U.run_prices([root], xs.url, st, logger()[1], now=t0 + U.PRICE_FRESH + 60)
    U.run_prices([root], xs.url, st, logger()[1], now=t0 + U.LIST_EVERY - 60)
    assert paths(xs).count("/api/v1/markets") == 1
    xs.market_list = b"<html>nope</html>"                       # list broken, export still fine
    res, _ = U.run_prices([root], xs.url, st, logger()[1], now=t0 + U.LIST_EVERY + 60)
    assert res == U.PASS_OK and paths(xs).count("/api/v1/markets") == 2
    assert [e["key"] for e in st["markets_list"]["forever"]] == ["forever/EU/pve/alliance"]   # kept
    assert len(lua_load(midas / "Prices.lua").markets) == 1
    U.run_prices([root], xs.url, st, logger()[1], now=t0 + U.LIST_EVERY + 120)
    assert paths(xs).count("/api/v1/markets") == 2                  # not again within 30 min
    xs.market_list = [entry(), entry(faction="horde")]
    U.run_prices([root], xs.url, st, logger()[1], now=t0 + U.LIST_EVERY + U.LIST_RETRY + 120)
    assert paths(xs).count("/api/v1/markets") == 3 and len(st["markets_list"]["forever"]) == 2


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"markets": []}', b"[" + b'"x",' * 400_000 + b'"x"]'])
def test_bad_or_oversized_list_is_skipped(tmp_path, xs, raw):
    root = tmp_path / "wow"
    _, midas = forever_user(root)
    xs.market_list = raw
    res, _ = U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert res == U.PASS_OK and exports(xs) == []
    assert "markets = {\n}" in (midas / "Prices.lua").read_text(encoding="utf-8")


def test_list_unreachable_or_redirect_is_skipped(tmp_path, xs):
    root = tmp_path / "wow"
    forever_user(root)
    for code, headers in ((404, {}), (503, {}), (302, {"Location": "http://127.0.0.1:9/x"})):
        xs.requests.clear()
        xs.fail, xs.fail_headers = code, headers
        st = U.load_state()
        st["markets_list"] = {"base": "", "at": 0, "tried": 0, "forever": []}
        assert U.server_forever_markets(U.export_url(xs.url), st, 1000) == []
        assert len(xs.requests) == 1 and st["markets_list"]["tried"] == 1000 and st["markets_list"]["at"] == 0


def test_seen_forever_market_switches_the_list_off(tmp_path, xs):
    root = tmp_path / "wow"
    seen = mk("Some EU5 Realm 7 PvE", last=int(time.time()) - 60)
    midas, _ = make_midas(root, seen=[seen])
    base = root / "_forever_"
    realm_dir(base, "93")
    config(base, 'SET portal "EU"\n')
    serve(xs, entry(), entry(faction="horde"))
    xs.markets[U.raw_key(seen)] = export("forever/EU/pve/alliance")
    U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert "/api/v1/markets" not in paths(xs)
    assert [q.get("realm") for q in exports(xs)] == ["Some EU5 Realm 7 PvE"]
    m = lua_markets(lua_load(midas / "Prices.lua"))
    assert len(m) == 1 and m[0]["match"][0]["realm"] == "Some EU5 Realm 7 PvE"


@pytest.mark.parametrize("numbered,portal", [((), 'SET portal "EU"\n'), (("93",), None),
                                             (("93",), 'SET portal "xx"\n')])
def test_no_list_without_region_or_realm_folder(tmp_path, xs, numbered, portal):
    root = tmp_path / "wow"
    forever_user(root, numbered=numbered, portal=portal)
    serve(xs, entry())
    U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert xs.requests == []


def test_era_never_uses_the_list(tmp_path, xs):
    root = tmp_path / "wow"
    new_user(root, flavor="_classic_era_", realms=(("93", 0),))
    serve(xs, entry())
    U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert xs.requests == []


def test_older_server_without_realmless_export(tmp_path, xs):
    root = tmp_path / "wow"
    _, midas = forever_user(root)
    xs.market_list = [entry()]
    xs.codes[None] = 422                                           # realm missing -> 422
    st, t0 = U.load_state(), int(time.time())
    lines, log = logger()
    res, _ = U.run_prices([root], xs.url, st, log, now=t0)
    assert res == U.PASS_OK and len(exports(xs)) == 2               # days=90, then without days
    assert "markets = {\n}" in (midas / "Prices.lua").read_text(encoding="utf-8")
    assert not any("error" in x.lower() or "fail" in x.lower() for x in lines)
    U.run_prices([root], xs.url, st, log, now=t0 + HOUR)
    assert len(exports(xs)) == 2                                    # not asked again for 6 h
    del xs.codes[None]
    xs.markets[("forever", "EU", "", "Alliance")] = export("forever/EU/pve/alliance")
    U.run_prices([root], xs.url, st, log, now=t0 + U.NONE_RECHECK + 60)
    assert len(lua_load(midas / "Prices.lua").markets) == 1         # server updated: prices arrive


def test_answer_for_another_market_is_dropped(tmp_path, xs):
    root = tmp_path / "wow"
    _, midas = forever_user(root)
    xs.market_list = [entry()]
    xs.markets[("forever", "EU", "", "Alliance")] = export("forever/EU/pvp/alliance")
    res, _ = U.run_prices([root], xs.url, U.load_state(), logger()[1])
    assert res == U.PASS_OK and "markets = {\n}" in (midas / "Prices.lua").read_text(encoding="utf-8")


def test_request_cap_counts_the_list(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    forever_user(root)
    serve(xs, entry(), entry(faction="horde"), entry(ruleset="PvP"))
    monkeypatch.setattr(U, "PRICE_MAX_QUERIES", 2)
    stats = {}
    U.run_prices([root], xs.url, U.load_state(), logger()[1], stats=stats)
    assert len(xs.requests) == 2 and stats == {"requests": 2, "deferred": 2, "never": 2}


def test_markets_list_state_type_checked(own_appdata):
    U.state_file().parent.mkdir(parents=True, exist_ok=True)
    good = {"key": "forever/EU/pve/alliance", "client": "forever", "region": "EU", "ruleset": "PvE",
            "faction": "Alliance", "last_ts": 5}
    U.state_file().write_text(json.dumps({"markets_list": {
        "base": 5, "at": "x", "tried": -1, "forever": [good, {"key": "x"}, 7]}}), encoding="utf-8")
    ml = U.load_state()["markets_list"]
    assert ml["base"] == "" and ml["at"] == 0 and ml["tried"] == 0
    assert [e["key"] for e in ml["forever"]] == ["forever/EU/pve/alliance"]     # bad entries dropped
    st = U.load_state()
    st["markets_list"] = {"base": "u", "at": 1, "tried": 2, "forever": U.clean_market_list([good])}
    U.save_state(st)
    # the cache stores the cleaned form: it must survive a reload
    assert U.load_state()["markets_list"]["forever"] == st["markets_list"]["forever"] != []
    U.state_file().write_text(json.dumps({"markets_list": [1]}), encoding="utf-8")
    assert U.load_state()["markets_list"] == {"base": "", "at": 0, "tried": 0, "forever": []}


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #
def test_status_shows_server_list_as_source(tmp_path, xs):
    root = tmp_path / "wow"
    forever_user(root)
    serve(xs, entry(), entry(faction="horde"))
    st = U.load_state()
    U.run_prices([root], xs.url, st, logger()[1])
    n = len(xs.requests)
    s = T.build_status([root], st, xs.url, T.SvCache())
    assert len(xs.requests) == n                                    # the window never asks the server
    rows = [m for m in s["markets"] if m["src"] == "list"]
    assert {(m["client"], m["region"], m["ruleset"], m["faction"], m["realm"]) for m in rows} == {
        ("forever", "EU", "PvE", "Alliance", ""), ("forever", "EU", "PvE", "Horde", "")}
    assert all(m["server"] == f'forever/EU/pve/{m["faction"].lower()}' and m["items"] == 2 for m in rows)
    found = next(f["found"] for f in s["flavors"] if f["flavor"] == "_forever_")
    assert found == {"region": "EU", "realms": 0, "numbered": 1, "no_region": False, "listed": 2}
    json.dumps(s)
    empty = T.build_status([root], U.load_state(), "http://127.0.0.1:9/api/v1/snapshots", T.SvCache())
    assert [m for m in empty["markets"] if m["src"] == "list"] == []


def test_texts_new_source_and_no_old_addon_name():
    d = UI.ui_dir()
    js = (d / "app.js").read_text(encoding="utf-8")
    html = (d / "index.html").read_text(encoding="utf-8")
    assert "aus Serverliste (Forever)" in js and "from server list (Forever)" in js
    assert "Midas_Uplink" not in js and "Midas_Uplink" not in html and "oldUplink" not in js
