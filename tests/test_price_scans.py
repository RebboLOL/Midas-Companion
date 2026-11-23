"""Companion 1.3.1 (19.11.2026), Datalink addendum 3 of 19.11.2026: price scans without an offer
book (MidasUplinkDB.pqueue, items "listed,low") are posted with "kind": "prices"
after the full snapshots of a pass, with their own backoff.
Every test uses its own APPDATA (conftest) and a local fake server."""
import time
from pathlib import Path

import pytest

import uploader as U
import tray as T
from test_meta import META, write_meta_sv
from test_uploader import ITEMS_A, ITEMS_B, logger, lua_str, snap_lua, write_sv

PITEMS = {1000: "12,480", 1001: "3,95", 1002: "0,11"}
OK_P = (201, {"status": "ok", "kind": "prices", "meta": 0})     # server that knows price scans
OK_F = (201, {"status": "ok", "meta": 0})


def pscan_lua(ts, items=PITEMS, realm="Test Realm", faction="Alliance", seq=7, kind="prices", v=1):
    it = "\n".join(f"[{i}] = {lua_str(x)}," for i, x in items.items())
    k = f'["kind"] = {lua_str(kind)}, ' if kind is not None else ""
    return (f'{{ ["v"] = {v}, {k}["seq"] = {seq}, ["ts"] = {ts}, ["addon"] = "1.3.1", '
            f'["count"] = {len(items)},\n'
            f'["market"] = {{ ["client"] = "forever", ["region"] = "EU", ["realm"] = {lua_str(realm)},\n'
            f'["faction"] = {lua_str(faction)}, ["ruleset"] = "PvE", }},\n'
            f'["items"] = {{\n{it}\n}}, }},\n')


def write_psv(root, snaps, pscans, meta=None, **kw):
    """SV file with queue (full snapshots) and pqueue (price scans)."""
    f = write_meta_sv(root, snaps, meta=meta, **kw) if meta is not None else write_sv(root, snaps, **kw)
    text = f.read_bytes().decode("utf-8")
    assert text.endswith("}\n")
    block = '["pqueue"] = {\n' + "".join(pscans) + "},\n"
    f.write_bytes((text[:-2] + block + "}\n").encode("utf-8"))
    return f


def now_ts(back=0):
    return int(time.time()) - back


def kinds(server):
    return [b.get("kind", "full") for b in server.bodies]


KIND_422 = (422, {"status": "error", "detail": "kind muss full oder prices sein"})


@pytest.fixture(autouse=True)
def server_knows_price_scans(monkeypatch):
    """GET /health is answered "snapshot_kinds: [full, prices]" unless a test asks the real
    function (FakeServer answers GET with an empty 200)."""
    calls = []

    def caps(url, **kw):
        calls.append(url)
        return True, "test"
    monkeypatch.setattr(U, "fetch_capabilities", caps)
    return calls


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def snapd(**over):
    s = {"v": 1, "kind": "prices", "seq": 3, "ts": 1000, "addon": "1.3.1",
         "market": {"client": "forever", "region": "EU", "realm": "R", "faction": "Horde", "ruleset": "PvE"},
         "items": {1: "5,100"}}
    s.update(over)
    return s


@pytest.mark.parametrize("value,ok", [
    ("5,100", True), ("0,1", True), ("12,480", True),
    ("5,0", False),                       # low must be > 0
    ("-1,5", False), ("5,-1", False), ("5", False), ("5,100,3", False), ("5,100,", False),
    (",100", False), ("5 ,100", False), ("1.5,100", False), ("٥,100", False),   # non-ASCII digit
    (str(2 ** 53 - 1) + ",1", True), (str(2 ** 53) + ",1", False), ("1," + str(2 ** 53), False),
    (5, False), (None, False),
])
def test_price_item_values(value, ok):
    p = U.valid_price_snapshot(snapd(items={1: value, 2: "1,1"}))
    assert (1 in p["items"]) is ok
    assert p["items"][2] == "1,1"


def test_price_snapshot_rules():
    assert U.valid_price_snapshot(snapd())["items"] == {1: "5,100"}
    assert U.valid_price_snapshot(snapd(items={"1": "5,100"})) is None          # string key
    assert U.valid_price_snapshot(snapd(items={0: "5,100"})) is None
    assert U.valid_price_snapshot(snapd(items={U.ITEM_ID_MAX + 1: "5,100"})) is None
    assert U.valid_price_snapshot(snapd(items={U.ITEM_ID_MAX: "5,100"})) is not None
    assert U.valid_price_snapshot(snapd(items={})) is None
    assert U.valid_price_snapshot(snapd(items=["5,100", "1,2"]))["items"] == {1: "5,100", 2: "1,2"}
    assert U.valid_price_snapshot(snapd(kind="full")) is None
    assert U.valid_price_snapshot({k: v for k, v in snapd().items() if k != "kind"}) is None
    assert U.valid_price_snapshot(snapd(v=2)) is None
    assert U.valid_price_snapshot(snapd(ts="1000")) is None
    assert U.valid_price_snapshot(snapd(ts=True)) is None
    assert U.valid_price_snapshot(snapd(market={"client": "forever", "region": "EU", "faction": "Horde"})) is None
    assert U.valid_price_snapshot(snapd(market={"client": "forever", "region": "EU", "realm": 5,
                                                "faction": "Horde"})) is None
    big = {i: "1,1" for i in range(1, U.PQ_MAX_ITEMS + 2)}
    assert U.valid_price_snapshot(snapd(items=big)) is None
    big.pop(U.PQ_MAX_ITEMS + 1)
    assert len(U.valid_price_snapshot(snapd(items=big))["items"]) == U.PQ_MAX_ITEMS
    assert U.valid_price_snapshot("x") is None


def test_payload_of_price_scan_and_full_unchanged():
    p = U.valid_price_snapshot(snapd(items={1: "5,100", 7: "0,3"}))
    b = U.payload(p, "a" * 32)
    assert b["kind"] == "prices" and b["snapshot"]["kind"] == "prices"
    assert b["snapshot"]["items"] == {"1": "5,100", "7": "0,3"} and b["snapshot"]["count"] == 2
    assert b["snapshot"]["market"] == {"client": "forever", "region": "EU", "realm": "R",
                                       "faction": "Horde", "ruleset": "PvE"}
    assert b["format"] == 1 and b["uploader"] == U.VERSION == "1.3.2"
    full = {"v": 1, "ts": 5, "market": p["market"], "items": {1: "1,1,5,1,1"}}
    fb = U.payload(full, "a" * 32)
    assert "kind" not in fb and "kind" not in fb["snapshot"]


def test_read_sv_pqueue(tmp_path):
    root = tmp_path / "wow"
    f = write_psv(root, [snap_lua(100, ITEMS_A)],
                  [pscan_lua(300, seq=9), pscan_lua(200, seq=8), pscan_lua(250, kind=None),
                   pscan_lua(260, items={5: "bad"}), pscan_lua(270, kind="full")])
    sv = U.read_sv(f)
    assert [s["ts"] for s in sv["queue"]] == [100]
    assert [p["ts"] for p in sv["pqueue"]] == [200, 300]                # sorted, invalid ones left out
    assert sv["pqueue"][0]["seq"] == 8 and sv["pqueue"][0]["items"] == PITEMS
    # opt-in off: nothing, also no price scans
    f = write_psv(root, [snap_lua(100, ITEMS_A)], [pscan_lua(300)], enabled=False)
    assert U.read_sv(f)["pqueue"] == [] and U.read_sv(f)["queue"] == []
    # pqueue without full snapshots is read
    f = write_psv(root, [], [pscan_lua(300)])
    assert len(U.read_sv(f)["pqueue"]) == 1


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #
def test_full_before_prices_and_sent_once(tmp_path, server):
    server.script = [OK_F, OK_F, OK_P, OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t + 60)], flavor="_forever_")
    write_psv(root, [snap_lua(t + 5, ITEMS_B, realm="Other")], [pscan_lua(t + 70, realm="Other")],
              flavor="_anniversary_")
    st = U.load_state()
    lines, log = logger()
    info = {}
    assert U.run_pass([root], U.Sender(server.url), st, log, info) == U.PASS_OK
    assert kinds(server) == ["full", "full", "prices", "prices"]       # full of every file first
    pb = server.bodies[2]
    assert pb["snapshot"]["kind"] == "prices" and pb["snapshot"]["items"] == {str(k): v for k, v in PITEMS.items()}
    assert pb["client_id"] == st["client_id"] and pb["snapshot"]["ts"] == t + 60
    assert info["prices_wait"] is None
    sent = st["sent"][server.url]
    assert "prices|forever|EU|Test Realm|Alliance|%d" % (t + 60) in sent
    assert any(l.startswith("sent price scan forever/EU/PvE/Alliance") for l in lines)
    assert any(l.startswith("sent full scan forever/EU/PvE/Alliance") for l in lines)
    assert st["last_upload_kind"] == "prices" and st["stats"]["last_prices"] and st["stats"]["last_full"]
    assert st["upload_markets"]["forever|EU|Test Realm|Alliance"]["kind"] == "prices"
    assert U.load_state()["last_upload_kind"] == "prices"
    # nothing new: nothing sent
    U.run_pass([root], U.Sender(server.url), st, log)
    assert len(server.bodies) == 4


def test_price_scan_superseded_by_full_or_newer_scan(tmp_path, server):
    server.script = [OK_F, OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    # same market: full snapshot at t -> price scans at t and before are superseded;
    # of two newer price scans only the newest goes out (server quota 12/h with the full ones)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t - 60), pscan_lua(t), pscan_lua(t + 30),
                                             pscan_lua(t + 90)])
    st = U.load_state()
    lines, log = logger()
    U.run_pass([root], U.Sender(server.url), st, log)
    assert kinds(server) == ["full", "prices"] and server.bodies[1]["snapshot"]["ts"] == t + 90
    for x in (t - 60, t, t + 30, t + 90):
        assert "prices|forever|EU|Test Realm|Alliance|%d" % x in st["sent"][server.url]
    assert any("3 older price scan(s) not sent" in l for l in lines)


def test_names_and_meta_ride_with_price_scans(tmp_path, server):
    server.script = [(201, {"status": "ok", "kind": "prices", "meta": 2})]
    root = tmp_path / "wow"
    write_psv(root, [], [pscan_lua(now_ts(600))], meta=META,
              names={1000: "Erz", 1001: "Kraut"}, quality={1000: 1, 1001: 2})
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    b = server.bodies[0]
    assert b["names"] == {"locale": "deDE", "items": {"1000": ["Erz", 1], "1001": ["Kraut", 2]}}
    assert b["meta"] == {str(k): v for k, v in META.items()}
    assert st["meta_sent"][server.url + "|forever"] == "1000-1002"
    assert st["names_sent"][server.url + "|deDE"] == "1000-1001"


def test_422_old_server_backoff_and_full_snapshots_go_on(tmp_path, server):
    server.script = [KIND_422]
    root = tmp_path / "wow"
    t = now_ts(3000)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    lines, log = logger()
    info = {}
    assert U.run_pass([root], U.Sender(server.url), st, log, info) == U.PASS_OK
    assert len(server.bodies) == 1
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t not in st["sent"][server.url]
    assert any("does not accept price scans yet" in l for l in lines)
    assert 3500 <= info["prices_wait"] <= 3600
    assert st["pqueue_not_before"] >= now_ts() + 3500
    assert U.load_state()["pqueue_tries"] == {"prices|forever|EU|Test Realm|Alliance|%d" % t: 1}
    # again right away: no request (no endless firing)
    info = {}
    U.run_pass([root], U.Sender(server.url), st, log, info)
    assert len(server.bodies) == 1 and info["prices_wait"] > 3000
    # a new full snapshot is sent at once although price scans are paused
    server.script = [OK_F]
    write_psv(root, [snap_lua(t + 100, ITEMS_A, realm="Other")], [pscan_lua(t)])
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_OK
    assert kinds(server) == ["prices", "full"]
    # after the pause: tried again; refused PQ_GIVE_UP times in all -> given up
    server.script = [KIND_422]
    for n in range(2, U.PQ_GIVE_UP + 1):
        st["pqueue_not_before"] = 0
        U.run_pass([root], U.Sender(server.url), st, log)
        assert len(server.bodies) == n + 1
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t in st["sent"][server.url]
    assert st["pqueue_tries"] == {}
    assert any("refused 3 times" in l for l in lines)
    st["pqueue_not_before"] = 0
    U.run_pass([root], U.Sender(server.url), st, log)
    assert len(server.bodies) == U.PQ_GIVE_UP + 1


def test_422_with_meta_tried_once_without(tmp_path, server):
    server.script = [(422, {}), OK_P]
    root = tmp_path / "wow"
    write_psv(root, [], [pscan_lua(now_ts(600))], meta=META)
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert "meta" in server.bodies[0] and "meta" not in server.bodies[1]
    assert st["last_upload_kind"] == "prices" and st["pqueue_tries"] == {}


def test_answer_without_kind_pauses_price_scans(tmp_path, server):
    server.script = [OK_F]                          # server before price scans: 201, no "kind"
    root = tmp_path / "wow"
    t = now_ts(3000)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t + 10), pscan_lua(t + 20, realm="Other")])
    st = U.load_state()
    lines, log = logger()
    info = {}
    assert U.run_pass([root], U.Sender(server.url), st, log, info) == U.PASS_OK
    assert kinds(server) == ["full", "prices"]                        # second scan not sent
    assert "prices|forever|EU|Test Realm|Alliance|%d" % (t + 10) in st["sent"][server.url]
    assert "prices|forever|EU|Other|Alliance|%d" % (t + 20) not in st["sent"][server.url]
    assert st["pqueue_caps"][server.url] == {"at": st["pqueue_caps"][server.url]["at"], "ok": False}
    assert any("did not confirm the price scan" in l for l in lines)
    assert st["pqueue_not_before"] >= now_ts() + U.PQ_UNCONFIRMED_WAIT - 100
    assert info["prices_wait"] > U.PQ_UNCONFIRMED_WAIT - 100
    assert st["last_upload_kind"] == "full" and st["stats"]["last_prices"] == 0
    U.run_pass([root], U.Sender(server.url), st, log)
    assert len(server.bodies) == 2


def test_retry_own_backoff_never_blocks_full(tmp_path, server):
    server.script = [(503, {}), (503, {})]
    root = tmp_path / "wow"
    t = now_ts(3000)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    info = {}
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1], info) == U.PASS_OK
    assert st["pqueue_backoff"] == 60 and info["prices_wait"] == 60
    st["pqueue_not_before"] = 0
    U.run_pass([root], U.Sender(server.url), st, logger()[1], info)
    assert st["pqueue_backoff"] == 120
    for _ in range(10):
        st["pqueue_not_before"] = 0
        U.run_pass([root], U.Sender(server.url), st, logger()[1], info)
    assert st["pqueue_backoff"] == U.MAX_BACKOFF
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t not in st["sent"][server.url]
    # success resets the backoff
    server.script = [OK_P]
    st["pqueue_not_before"] = 0
    U.run_pass([root], U.Sender(server.url), st, logger()[1], info)
    assert st["pqueue_backoff"] == 0 and info["prices_wait"] is None


def test_full_retry_holds_price_scans(tmp_path, server):
    server.script = [(503, {})]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t + 1)])
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_RETRY
    assert kinds(server) == ["full"]


def test_400_drops_price_scan(tmp_path, server):
    server.script = [(400, {})]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t in st["sent"][server.url]
    assert st["pqueue_not_before"] == 0


def test_redirect_is_config_error(tmp_path, server):
    server.script = [(302, {}, {"Location": "http://127.0.0.1:1/x"})]
    root = tmp_path / "wow"
    write_psv(root, [], [pscan_lua(now_ts(600))])
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_CONFIG


def test_too_old_or_future_price_scans_not_sent(tmp_path, server):
    root = tmp_path / "wow"
    write_psv(root, [], [pscan_lua(now_ts(8 * 86400)), pscan_lua(now_ts(-3600))])
    st = U.load_state()
    info = {}
    U.run_pass([root], U.Sender(server.url), st, logger()[1], info)
    assert server.bodies == [] and info["prices_wait"] is None


def test_outbox_price_scan(tmp_path):
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t + 1)])
    st = U.load_state()
    box = tmp_path / "box"
    U.run_pass([root], U.Sender("http://127.0.0.1:9/api/v1/snapshots", outbox=box), st, logger()[1])
    names = sorted(p.name for p in box.iterdir())
    assert names == [f"{t}_forever_EU_Alliance.json", f"prices_{t + 1}_forever_EU_Alliance.json"]
    assert st["last_upload_kind"] == "prices"


def test_watcher_retries_paused_price_scans_without_backoff(tmp_path, server, monkeypatch):
    server.script = [KIND_422]
    root = tmp_path / "wow"
    write_psv(root, [], [pscan_lua(now_ts(600))])
    w = U.Watcher([root], U.Sender(server.url), logger()[1])
    w.step()
    assert len(server.bodies) == 1
    assert w.backoff == 0 and w.pq_due is not None                    # full snapshots not slowed down
    assert w.step() == w.interval and len(server.bodies) == 1         # not due yet: no pass
    server.script = [OK_P]
    w.st["pqueue_not_before"] = 0
    w.pq_due = 0.0
    w.step()
    assert kinds(server) == ["prices", "prices"] and w.pq_due is None


# --------------------------------------------------------------------------- #
# state, window, tray
# --------------------------------------------------------------------------- #
def test_state_migration_and_types(own_appdata):
    d = own_appdata / "MidasUplink"
    d.mkdir()
    import json
    (d / "state.json").write_text(json.dumps({
        "stats": {"uploads": 4, "last": 1234}, "pqueue_not_before": "x", "pqueue_backoff": -5,
        "pqueue_tries": {"a": 1, "b": "2", "c": 99, "d": 2},
        "upload_markets": {"m": {"ts": 1, "at": 2, "kind": "prices"}, "n": {"ts": 1, "at": 2, "kind": "x"}}}),
        encoding="utf-8")
    st = U.load_state()
    assert st["stats"] == {"uploads": 4, "last": 1234, "last_full": 1234, "last_prices": 0}
    assert st["last_upload_kind"] == "full"                            # before price scans every upload was full
    assert st["pqueue_not_before"] == 0 and st["pqueue_backoff"] == 0
    assert st["pqueue_tries"] == {"a": 1, "d": 2}
    assert st["upload_markets"]["m"]["kind"] == "prices" and st["upload_markets"]["n"]["kind"] == "full"
    (d / "state.json").write_text(json.dumps({"pqueue_not_before": now_ts() + 40 * 86400,
                                              "pqueue_backoff": 10 ** 9}), encoding="utf-8")
    st = U.load_state()
    assert st["pqueue_not_before"] == 0 and st["pqueue_backoff"] == U.MAX_BACKOFF
    assert st["last_upload_kind"] is None


def test_status_and_tray_tell_kinds_apart(tmp_path, server, own_appdata):
    server.script = [OK_F, OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [snap_lua(t, ITEMS_A)], [pscan_lua(t + 5)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    s = T.build_status([root], st, server.url, T.SvCache())
    assert s["last_kind"] == "prices" and s["last_full"] and s["last_prices"]
    assert s["last_upload"] == st["stats"]["last"]
    up = st["upload_markets"]["forever|EU|Test Realm|Alliance"]
    assert up["kind"] == "prices"
    app = T.App(server.url, [], 30, lang="de")
    try:
        assert app.last_text().endswith("(Preis-Scan)")
        app.lang = "en"
        app.t = T.TEXT["en"]
        assert app.last_text().endswith("(price scan)")
    finally:
        for h in list(app.log.handlers):
            h.close()
            app.log.removeHandler(h)


def test_window_texts_name_both_kinds():
    js = (Path(U.__file__).parent / "ui" / "app.js").read_text(encoding="utf-8")
    for txt in ("voller Scan", "Preis-Scan", "full scan", "price scan", "last_kind", "upload_kind"):
        assert txt in js


# --------------------------------------------------------------------------- #
# review 19.11.: health check, 422 detail, quota, targets, duplicates
# --------------------------------------------------------------------------- #
def test_same_scan_in_two_files_sent_once(tmp_path, server):
    server.script = [OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t)], flavor="_forever_")
    write_psv(root, [], [pscan_lua(t)], flavor="_anniversary_")
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == 1


def test_422_kind_with_two_scans_one_request(tmp_path, server):
    server.script = [KIND_422]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t), pscan_lua(t + 5, realm="Other")])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == 1


def test_422_other_reason_dropped_and_next_sent(tmp_path, server):
    server.script = [(422, {"status": "error", "detail": "market.region unbekannt"}), OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t), pscan_lua(t + 5, realm="Other")])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == 2
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t in st["sent"][server.url]
    assert st["pqueue_not_before"] == 0 and st["pqueue_tries"] == {}


def test_duplicate_not_counted(tmp_path, server):
    server.script = [(200, {"status": "duplicate", "kind": "prices", "meta": 0})]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t in st["sent"][server.url]
    assert st["stats"]["uploads"] == 0 and st["last_upload_kind"] is None


def test_hourly_cap(tmp_path, server):
    server.script = [OK_P]
    root = tmp_path / "wow"
    t = now_ts(600)
    realms = [f"R{i}" for i in range(U.PQ_PER_HOUR + 2)]
    write_psv(root, [], [pscan_lua(t + i, realm=r) for i, r in enumerate(realms)])
    st = U.load_state()
    info = {}
    U.run_pass([root], U.Sender(server.url), st, logger()[1], info)
    assert len(server.bodies) == U.PQ_PER_HOUR
    assert 3500 < info["prices_wait"] <= 3600
    assert len(U.load_state()["pqueue_sent_at"]) == U.PQ_PER_HOUR
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == U.PQ_PER_HOUR
    st["pqueue_sent_at"] = [t - 4000] * U.PQ_PER_HOUR             # an hour later
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == U.PQ_PER_HOUR + 2


def test_pause_is_kept_per_target(tmp_path, server):
    server.script = [KIND_422]
    root = tmp_path / "wow"
    t = now_ts(600)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert st["pqueue_not_before"] > now_ts()
    box = tmp_path / "box"                                           # another target: not paused
    U.run_pass([root], U.Sender(server.url, outbox=box), st, logger()[1])
    assert len(list(box.iterdir())) == 1
    assert st["pqueue_target"] == "outbox:" + str(box) and st["pqueue_not_before"] == 0


def test_health_asked_once_per_6_hours(tmp_path, server, server_knows_price_scans):
    server.script = [OK_P]
    root = tmp_path / "wow"
    t = now_ts(900)
    write_psv(root, [], [pscan_lua(t)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert server_knows_price_scans == [server.url[: -len("snapshots")] + "health"]
    write_psv(root, [], [pscan_lua(t + 60)])
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server_knows_price_scans) == 1 and len(server.bodies) == 2
    st["pqueue_caps"][server.url]["at"] -= U.PQ_CAPS_EVERY
    write_psv(root, [], [pscan_lua(t + 120)])
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server_knows_price_scans) == 2


@pytest.mark.parametrize("ok,msg", [(False, "HTTP 200 without price scans"), (None, "HTTP 503")])
def test_server_without_price_scans_gets_none(tmp_path, server, monkeypatch, ok, msg):
    monkeypatch.setattr(U, "fetch_capabilities", lambda url, **kw: (ok, msg))
    server.script = [OK_F]
    root = tmp_path / "wow"
    t = now_ts(900)
    write_psv(root, [snap_lua(t, ITEMS_A, realm="Other")], [pscan_lua(t)])
    st = U.load_state()
    lines, log = logger()
    info = {}
    U.run_pass([root], U.Sender(server.url), st, log, info)
    assert kinds(server) == ["full"]                                  # full snapshots go on
    assert "prices|forever|EU|Test Realm|Alliance|%d" % t not in st["sent"][server.url]
    if ok is False:
        assert any("does not take price scans yet" in l for l in lines)
        assert info["prices_wait"] > U.PQ_CAPS_EVERY - 100 and st["pqueue_caps"][server.url]["ok"] is False
    else:
        assert any("cannot ask the server about price scans" in l for l in lines)
        assert info["prices_wait"] == 60 and server.url not in st["pqueue_caps"]


class HealthServer:
    def __init__(self, code, body):
        import http.server
        import threading
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                srv.paths.append(self.path)
                data = body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.paths = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/health"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("code,body,want", [
    (200, {"status": "ok", "snapshot_kinds": ["full", "prices"]}, True),
    (200, {"status": "ok", "version": "x"}, False),               # server before price scans
    (200, {"status": "ok", "snapshot_kinds": "prices"}, False),
    (200, b"not json", False),
    (404, {"detail": "Not Found"}, False),
    (503, {}, None), (429, {}, None),
    (302, {}, None),
])
def test_fetch_capabilities(code, body, want):
    hs = HealthServer(code, body)
    try:
        ok, _msg = REAL_CAPS(hs.url, timeout=5, deadline=10)
        assert ok is want
        assert hs.paths == ["/api/v1/health"]
    finally:
        hs.close()


REAL_CAPS = U.fetch_capabilities           # captured at import, before the autouse patch


def test_health_url():
    assert U.health_url("https://midasmarket.app/api/v1/snapshots") == "https://midasmarket.app/api/v1/health"
    assert U.health_url("http://127.0.0.1:8000/x") is None
