"""Companion 1.3.1: server prices before the first logout. Markets from the game
folder (WTF realm folders + Config.wtf), seen first, both factions, markets added
by hand (state, validation, window endpoint with token).
Every test uses its own APPDATA (conftest) and a temporary game folder; realm
names are made up."""
import json
import os
import time
from pathlib import Path

import pytest

import tray as T
import ui_server as UI
import uploader as U
from test_companion import login, make_midas, post_headers, req
from test_prices import ExportServer, export, lua_load, lua_markets, mk
from test_uploader import logger

DAY = 86400


@pytest.fixture
def xs():
    s = ExportServer()
    yield s
    s.close()


def realm_dir(base, realm, chars=("Hero",), account="ACC1", age_days=0):
    """WTF\\Account\\<acc>\\<realm>\\<char> with a file per character, age set via mtime."""
    r = Path(base) / "WTF" / "Account" / account / realm
    t = time.time() - age_days * DAY
    for c in chars:
        d = r / c
        d.mkdir(parents=True, exist_ok=True)
        f = d / "config-cache.wtf"
        f.write_text('SET x "1"\n', encoding="ascii")
        os.utime(f, (t, t))
        os.utime(d, (t, t))
    os.utime(r, (t, t))
    return r


def config(base, text):
    w = Path(base) / "WTF"
    w.mkdir(parents=True, exist_ok=True)
    (w / "Config.wtf").write_bytes(text.encode("utf-8"))


def new_user(root, flavor="_classic_era_", realms=(("Alpha Realm", 0),), portal='SET portal "eu"\n'):
    """Midas 1.3.0 installed, realm folders + Config.wtf, NO SavedVariables."""
    base = Path(root) / flavor
    midas = base / "Interface" / "AddOns" / "Midas"
    midas.mkdir(parents=True, exist_ok=True)
    (midas / "Midas.toc").write_text("## Title: Midas\n## Version: 1.3.0\n"
                                     "## SavedVariables: MidasDB, MidasUplinkDB\n\nPrices.lua\nMidas.lua\n",
                                     encoding="utf-8")
    (midas / "Prices.lua").write_text("MidasUplinkPrices = nil\n", encoding="ascii")
    for name, age in realms:
        realm_dir(base, name, age_days=age)
    if portal is not None:
        config(base, portal)
    return base, midas


def gets(xs):
    return [r["q"] for r in xs.requests if r["path"] == "/api/v1/export"]


# --------------------------------------------------------------------------- #
# WTF folder
# --------------------------------------------------------------------------- #
def test_wtf_realms_accounts_filters_and_special_names(tmp_path):
    base = tmp_path / "_classic_era_"
    realm_dir(base, "Alpha Realm", account="ACC1", age_days=3)
    realm_dir(base, "Alpha Realm", account="ACC2", age_days=1)          # merged, newest wins
    realm_dir(base, "O'Brien Hold", account="ACC2")                     # apostrophe
    realm_dir(base, "Zwei Worte Drei", account="ACC1")                  # spaces
    realm_dir(base, "Ünterwelt", account="ACC1")                        # non-ASCII
    realm_dir(base, "Old Realm", account="ACC1", age_days=31)           # older than 30 days
    realm_dir(base, "93", account="ACC1")                               # numbered (Forever style)
    (base / "WTF" / "Account" / "ACC1" / "Empty Realm").mkdir()         # no character folder
    sv = base / "WTF" / "Account" / "ACC1" / "SavedVariables" / "Hero"
    sv.mkdir(parents=True)                                              # never a realm
    (base / "WTF" / "Account" / "ACC1" / "bindings-cache.wtf").write_text("x", encoding="ascii")
    realms, numbered = U.wtf_realms(base)
    assert set(realms) == {"Alpha Realm", "O'Brien Hold", "Zwei Worte Drei", "Ünterwelt"}
    assert numbered == 1
    assert time.time() - realms["Alpha Realm"] < 1.5 * DAY
    assert U.wtf_realms(tmp_path / "missing") == ({}, 0)


def test_character_file_counts_as_change(tmp_path):
    base = tmp_path / "_anniversary_"
    r = realm_dir(base, "Beta Realm", age_days=40)
    f = r / "Hero" / "AddOns.txt"
    f.write_text("Midas: enabled\n", encoding="ascii")                  # written at logout = now
    os.utime(r / "Hero", (time.time() - 40 * DAY,) * 2)
    os.utime(r, (time.time() - 40 * DAY,) * 2)
    assert "Beta Realm" in U.wtf_realms(base)[0]


@pytest.mark.parametrize("text,region", [
    ('SET portal "eu"\n', "EU"),
    ('SET portal "EU"\r\nSET textLocale "deDE"\r\n', "EU"),
    ('SET gxApi "D3D11"\nSET portal "us"\n', "US"),
    ('  set portal "kr"\n', "KR"),
    ('SET portal tw\n', "TW"),
    ('SET portal "CN"\n', "CN"),
    ('SET portal "eu"\nSET portal "us"\n', "US"),                      # last one counts
    ('SET portal "public-test"\n', None),
    ('SET portal ""\n', None),
    ('SET realmName "x"\n', None),
    ('', None),
])
def test_config_wtf_region_variants(tmp_path, text, region):
    config(tmp_path, text)
    assert U.wtf_region(tmp_path) == region


def test_config_wtf_missing(tmp_path):
    assert U.wtf_region(tmp_path) is None


@pytest.mark.parametrize("raw", [
    "\ufeffSET portal \"eu\"\n".encode("utf-8"),
    'SET portal "eu"\r\n'.encode("utf-16"),                       # with BOM
    'SET textLocale "deDE"\nSET portal "eu"\n'.encode("utf-16-le"),  # without BOM
    'SET portal "eu"\n'.encode("utf-16-be"),
])
def test_config_wtf_bom_and_utf16(tmp_path, raw):
    (tmp_path / "WTF").mkdir()
    (tmp_path / "WTF" / "Config.wtf").write_bytes(raw)
    assert U.wtf_region(tmp_path) == "EU"


def test_config_wtf_cached_until_changed(tmp_path, monkeypatch):
    config(tmp_path, 'SET portal "eu"\n')
    assert U.wtf_region(tmp_path) == "EU"
    monkeypatch.setattr(U, "_portal_region", lambda text: pytest.fail("read again"))
    assert U.wtf_region(tmp_path) == "EU"
    monkeypatch.undo()
    config(tmp_path, 'SET portal "us"\n')
    f = tmp_path / "WTF" / "Config.wtf"
    os.utime(f, (time.time() + 5,) * 2)
    assert U.wtf_region(tmp_path) == "US"


def test_ruleset_from_realm_suffix():
    assert U.forever_ruleset("Some EU5 Realm 7 PvE") == "PvE"
    assert U.forever_ruleset("Some Realm RPPvP") == "RPPvP"
    assert U.forever_ruleset("Some Realm Hardcore") == "Hardcore"
    assert U.forever_ruleset("Plain Realm") == "Normal"
    assert U.market_ruleset("era", "Plain Realm PvP") is None


@pytest.mark.parametrize("realm,ruleset", [
    ("Test EU4 Realm 9 RP PvP", "RPPvP"),          # role-play PvP: two words, own auction house
    ("Test US1 Realm 12 RP PVP", "RPPvP"),
    ("Test EU4 Realm 9 rp pvp", "RPPvP"),
    ("Test EU4 Realm 9 RP  PvP", "RPPvP"),
    ("RP PvP", "RPPvP"),
    ("Test EU4 Realm 9 RP-PvP", "RPPvP"),
    ("Test EU4 Realm 9 RPPvP", "RPPvP"),
    ("Test EU5 Realm 10 RP", "RP"),
    ("Test US1 Realm 8 PvP", "PvP"),
    ("PvP", "PvP"),
    ("Test EU5 Realm 7 RP PvE", "PvE"),            # only RP + PvP belong together
    ("Test EU5 Realm 7 Harp PvP", "PvP"),          # whole word, not a word ending
    ("Test EU5 Realm 7 PvP RP", "RP"),
    ("Test EU5 Realm 7 pve", "PvE"),               # any case, like the server
    ("Test EU5 Realm 9", "Normal"),
    ("", "Normal"),
    (None, "Normal"),
])
def test_ruleset_two_word_suffix(realm, ruleset):
    assert U.forever_ruleset(realm) == ruleset
    assert U.market_ruleset("forever", realm) == ruleset
    assert U.market_ruleset("era", realm) is None


def test_rp_pvp_folder_and_pvp_folder_are_two_markets(tmp_path):
    base = tmp_path / "_forever_"
    realm_dir(base, "Test EU4 Realm 2 PvP")
    realm_dir(base, "Test EU4 Realm 9 RP PvP")
    config(base, 'SET portal "EU"\n')
    out, _info = U.extra_markets(tmp_path, "_forever_", [])
    assert {(m["realm"], m["ruleset"]) for m in out} == {
        ("Test EU4 Realm 2 PvP", "PvP"), ("Test EU4 Realm 9 RP PvP", "RPPvP")}
    assert len({U.raw_key(m) for m in out}) == 4            # both factions each, never merged
    q = dict(U.export_params(next(m for m in out if m["ruleset"] == "RPPvP")))
    assert q["realm"] == "Test EU4 Realm 9 RP PvP" and q["ruleset"] == "RPPvP"
    assert len({U.query_key("http://127.0.0.1:1/api/v1/export", m) for m in out}) == 4


def test_manual_rp_pvp_market_asks_for_rppvp(tmp_path, xs):
    root = tmp_path / "wow"
    _, midas = new_user(root, flavor="_forever_", realms=(), portal=None)
    st = U.load_state()
    lst = U.add_manual_market([], {"flavor": "_forever_", "region": "EU",
                                   "realm": "Test EU4 Realm 9 RP PvP", "faction": "Horde"})
    st["manual_markets"] = U.add_manual_market(lst, {"flavor": "_forever_", "region": "EU",
                                                     "realm": "Test EU4 Realm 2 PvP", "faction": "Horde"})
    xs.markets[("forever", "EU", "Test EU4 Realm 9 RP PvP", "Horde")] = export("forever/EU/rppvp/horde")
    xs.markets[("forever", "EU", "Test EU4 Realm 2 PvP", "Horde")] = export("forever/EU/pvp/horde")
    res, _ = U.run_prices([root], xs.url, st, logger()[1])
    assert res == U.PASS_OK
    assert sorted((q["realm"], q["ruleset"]) for q in gets(xs)) == [
        ("Test EU4 Realm 2 PvP", "PvP"), ("Test EU4 Realm 9 RP PvP", "RPPvP")]
    got = {x["market"]: [m["realm"] for m in x["match"]] for x in lua_markets(lua_load(midas / "Prices.lua"))}
    assert got == {"forever/EU/pvp/horde": ["Test EU4 Realm 2 PvP"],
                   "forever/EU/rppvp/horde": ["Test EU4 Realm 9 RP PvP"]}


# --------------------------------------------------------------------------- #
# market list: seen first, both factions, limits
# --------------------------------------------------------------------------- #
def test_extra_markets_newest_five_both_factions(tmp_path):
    base = tmp_path / "_classic_era_"
    for i in range(7):
        realm_dir(base, f"Realm {i}", age_days=i)
    config(base, 'SET portal "eu"\n')
    out, info = U.extra_markets(tmp_path, "_classic_era_", [])
    assert info["realms"] == [f"Realm {i}" for i in range(5)] and info["region"] == "EU"
    assert len(out) == 10
    assert {(m["realm"], m["faction"]) for m in out} == {(f"Realm {i}", f) for i in range(5)
                                                         for f in ("Alliance", "Horde")}
    assert all(m["client"] == "era" and m["region"] == "EU" and m["ruleset"] is None
               and m["src"] == "wtf" for m in out)


def test_forever_folder_names_and_numbers(tmp_path):
    base = tmp_path / "_forever_"
    realm_dir(base, "93")                                       # how Forever names them today
    config(base, 'SET portal "EU"\n')
    out, info = U.extra_markets(tmp_path, "_forever_", [])
    assert out == [] and info["numbered"] == 1 and info["realms"] == []
    realm_dir(base, "Test EU9 Realm 2 PvP")                    # a named folder would work
    out, info = U.extra_markets(tmp_path, "_forever_", [])
    assert {(m["client"], m["ruleset"], m["faction"]) for m in out} == {
        ("forever", "PvP", "Alliance"), ("forever", "PvP", "Horde")}


def test_seen_realm_wins(tmp_path):
    base = tmp_path / "_classic_era_"
    realm_dir(base, "Alpha Realm")
    realm_dir(base, "Beta Realm")
    config(base, 'SET portal "eu"\n')
    seen = [mk("alpha realm", client="era", ruleset="Normal")]     # case-insensitive, one faction
    out, _ = U.extra_markets(tmp_path, "_classic_era_", seen)
    assert {m["realm"] for m in out} == {"Beta Realm"}


def test_unknown_region_skips_folder_markets(tmp_path):
    base = tmp_path / "_classic_era_"
    realm_dir(base, "Alpha Realm")
    out, info = U.extra_markets(tmp_path, "_classic_era_", [])
    assert out == [] and info["no_region"] and info["region"] is None
    config(base, 'SET portal "beta"\n')
    out, info = U.extra_markets(tmp_path, "_classic_era_", [])
    assert out == [] and info["no_region"]


# --------------------------------------------------------------------------- #
# downlink without SavedVariables
# --------------------------------------------------------------------------- #
def test_prices_written_without_saved_variables(tmp_path, xs):
    root = tmp_path / "wow"
    base, midas = new_user(root, realms=(("Alpha Realm", 0), ("O'Brien Hold", 2)))
    assert not list(base.rglob("SavedVariables"))
    xs.gzip = True
    xs.markets[("era", "EU", "O'Brien Hold", "Horde")] = export("era/EU/obrien-hold/horde")
    st = U.load_state()
    lines, log = logger()
    stats = {}
    res, summary = U.run_prices([root], xs.url, st, log, stats=stats)
    assert res == U.PASS_OK, summary
    asked = {(q["realm"], q["faction"]) for q in gets(xs)}
    assert asked == {("Alpha Realm", "Alliance"), ("Alpha Realm", "Horde"),
                     ("O'Brien Hold", "Alliance"), ("O'Brien Hold", "Horde")}
    assert all("ruleset" not in q and q["region"] == "EU" and q["client"] == "era" for q in gets(xs))
    assert stats == {"requests": 4, "deferred": 0, "never": 0}
    t = lua_load(midas / "Prices.lua")
    m = lua_markets(t)
    assert len(m) == 1 and m[0]["market"] == "era/EU/obrien-hold/horde"
    assert m[0]["match"] == [{"client": "era", "region": "EU", "realm": "O'Brien Hold", "faction": "Horde"}]
    assert not any("error" in x.lower() or "fail" in x.lower() for x in lines)    # 404 is normal
    # 404 markets are not asked again within 6 h, the one with data not within 29 min
    n = len(gets(xs))
    U.run_prices([root], xs.url, st, log)
    assert gets(xs)[n:] == []
    assert lua_markets(lua_load(midas / "Prices.lua"))[0]["market"] == "era/EU/obrien-hold/horde"
    U.run_prices([root], xs.url, st, log, now=int(time.time()) + U.PRICE_FRESH + 60)
    assert [(q["realm"], q["faction"]) for q in gets(xs)[n:]] == [("O'Brien Hold", "Horde")]
    # after 6 h all of them are
    n = len(gets(xs))
    U.run_prices([root], xs.url, st, log, now=int(time.time()) + U.NONE_RECHECK + 60)
    assert len(gets(xs)) - n == 4


def test_seen_comes_first_and_folder_market_drops_out(tmp_path, xs):
    root = tmp_path / "wow"
    seen = mk("Alpha Realm", faction="Alliance", client="era", ruleset="Normal",
              last=int(time.time()) - 60)
    make_midas(root, flavor="_classic_era_", seen=[seen])
    base = root / "_classic_era_"
    realm_dir(base, "Alpha Realm")
    realm_dir(base, "Beta Realm")
    config(base, 'SET portal "eu"\n')
    U.run_prices([root], xs.url, U.load_state(), logger()[1])
    asked = [(q["realm"], q["faction"], q.get("ruleset")) for q in gets(xs)]
    assert asked[0] == ("Alpha Realm", "Alliance", "Normal")                 # seen asked first
    assert sorted(asked[1:]) == [("Beta Realm", "Alliance", None), ("Beta Realm", "Horde", None)]


def test_request_limit_per_pass(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    new_user(root, realms=tuple((f"Realm {i}", i) for i in range(5)))
    monkeypatch.setattr(U, "PRICE_MAX_QUERIES", 3)
    stats = {}
    res, _ = U.run_prices([root], xs.url, U.load_state(), logger()[1], stats=stats)
    assert res == U.PASS_OK and len(gets(xs)) == 3 and stats == {"requests": 3, "deferred": 7, "never": 7}


def test_request_cap_rotates_and_then_keeps_the_30_min_cadence(tmp_path, xs, monkeypatch):
    """10 markets with data, cap 3: every market is asked within a few passes (never
    asked first, then the oldest check), afterwards none again before 29 min."""
    root = tmp_path / "wow"
    new_user(root, realms=tuple((f"Realm {i}", i) for i in range(5)))
    for i in range(5):
        for f in ("Alliance", "Horde"):
            xs.markets[("era", "EU", f"Realm {i}", f)] = export(f"era/EU/realm-{i}/{f.lower()}")
    monkeypatch.setattr(U, "PRICE_MAX_QUERIES", 3)
    st, t0 = U.load_state(), int(time.time())
    asked, per_pass, nevers = [], [], []
    for p in range(8):
        n = len(gets(xs))
        stats = {}
        U.run_prices([root], xs.url, st, logger()[1], now=t0 + p * 61, stats=stats)
        new = [(q["realm"], q["faction"]) for q in gets(xs)[n:]]
        asked += new
        per_pass.append(len(new))
        nevers.append(stats["never"])
    assert len(set(asked)) == 10 and len(asked) == 10              # each exactly once
    assert per_pass == [3, 3, 3, 1, 0, 0, 0, 0] and nevers[:4] == [7, 4, 1, 0]
    m = lua_markets(lua_load(root / "_classic_era_" / "Interface" / "AddOns" / "Midas" / "Prices.lua"))
    assert len(m) == 10                                             # deferred ones kept from cache
    # 30 min later: the three checked longest ago come first
    n = len(gets(xs))
    U.run_prices([root], xs.url, st, logger()[1], now=t0 + 30 * 60)
    assert {(q["realm"], q["faction"]) for q in gets(xs)[n:]} == set(asked[:3])


def test_fallback_422_counts_as_two_requests(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    new_user(root, realms=(("Realm 0", 0), ("Realm 1", 1)))
    xs.reject_days = True                                           # old server: 422 for days=90
    monkeypatch.setattr(U, "PRICE_MAX_QUERIES", 3)
    stats = {}
    U.run_prices([root], xs.url, U.load_state(), logger()[1], stats=stats)
    assert len(gets(xs)) == 4 and stats["requests"] == 4 and stats["deferred"] == 2


def test_watcher_catch_up_only_for_never_asked(tmp_path, xs, monkeypatch):
    root = tmp_path / "wow"
    new_user(root, realms=tuple((f"Realm {i}", i) for i in range(3)))
    monkeypatch.setattr(U, "PRICE_MAX_QUERIES", 4)
    w = U.Watcher([root], U.Sender(xs.url), logger()[1])
    w.step()
    assert w.prices_due == w.prices_ran + U.PRICES_MIN_GAP          # 2 never asked: soon
    w.prices_ran -= U.PRICES_MIN_GAP + 1
    w.prices_due -= U.PRICES_MIN_GAP + 1
    w.step()
    assert len(gets(xs)) == 6 and w.prices_due == w.prices_ran + U.PRICES_EVERY


def test_watcher_rechecks_after_new_realm_folder(tmp_path, xs):
    root = tmp_path / "wow"
    base, _ = new_user(root, realms=())
    w = U.Watcher([root], U.Sender(xs.url), logger()[1])
    w.step()
    assert gets(xs) == []
    w.prices_ran -= U.PRICES_MIN_GAP + 1                # minimum gap over, 30 min not yet
    w.step()
    assert gets(xs) == []                               # nothing changed: no check
    realm_dir(base, "Gamma Realm")
    w.step()
    assert {q["realm"] for q in gets(xs)} == {"Gamma Realm"}


# --------------------------------------------------------------------------- #
# markets added by hand
# --------------------------------------------------------------------------- #
GOOD = {"flavor": "_classic_era_", "region": "eu", "realm": " Alpha Realm ", "faction": "horde"}


@pytest.mark.parametrize("bad,why", [
    ({"flavor": "_retail_"}, "flavor"), ({"flavor": "_classic_era_ptr_"}, "flavor"),
    ({"region": "XX"}, "region"), ({"region": 3}, "region"),
    ({"realm": ""}, "realm"), ({"realm": "   "}, "realm"), ({"realm": "x" * 101}, "realm"),
    ({"realm": "Bad\x01Realm"}, "realm"), ({"realm": "A|B"}, "realm"), ({"realm": 7}, "realm"),
    ({"faction": "Neutral"}, "faction"), ({"faction": None}, "faction"),
])
def test_manual_market_validation(bad, why):
    with pytest.raises(ValueError) as e:
        U.clean_manual_market(dict(GOOD, **bad))
    assert str(e.value) == why


def test_lone_surrogates_rejected_and_dropped(tmp_path, xs, own_appdata):
    assert not U.valid_realm_name("A\ud800B") and not U.valid_realm_name("\udcff")
    assert U.valid_realm_name("\U0001F600 Emoji")                     # real non-BMP character is fine
    with pytest.raises(ValueError, match="realm"):
        U.add_manual_market([], dict(GOOD, realm="A\ud800B"))
    st = U.load_state()
    st["manual_markets"] = [dict(GOOD, realm="A\ud800B", added=1), dict(GOOD, realm="Fine", added=1)]
    U.save_state(st)                                                  # JSON keeps \ud800 escaped
    assert [m["realm"] for m in U.load_state()["manual_markets"]] == ["Fine"]
    # a market with an unencodable realm never stops the pass
    root = tmp_path / "wow"
    base, midas = new_user(root, realms=(("Alpha Realm", 0),))
    xs.markets[("era", "EU", "Alpha Realm", "Horde")] = export("era/EU/alpha-realm/horde")
    bad = U._raw_market("era", "EU", "Bad\ud800Realm", "Horde", time.time(), "wtf")
    real = U.extra_markets
    U.extra_markets = lambda *a, **k: (lambda r: (r[0] + [bad], r[1]))(real(*a, **k))
    try:
        res, _ = U.run_prices([root], xs.url, U.load_state(), logger()[1])
    finally:
        U.extra_markets = real
    assert res == U.PASS_OK
    assert lua_markets(lua_load(midas / "Prices.lua"))[0]["market"] == "era/EU/alpha-realm/horde"


def test_surrogate_folder_name_skipped(tmp_path, monkeypatch):
    base = tmp_path / "_classic_era_"
    realm_dir(base, "Alpha Realm")
    try:
        realm_dir(base, "Bad\ud800Realm")
    except (OSError, UnicodeError):
        pytest.skip("file system refuses the name")
    assert set(U.wtf_realms(base)[0]) == {"Alpha Realm"}


def test_manual_market_add_limits_and_remove():
    c = U.clean_manual_market(GOOD)
    assert c == {"flavor": "_classic_era_", "region": "EU", "realm": "Alpha Realm", "faction": "Horde", "added": 0}
    assert U.clean_manual_market(dict(GOOD, realm="x" * 100))["realm"] == "x" * 100
    lst = U.add_manual_market([], GOOD, now=5)
    with pytest.raises(ValueError, match="dup"):
        U.add_manual_market(lst, dict(GOOD, realm="ALPHA REALM"))
    for i in range(1, U.MANUAL_MAX):
        lst = U.add_manual_market(lst, dict(GOOD, realm=f"Realm {i}"))
    with pytest.raises(ValueError, match="full"):
        U.add_manual_market(lst, dict(GOOD, realm="One More"))
    lst2 = U.remove_manual_market(lst, U.manual_id(lst[0]))
    assert len(lst2) == U.MANUAL_MAX - 1 and lst2[0]["realm"] == "Realm 1"


def test_manual_markets_state_type_checked(own_appdata):
    st = U.load_state()
    good = [dict(GOOD, realm=f"Realm {i}") for i in range(12)]
    st["manual_markets"] = ["x", {"flavor": "nope"}, good[0], dict(good[0])] + good[1:]
    U.save_state(st)
    got = U.load_state()["manual_markets"]
    assert len(got) == U.MANUAL_MAX and got[0]["realm"] == "Realm 0" and got[1]["realm"] == "Realm 1"
    (U.state_file()).write_text(json.dumps({"manual_markets": {"a": 1}}), encoding="utf-8")
    assert U.load_state()["manual_markets"] == []


def test_manual_market_fetched_both_factions(tmp_path, xs):
    root = tmp_path / "wow"
    _, midas = new_user(root, flavor="_forever_", realms=(), portal=None)
    st = U.load_state()
    st["manual_markets"] = U.add_manual_market([], {"flavor": "_forever_", "region": "EU",
                                                    "realm": "Test EU9 Realm 2 PvE", "faction": "Both"})
    xs.markets[("forever", "EU", "Test EU9 Realm 2 PvE", "Alliance")] = export("forever/EU/pve/alliance")
    res, _ = U.run_prices([root], xs.url, st, logger()[1])
    assert res == U.PASS_OK
    assert sorted((q["faction"], q["ruleset"]) for q in gets(xs)) == [("Alliance", "PvE"), ("Horde", "PvE")]
    m = lua_markets(lua_load(midas / "Prices.lua"))
    assert [x["market"] for x in m] == ["forever/EU/pve/alliance"]


def test_status_shows_sources_and_folder_notes(tmp_path, xs):
    root = tmp_path / "wow"
    seen = mk("Alpha Realm", client="era", ruleset="Normal", last=int(time.time()) - 60)
    make_midas(root, flavor="_classic_era_", seen=[seen])
    base = root / "_classic_era_"
    realm_dir(base, "Alpha Realm")
    realm_dir(base, "Beta Realm")
    config(base, 'SET portal "eu"\n')
    new_user(root, flavor="_anniversary_", realms=(("Delta Realm", 0),), portal=None)
    new_user(root, flavor="_forever_", realms=(("93", 0),))
    manual = U.add_manual_market([], dict(GOOD, realm="Hand Realm"))
    s = T.build_status([root], U.load_state(), xs.url, T.SvCache(), manual=manual)
    src = {(m["realm"], m["faction"]): m["src"] for m in s["markets"]}
    assert src[("Alpha Realm", "Alliance")] == "seen"
    assert src[("Beta Realm", "Alliance")] == src[("Beta Realm", "Horde")] == "wtf"
    assert src[("Hand Realm", "Horde")] == "manual"
    assert ("Alpha Realm", "Horde") not in src                    # realm known from Midas
    assert [m["src"] for m in s["markets"]][:2] == ["seen", "manual"]
    fl = {f["flavor"]: f["found"] for f in s["flavors"]}
    assert fl["_classic_era_"] == {"region": "EU", "realms": 1, "numbered": 0, "no_region": False, "listed": 0}
    assert fl["_anniversary_"]["no_region"] and fl["_anniversary_"]["realms"] == 0
    assert fl["_forever_"]["numbered"] == 1 and fl["_forever_"]["realms"] == 0
    assert s["manual"][0]["realm"] == "Hand Realm" and s["manual_max"] == U.MANUAL_MAX
    wtf_row = next(m for m in s["markets"] if m["src"] == "wtf")
    assert wtf_row["folder_at"] > 0 and wtf_row["last_seen"] == 0
    json.dumps(s)


def test_window_endpoint_add_and_remove_market(own_appdata):
    app = T.App(U.DEFAULT_URL, [], 30, lang="de")
    srv = UI.UIServer(T._Backend(app), "de").start()
    try:
        ck, _ = login(srv)
        body = json.dumps(dict(GOOD, realm="Hand Realm")).encode()
        code, _, _ = req(srv, "POST", "api/market_add", dict(ck, **{"Content-Type": "application/json"}), body)
        assert code == 403                                               # no page token
        assert U.load_state()["manual_markets"] == []
        code, _, data = req(srv, "POST", "api/market_add", post_headers(srv, ck), body)
        r = json.loads(data)
        assert code == 200 and r["ok"]
        assert [m["realm"] for m in U.load_state()["manual_markets"]] == ["Hand Realm"]
        code, _, data = req(srv, "POST", "api/market_add", post_headers(srv, ck), body)
        assert json.loads(data) == {"ok": False, "error": "dup"}
        bad = json.dumps(dict(GOOD, realm="")).encode()
        assert json.loads(req(srv, "POST", "api/market_add", post_headers(srv, ck), bad)[2])["error"] == "realm"
        code, _, data = req(srv, "GET", "api/status", ck)
        s = json.loads(data)
        assert s["manual"][0]["id"] == r["id"]
        assert json.loads(req(srv, "POST", "api/market_del", post_headers(srv, ck),
                              json.dumps({"id": "nope"}).encode())[2])["ok"] is False
        assert json.loads(req(srv, "POST", "api/market_del", post_headers(srv, ck),
                              json.dumps({"id": r["id"]}).encode())[2])["ok"] is True
        assert U.load_state()["manual_markets"] == []
    finally:
        srv.close()
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


def test_direct_state_write_not_lost_to_new_watcher(own_appdata, tmp_path, monkeypatch):
    """Without a watcher the window writes the state directly; a watcher created at
    the same time must load that change, not overwrite it."""
    import threading
    root = tmp_path / "wow"
    new_user(root, realms=(), portal=None)
    app = T.App("http://127.0.0.1:9/api/v1/snapshots", [], 30, only_wow=[str(root)], lang="de")
    try:
        entered, release = threading.Event(), threading.Event()
        real_save = U.save_state

        def slow_save(st):                      # the direct write pauses inside the lock
            if st.get("manual_markets"):
                entered.set()
                release.wait(5)
            return real_save(st)
        monkeypatch.setattr(U, "save_state", slow_save)
        th = threading.Thread(target=app.add_market, args=(dict(GOOD, realm="Hand Realm"),))
        th.start()
        assert entered.wait(5)
        made = threading.Thread(target=lambda: app.__dict__.update(
            watcher=(app._state_lock.acquire(), U.Watcher([root], U.Sender(app.url), logger()[1]),
                     app._state_lock.release())[1]))
        made.start()
        time.sleep(0.2)
        assert app.watcher is None                                    # waits for the write
        release.set()
        th.join(5)
        made.join(5)
        assert [m["realm"] for m in app.watcher.st["manual_markets"]] == ["Hand Realm"]
        assert [m["realm"] for m in U.load_state()["manual_markets"]] == ["Hand Realm"]
    finally:
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


def test_watcher_applies_manual_markets(tmp_path, xs):
    root = tmp_path / "wow"
    new_user(root, realms=(), portal=None)
    w = U.Watcher([root], U.Sender(xs.url), logger()[1])
    w.step()
    assert gets(xs) == []
    lst = U.add_manual_market([], dict(GOOD, realm="Hand Realm"))
    w.set_manual(lst)
    assert w.manual_markets() == lst and w.st["manual_markets"] == []
    w.prices_ran -= U.PRICES_MIN_GAP + 1
    w.step()
    assert w.st["manual_markets"] == lst and U.load_state()["manual_markets"] == lst
    assert {(q["realm"], q["faction"]) for q in gets(xs)} == {("Hand Realm", "Horde")}


def test_ui_files_carry_new_texts():
    d = UI.ui_dir()
    js = (d / "app.js").read_text(encoding="utf-8")
    html = (d / "index.html").read_text(encoding="utf-8")
    for t in ("aus Spielordner erkannt", "aus Midas", "found in the game folder", "from Midas",
              "Markt hinzufügen", "Add market", "api/market_add", "api/market_del"):
        assert t in js, t
    assert 'id="addm-realm"' in html and 'id="manual"' in html and "style=" not in html
    assert "innerHTML" not in js
