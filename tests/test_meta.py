"""Companion 1.3.1, Datalink addendum 18.11.2026: item data from the game
(MidasUplinkDB.meta) rides with the snapshot upload, once per URL.
Every test uses its own APPDATA (conftest) and a local fake server."""
import gzip
import json
import random

import pytest

import uploader as U
from test_uploader import ITEMS_A, ITEMS_B, logger, snap_lua, write_sv

META = {1000: "134830:0:1:0:45:35::20:250", 1001: "133217:4:2:1:60:55:INVTYPE_HEAD:1:12345",
        1002: "132889:7:5:0:1:0::20:0"}


def write_meta_sv(root, snaps, meta=META, meta_v=1, **kw):
    f = write_sv(root, snaps, **kw)
    text = f.read_bytes().decode("utf-8")
    assert text.endswith("}\n")
    block = '["meta"] = {\n' + "".join(f'[{k}] = "{v}",\n' for k, v in meta.items()) + "},\n"
    if meta_v is not None:
        block += f'["metaV"] = {meta_v},\n'
    f.write_bytes((text[:-2] + block + "}\n").encode("utf-8"))
    return f


@pytest.mark.parametrize("v,ok", [
    ("134830:0:1:0:45:35::20:250", True),
    ("0:0:0:0:0:0:INVTYPE_2HWEAPON:1:0", True),
    ("1:2:3:4:5:6:" + "A" * 30 + ":7:8", True),
    ("1:2:3:4:5:6:" + "A" * 31 + ":7:8", False),
    ("1:2:3:4:5:6::7", False),                       # 8 fields
    ("1:2:3:4:5:6::7:8:9", False),                   # 10 fields
    ("-1:2:3:4:5:6::7:8", False),
    ("1.5:2:3:4:5:6::7:8", False),
    ("1:2:3:4:5:6:invtype_head:7:8", False),
    ("1:2:3:4:5:6:INV TYPE:7:8", False),
    (":2:3:4:5:6::7:8", False),
    ("12345678901:2:3:4:5:6::7:8", False),
    ("2147483647:2:3:4:5:6::7:2147483647", True),
    ("2147483648:2:3:4:5:6::7:8", False), ("1:2:3:4:5:6::7:2147483648", False),
    ("١:2:3:4:5:6::7:8", False),                     # non-ASCII digit
    (None, False), (5, False), ("", False),
])
def test_meta_format(v, ok):
    assert U.valid_meta(v) is ok


ACK = (201, {"status": "ok", "meta": 2})        # server that knows the field


def test_meta_sent_once_per_url_only_snapshot_items(tmp_path, server):
    server.script = [ACK]
    root = tmp_path / "wow"
    meta = {**META, 1002: "bad", 4711: "1:1:1:1:1:1::1:1"}            # 4711 is in no snapshot
    write_meta_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)], meta=meta)
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    b1, b2 = server.bodies
    assert b1["meta"] == {"1000": META[1000], "1001": META[1001]}
    assert "meta" not in b2                                           # 1000 sent, 1002 invalid
    assert st["meta_sent"] == {server.url + "|forever": "1000-1001"}
    assert U.load_state()["meta_sent"] == {server.url + "|forever": "1000-1001"}
    # the addon learned 1002 meanwhile: only that one goes out
    write_meta_sv(root, [snap_lua(300, ITEMS_B)])
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert server.bodies[2]["meta"] == {"1002": META[1002]}
    assert st["meta_sent"][server.url + "|forever"] == "1000-1002"
    # language change does not send it again (one list per URL, not per locale)
    write_meta_sv(root, [snap_lua(400, ITEMS_A)], locale="enUS")
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert "meta" not in server.bodies[3]


def test_meta_other_url_sent_again(tmp_path, server):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    server.script = [ACK]
    st = U.load_state()
    st["meta_sent"]["http://elsewhere/api|forever"] = "1000-1001"
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert set(server.bodies[0]["meta"]) == {"1000", "1001"}


def test_meta_marked_only_after_2xx(tmp_path, server):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    st = U.load_state()
    server.script = [(503, {}), ACK]
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_RETRY
    assert st["meta_sent"] == {} and not any(st["sent"].values())
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert "meta" in server.bodies[1] and st["meta_sent"][server.url + "|forever"] == "1000-1001"


@pytest.mark.parametrize("code", [400, 413, 422])
def test_refused_with_meta_is_sent_once_more_without(tmp_path, server, code):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)])
    st = U.load_state()
    server.script = [(code, {"detail": "x"}), (201, {"status": "ok"}), (code, {"detail": "x"}),
                     (code, {"detail": "x"})]
    lines, log = logger()
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_OK
    assert ["meta" in b for b in server.bodies] == [True, False, True, False]
    assert server.bodies[1]["snapshot"] == server.bodies[0]["snapshot"]
    assert st["meta_sent"] == {}                                      # never marked without a 2xx with meta
    assert st["stats"]["uploads"] == 1 and len(st["sent"][server.url]) == 2    # second one dropped for good
    assert "without item data" in lines[0]


def test_meta_needs_opt_in_and_layout_version(tmp_path, server):
    root = tmp_path / "wow"
    f = write_meta_sv(root, [snap_lua(100, ITEMS_A)], enabled=False)
    assert U.read_sv(f)["meta"] == {} and U.read_sv(f)["queue"] == []
    for v in (2, None):
        f = write_meta_sv(root, [snap_lua(100, ITEMS_A)], meta_v=v)
        assert U.read_sv(f)["meta"] == {}
    U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1])
    assert "meta" not in server.bodies[0]
    assert U.meta_for({"items": {"x": "1", 0: "1", 1000: "1"}}, {"meta": {0: META[1000], 1000: META[1000]}},
                      set()) == {"1000": META[1000]}
    assert U.meta_for({"items": {1000: "1"}}, {"meta": {1000: META[1000]}}, {1000}) is None


@pytest.mark.parametrize("reply,marked", [
    ({"status": "ok"}, False),                       # today's server: field unknown
    ({"status": "duplicate"}, False),
    ({"status": "ok", "meta": 0}, False),            # took none (parallel duplicate, all invalid)
    ({"status": "ok", "meta": 2}, True),
    ({"status": "duplicate", "meta": 1}, True),      # duplicate snapshot, item data still taken
    ({"status": "ok", "quarantined": True, "meta": 0}, False),
    ({"status": "ok", "quarantined": True, "meta": 2}, False),
    ({"status": "ok", "quarantined": False, "meta": 2}, True),
    ({"status": "ok", "meta": -1}, False), ({"status": "ok", "meta": "2"}, False),
    ({"status": "ok", "meta": True}, False), ({"status": "ok", "meta": 1.0}, False),
    ({"status": "ok", "meta": None}, False), ([1], False),
])
def test_meta_marked_only_when_server_confirms(tmp_path, server, reply, marked):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_A)])
    st = U.load_state()
    server.script = [(201, reply)]
    lines, log = logger()
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_OK
    assert st["stats"]["uploads"] == 2 and len(st["sent"][server.url]) == 2      # snapshots are done
    if marked:
        assert st["meta_sent"] == {server.url + "|forever": "1000-1001"}
        assert ["meta" in b for b in server.bodies] == [True, False]
        assert "not confirmed" not in lines[0]
    else:
        assert st["meta_sent"] == {}
        assert ["meta" in b for b in server.bodies] == [True, True]            # sent again
        assert "not confirmed by the server yet" in lines[0] or "sent again next time" in lines[0]


def test_meta_arrives_after_server_update(tmp_path, server):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])                     # old server
    assert st["meta_sent"] == {}
    server.script = [ACK]                                                         # server deployed
    write_meta_sv(root, [snap_lua(300, ITEMS_A)])
    U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1])
    assert server.bodies[1]["meta"] == server.bodies[0]["meta"]
    assert U.load_state()["meta_sent"] == {server.url + "|forever": "1000-1001"}


def test_non_json_2xx_answer_marks_nothing():
    class S:
        last_reply = None
    assert U.meta_acknowledged(S()) is False and U.meta_acknowledged(object()) is False


def test_meta_outbox(tmp_path):
    root, out = tmp_path / "wow", tmp_path / "outbox"
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    st = U.load_state()
    sender = U.Sender("http://127.0.0.1:9/api/v1/snapshots", outbox=out)
    assert U.run_pass([root], sender, st, logger()[1]) == U.PASS_OK
    body = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert body["meta"] == {"1000": META[1000], "1001": META[1001]}
    assert st["meta_sent"] == {f"outbox:{out}|forever": "1000-1001"}


def test_meta_state_type_checked(own_appdata):
    U.state_file().parent.mkdir(parents=True, exist_ok=True)
    U.state_file().write_text(json.dumps({"meta_sent": {
        "http://x/api|forever": "1-3", "http://x/api|era": "7", "b|era": 5, "c|era": ["x"],
        "http://x/api": "1-9",                      # first layout (no client): dropped
        "http://x/api|retail": "1", "|era": "1"}}), encoding="utf-8")
    assert U.load_state()["meta_sent"] == {"http://x/api|forever": "1-3", "http://x/api|era": "7"}
    U.state_file().write_text(json.dumps({"meta_sent": [1]}), encoding="utf-8")
    assert U.load_state()["meta_sent"] == {}


def test_body_size_7000_items_with_names_and_meta(capsys):
    """Server limit: 2 MB gzip / 10 MB unpacked per snapshot upload."""
    rnd = random.Random(7)
    slots = ["", "", "", "INVTYPE_HEAD", "INVTYPE_2HWEAPON", "INVTYPE_ROBE", "INVTYPE_WEAPONMAINHAND"]
    items, names, qual, meta = {}, {}, {}, {}
    for i in range(7000):
        iid = 2000 + i * 37
        lv = []
        p = rnd.randint(50, 500000)
        for _ in range(rnd.randint(4, 20)):
            p += rnd.randint(1, 5000)
            lv += [p, rnd.randint(1, 200), rnd.randint(1, 5)]
        items[iid] = f"{rnd.randint(1, 5000)},{rnd.randint(1, 60)}," + ",".join(map(str, lv))
        names[iid] = f"Gegenstand der Prüfung Nummer {iid} des Adlers"
        qual[iid] = rnd.randint(0, 4)
        meta[iid] = (f"{rnd.randint(132000, 5999999)}:{rnd.randint(0, 15)}:{rnd.randint(0, 20)}:"
                     f"{rnd.randint(0, 4)}:{rnd.randint(1, 90)}:{rnd.randint(0, 60)}:{rnd.choice(slots)}:"
                     f"{rnd.choice([1, 5, 20, 200])}:{rnd.randint(0, 250000)}")
    s = {"ts": 1, "addon": "1.3.0", "items": items,
         "market": {"client": "forever", "region": "EU", "realm": "R", "faction": "Alliance", "ruleset": "PvE"}}
    sv = {"names": names, "quality": qual, "locale": "deDE", "meta": meta}
    sizes = {}
    for label, n, m in (("plain", None, None), ("names", U.names_for(s, sv, set()), None),
                        ("names+meta", U.names_for(s, sv, set()), U.meta_for(s, sv, set()))):
        raw = json.dumps(U.payload(s, "0" * 32, n, m), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sizes[label] = (len(raw), len(gzip.compress(raw)))
    with capsys.disabled():
        print("\n7,000 items (raw, gzip):", sizes)
    assert len(U.meta_for(s, sv, set())) == 7000
    assert sizes["names+meta"][1] < 2 * 1024 * 1024 and sizes["names+meta"][0] < 10 * 1024 * 1024


def test_realm_name_rejects_invisible_format_characters():
    for bad in ("‮evil", "Zero​Width", "A‍B", "﻿Realm", "Soft­Hyphen"):
        assert not U.valid_realm_name(bad), repr(bad)
        with pytest.raises(ValueError, match="realm"):
            U.clean_manual_market({"flavor": "_classic_era_", "region": "EU", "realm": bad, "faction": "Horde"})
    for good in ("Nek'Rosh Test", "Ünterwelt", "Пламегор", "아즈샤라", "\U0001F600 Emoji"):
        assert U.valid_realm_name(good), good


def test_meta_kept_per_game_client(tmp_path, server):
    """The server stores item data per flavor: Forever and Era each deliver theirs."""
    root = tmp_path / "wow"
    server.script = [ACK]
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    era = snap_lua(200, ITEMS_A).replace('"forever"', '"era"')
    write_meta_sv(root, [era], flavor="_classic_era_")
    st = U.load_state()
    st["meta_sent"][server.url] = "1-99999"                     # old key without client: no effect
    lines, log = logger()
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_OK
    assert sorted(b["snapshot"]["market"]["client"] for b in server.bodies) == ["era", "forever"]
    assert all(b["meta"] == {"1000": META[1000], "1001": META[1001]} for b in server.bodies)
    assert st["meta_sent"][server.url + "|forever"] == st["meta_sent"][server.url + "|era"] == "1000-1001"
    assert set(U.load_state()["meta_sent"]) == {server.url + "|forever", server.url + "|era"}
    assert all("2 item data (server took 2)" in x for x in lines)
    assert U.meta_key("t", "forever") == "t|forever" and U.meta_key("t", "retail") is None
    assert U.meta_key("t", None) is None


@pytest.mark.parametrize("reply,text", [
    ({"status": "ok", "meta": 1}, "2 item data (server took 1)"),          # all sent ids count as done
    ({"status": "ok", "meta": 0}, "2 item data (server took 0, sent again next time)"),
    ({"status": "ok", "quarantined": True, "meta": 0}, "(server took 0, sent again next time)"),
    ({"status": "ok"}, "2 item data (not confirmed by the server yet)"),
])
def test_log_names_what_the_server_took(tmp_path, server, reply, text):
    root = tmp_path / "wow"
    write_meta_sv(root, [snap_lua(100, ITEMS_A)])
    server.script = [(201, reply)]
    lines, log = logger()
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, log)
    assert text in lines[0], lines[0]
    assert (st["meta_sent"] == {server.url + "|forever": "1000-1001"}) is (reply.get("meta") == 1)
