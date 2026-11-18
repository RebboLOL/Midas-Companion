"""Price history backfill (0.4.0, SPEC 5b): block from MidasUplinkDB.history, sent once."""
import json
from pathlib import Path

import uploader as U
from test_uploader import ITEMS_A, logger, snap_lua

HKEY = "forever|EU|CAMWOW EU5 Realm 7 PvE|Alliance"


def hist_lua(created=1794000000, day0=1786320000, items=None, realm="CAMWOW EU5 Realm 7 PvE"):
    items = items if items is not None else {5: "0:70:7;89:100:3", 6: "86:61:4"}
    it = "\n".join(f'[{k}] = "{v}",' for k, v in items.items())
    return (f'["{HKEY}"] = {{\n["v"] = 1, ["created"] = {created}, ["addon"] = "1.2.28",\n'
            f'["market"] = {{ ["client"] = "forever", ["region"] = "EU", ["realm"] = "{realm}",\n'
            f'["faction"] = "Alliance", ["ruleset"] = "PvE", }},\n'
            f'["day0"] = {day0}, ["days"] = 90, ["rows"] = 3, ["count"] = 2,\n'
            f'["items"] = {{\n{it}\n}},\n}},\n')


def write_sv(root, history, snaps=(), enabled=True):
    d = Path(root) / "_forever_" / "WTF" / "Account" / "ACC1" / "SavedVariables"
    d.mkdir(parents=True, exist_ok=True)
    text = ("\nMidasUplinkDB = {\n" f'["enabled"] = {"true" if enabled else "false"},\n'
            '["format"] = 1,\n["seq"] = 3,\n["queue"] = {\n' + "".join(snaps) + "},\n"
            '["history"] = {\n' + "".join(history) + "},\n"
            f'["histDone"] = {{ ["{HKEY}"] = 1794000000, }},\n'
            '["locale"] = "deDE",\n}\n')
    f = d / U.SV_NAME
    f.write_bytes(text.encode("utf-8"))
    (Path(root) / "_forever_" / "Interface" / "AddOns" / "Midas_Uplink").mkdir(parents=True, exist_ok=True)
    return f


def test_read_and_validate_block(tmp_path):
    f = write_sv(tmp_path, [hist_lua(items={5: "0:70:7", 6: "1:2", 0: "0:1:1", 7: "0:-1:1"})])
    sv = U.read_sv(f)
    assert sv["history"] == [{"created": 1794000000, "addon": "1.2.28", "day0": 1786320000,
                              "market": {"client": "forever", "region": "EU",
                                         "realm": "CAMWOW EU5 Realm 7 PvE", "faction": "Alliance",
                                         "ruleset": "PvE"},
                              "items": {"5": "0:70:7"}}]
    assert U.read_sv(write_sv(tmp_path, [hist_lua()], enabled=False))["history"] == []
    assert U.read_sv(write_sv(tmp_path, [hist_lua(day0=1786320001)]))["history"] == []
    assert U.read_sv(write_sv(tmp_path, [hist_lua(items={})]))["history"] == []


def test_urls():
    assert U.history_url("https://midasmarket.app/api/v1/snapshots") == "https://midasmarket.app/api/v1/history"
    assert U.history_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/v1/history"
    assert U.history_url("http://x/other") is None


def test_sent_once_after_snapshots(tmp_path, server):
    write_sv(tmp_path, [hist_lua()], snaps=[snap_lua(1794000100, ITEMS_A)])
    st = U.load_state()
    lines, log = logger()
    sender = U.Sender(server.url)
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_OK
    assert server.requests == [("POST", "/api/v1/snapshots"), ("POST", "/api/v1/history")]
    body = server.bodies[1]
    assert body == {"format": 1, "uploader": U.VERSION, "client_id": st["client_id"],
                    "history": {"v": 1, "addon": "1.2.28", "day0": 1786320000,
                                "market": body["history"]["market"],
                                "items": {"5": "0:70:7;89:100:3", "6": "86:61:4"}}}
    assert st["history_sent"][server.url] == [HKEY + "|1794000000"]
    # second pass (and after a restart): nothing again
    assert U.run_pass([tmp_path], sender, U.load_state(), log) == U.PASS_OK
    assert len(server.requests) == 2
    assert any("history forever/EU/PvE/Alliance 2 items - HTTP 201" in l for l in lines)


def test_retry_drop_and_unsupported(tmp_path, server):
    write_sv(tmp_path, [hist_lua()])
    st = U.load_state()
    _lines, log = logger()
    sender = U.Sender(server.url)
    server.script = [(503, {})]
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_RETRY
    assert st["history_sent"].get(server.url, []) == []
    server.script = [(404, {"detail": "Not Found"})]
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_OK
    assert st["history_not_before"] > 0 and st["history_sent"][server.url] == []
    n = len(server.requests)
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_OK
    assert len(server.requests) == n                    # waits 6 h
    st["history_not_before"] = 0
    server.script = [(422, {"detail": "x"})]
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_OK
    assert st["history_sent"][server.url] == [HKEY + "|1794000000"]   # invalid: never again
    server.script = [(200, {"status": "duplicate"})]
    write_sv(tmp_path, [hist_lua(created=1794000500)])
    assert U.run_pass([tmp_path], sender, st, log) == U.PASS_OK
    assert len(st["history_sent"][server.url]) == 2


def test_outbox_and_state_types(tmp_path):
    write_sv(tmp_path, [hist_lua()])
    st = U.load_state()
    _lines, log = logger()
    out = tmp_path / "out"
    assert U.run_pass([tmp_path], U.Sender("http://127.0.0.1:9/api/v1/snapshots", outbox=out), st, log) == U.PASS_OK
    files = sorted(p.name for p in out.iterdir())
    assert files == ["history_forever_EU_Alliance_1786320000.json"]
    assert json.loads((out / files[0]).read_text(encoding="utf-8"))["history"]["day0"] == 1786320000
    U.state_file().write_text(json.dumps({"history_sent": {"a": [1, "k"]}, "history_not_before": True}),
                              encoding="utf-8")
    st = U.load_state()
    assert st["history_sent"] == {"a": ["k"]} and st["history_not_before"] == 0
