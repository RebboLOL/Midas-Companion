import json
import os
from pathlib import Path

import pytest

import uploader as U


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def snap_lua(ts, items, realm="Test Realm", faction="Alliance"):
    it = "\n".join(f"[{i}] = {lua_str(v)}," for i, v in items.items())
    return (f'{{ ["v"] = 1, ["seq"] = 1, ["ts"] = {ts}, ["addon"] = "1.2.26", ["count"] = {len(items)},\n'
            f'["market"] = {{ ["client"] = "forever", ["region"] = "EU", ["realm"] = {lua_str(realm)},\n'
            f'["faction"] = {lua_str(faction)}, ["ruleset"] = "PvE", }},\n'
            f'["items"] = {{\n{it}\n}}, }}, -- [n]\n')


def write_sv(root, snaps, enabled=True, names=None, quality=None, locale="deDE", flavor="_forever_"):
    d = Path(root) / flavor / "WTF" / "Account" / "ACC1" / "SavedVariables"
    d.mkdir(parents=True, exist_ok=True)
    parts = ["\nMidasUplinkDB = {\n", f'["enabled"] = {"true" if enabled else "false"},\n',
             '["format"] = 1,\n["seq"] = 3,\n["queue"] = {\n', "".join(snaps), "},\n"]
    if names is not None:
        parts.append('["names"] = {\n' + "".join(f"[{k}] = {lua_str(v)},\n" for k, v in names.items()) + "},\n")
    if quality is not None:
        parts.append('["quality"] = {\n' + "".join(f"[{k}] = {v},\n" for k, v in quality.items()) + "},\n")
    if locale:
        parts.append(f'["locale"] = "{locale}",\n')
    parts.append("}\n")
    f = d / U.SV_NAME
    f.write_bytes("".join(parts).encode("utf-8"))
    # the old sub-addon is installed (1.3.0: a leftover Midas_Uplink.lua without it never counts)
    (Path(root) / flavor / "Interface" / "AddOns" / "Midas_Uplink").mkdir(parents=True, exist_ok=True)
    return f


ITEMS_A = {1000: "10,2,500,6,1,700,4,1", 1001: "3,1,99,3,1"}
ITEMS_B = {1000: "8,2,520,5,1,800,3,1", 1002: "1,1,12,1,1"}


def logger():
    lines = []
    return lines, lines.append


# --------------------------------------------------------------------------- #
# Lua SV parser
# --------------------------------------------------------------------------- #
def test_parser_escapes():
    t = r'X = { ["a"] = "q\"uote", ["b"] = "back\\slash", ["c"] = "line\nnext", ["d"] = "dec\065\066", ["e"] = ' + "'single'" + " }"
    v = U.parse_saved_variables(t)["X"]
    assert v == {"a": 'q"uote', "b": "back\\slash", "c": "line\nnext", "d": "decAB", "e": "single"}


def test_parser_utf8_bytes_and_byte_escapes():
    raw = 'X = { [1] = "Gro\\195\\159er Trank", [2] = "Gr\xc3\xbcn", [5] = 1 }'.encode("latin-1")
    v = U.parse_saved_variables_bytes(raw)["X"]
    assert v == {1: "Großer Trank", 2: "Grün", 5: 1}


def test_parser_nested_tables_and_lists():
    t = '''
    A = {
      ["queue"] = {
        { ["x"] = { ["y"] = { 1, 2, { ["z"] = true } } } }, -- [1]
        { ["x"] = false }, -- [2]
      },
      ["mixed"] = { 7, 8, ["k"] = "v" },
      ["empty"] = {},
    }
    B = nil
    '''
    sv = U.parse_saved_variables(t)
    q = sv["A"]["queue"]
    assert isinstance(q, list) and len(q) == 2
    assert q[0]["x"]["y"] == [1, 2, {"z": True}]
    assert q[1] == {"x": False}
    assert sv["A"]["mixed"] == {1: 7, 2: 8, "k": "v"}
    assert sv["A"]["empty"] == []
    assert sv["B"] is None


def test_parser_numbers():
    sv = U.parse_saved_variables("N = { -5, -0.25, 1e3, 0x1F, 12345678901, -2e-2 }")
    assert sv["N"] == [-5, -0.25, 1000.0, 31, 12345678901, -0.02]
    assert isinstance(sv["N"][0], int)


def test_parser_comments():
    t = '''-- header
    --[[ block
    comment ]]
    X = { -- trailing
      ["a"] = 1, -- [1]
      --[[ inline ]] ["b"] = "--not a comment",
    }
    '''
    assert U.parse_saved_variables(t)["X"] == {"a": 1, "b": "--not a comment"}


def test_parser_errors():
    with pytest.raises(U.LuaParseError):
        U.parse_saved_variables('X = { ["a"] = "open')
    with pytest.raises(U.LuaParseError):
        U.parse_saved_variables("X = { ")


# --------------------------------------------------------------------------- #
# Opt-in + queue
# --------------------------------------------------------------------------- #
def test_opt_in_off_sends_nothing(tmp_path):
    f = write_sv(tmp_path / "wow", [snap_lua(100, ITEMS_A)], enabled=False)
    assert U.read_queue(f) == []
    out = tmp_path / "out"
    lines, log = logger()
    assert U.PASS_OK == U.run_pass([tmp_path / "wow"], U.Sender("x", out), U.load_state(), log)
    assert not out.exists()


def test_opt_in_on_reads_queue(tmp_path):
    f = write_sv(tmp_path / "wow", [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)])
    q = U.read_queue(f)
    assert [s["ts"] for s in q] == [100, 200]
    assert q[0]["items"][1000] == ITEMS_A[1000]


def test_invalid_snapshot_skipped(tmp_path):
    bad = '{ ["v"] = 1, ["ts"] = 5, ["market"] = { ["client"] = "era", ["region"] = "EU" }, ["items"] = {} },\n'
    f = write_sv(tmp_path / "wow", [bad, snap_lua(100, ITEMS_A)])
    assert [s["ts"] for s in U.read_queue(f)] == [100]


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #
def test_dedup_across_passes_and_restart(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)])
    lines, log = logger()
    st = U.load_state()
    assert U.PASS_OK == U.run_pass([root], U.Sender(server.url), st, log)
    assert len(server.bodies) == 2
    assert U.PASS_OK == U.run_pass([root], U.Sender(server.url), st, log)
    assert len(server.bodies) == 2
    st2 = U.load_state()                       # restart: state from disk
    assert st2["client_id"] == st["client_id"]
    U.run_pass([root], U.Sender(server.url), st2, log)
    assert len(server.bodies) == 2
    write_sv(root, [snap_lua(200, ITEMS_B), snap_lua(300, ITEMS_A)])
    U.run_pass([root], U.Sender(server.url), st2, log)
    assert [b["snapshot"]["ts"] for b in server.bodies] == [100, 200, 300]
    assert st2["stats"]["uploads"] == 3


def test_state_lives_in_appdata(own_appdata):
    st = U.load_state()
    U.save_state(st)
    assert (own_appdata / "MidasUplink" / "state.json").is_file()
    assert len(st["client_id"]) == 32


def test_payload_shape(tmp_path, server):
    write_sv(tmp_path / "wow", [snap_lua(100, ITEMS_A)])
    U.run_pass([tmp_path / "wow"], U.Sender(server.url), U.load_state(), logger()[1])
    b = server.bodies[0]
    assert b["format"] == 1 and b["uploader"] == U.VERSION and len(b["client_id"]) == 32
    s = b["snapshot"]
    assert s["ts"] == 100 and s["addon"] == "1.2.26" and s["count"] == 2
    assert s["market"] == {"client": "forever", "region": "EU", "realm": "Test Realm",
                           "faction": "Alliance", "ruleset": "PvE"}
    assert s["items"] == {str(k): v for k, v in ITEMS_A.items()}


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
NAMES = {1000: "Großer Heiltrank", 1001: 'Erz "Eisen"', 1002: "Seide", 9999: "Not in any snapshot"}
QUAL = {1000: 1, 1001: 2, 1002: 0, 9999: 4}


def test_names_sent_once_per_url_and_locale(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)], names=NAMES, quality=QUAL)
    st = U.load_state()
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    b1, b2 = server.bodies
    assert b1["names"] == {"locale": "deDE", "items": {"1000": ["Großer Heiltrank", 1], "1001": ['Erz "Eisen"', 2]}}
    assert b2["names"] == {"locale": "deDE", "items": {"1002": ["Seide", 0]}}   # 1000 already sent
    key = U.names_key(server.url, "deDE")
    assert st["names_sent"][key] == "1000-1002"
    # new snapshot with known items only -> no names field
    write_sv(root, [snap_lua(300, ITEMS_A)], names=NAMES, quality=QUAL)
    U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1])
    assert "names" not in server.bodies[2]
    # other locale -> names again
    write_sv(root, [snap_lua(400, ITEMS_A)], names=NAMES, quality=QUAL, locale="enUS")
    U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1])
    assert server.bodies[3]["names"]["locale"] == "enUS"
    assert set(server.bodies[3]["names"]["items"]) == {"1000", "1001"}


def test_names_other_url_sent_again(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)], names=NAMES, quality=QUAL)
    st = U.load_state()
    st["names_sent"][U.names_key("http://elsewhere/api", "deDE")] = "1000-1001"
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert set(server.bodies[0]["names"]["items"]) == {"1000", "1001"}


def test_names_need_quality_and_clean_text(tmp_path):
    s = {"items": {1: "x", 2: "x", 3: "x", 4: "x", 5: "x", 6: "x"}}
    sv = {"locale": "deDE",
          "names": {1: "Ok", 2: "No quality", 3: "Bad\x01ctrl", 4: "x" * 121, 5: "Q9", 6: "Already"},
          "quality": {1: 3, 3: 1, 4: 1, 5: 9, 6: 1}}
    assert U.names_for(s, sv, {6}) == {"locale": "deDE", "items": {"1": ["Ok", 3]}}
    assert U.names_for(s, dict(sv, locale=None), set()) is None
    assert U.names_for({"items": {7: "x"}}, sv, set()) is None


def test_names_marked_only_after_2xx(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)], names=NAMES, quality=QUAL)
    server.script = [(503, {}), (201, {"status": "ok"})]
    st = U.load_state()
    assert U.PASS_RETRY == U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert st["names_sent"] == {} and not any(st["sent"].values())
    assert U.PASS_OK == U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert "names" in server.bodies[1]
    assert st["names_sent"][U.names_key(server.url, "deDE")] == "1000-1001"


def test_names_not_marked_on_drop(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)], names=NAMES, quality=QUAL)
    server.script = [(422, {"detail": "bad"})]
    st = U.load_state()
    assert U.PASS_OK == U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert sum(map(len, st["sent"].values())) == 1                   # snapshot dropped for good
    assert st["names_sent"] == {}                 # names go with the next snapshot
    assert st["stats"]["uploads"] == 0


def test_id_codec():
    ids = {1, 2, 3, 7, 9, 10, 500000}
    txt = U.encode_ids(ids)
    assert txt == "1-3,7,9-10,500000"
    assert U.decode_ids(txt) == ids
    assert U.decode_ids("") == set() and U.encode_ids([]) == ""


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_retry_codes_keep_snapshot(tmp_path, server, code):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)])
    server.script = [(code, {}), (201, {"status": "ok"})]
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_RETRY
    assert not any(st["sent"].values()) and len(server.bodies) == 1     # stopped after first failure
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert [b["snapshot"]["ts"] for b in server.bodies] == [100, 100, 200]
    assert sum(map(len, st["sent"].values())) == 2


@pytest.mark.parametrize("code", [400, 413, 422])
def test_drop_codes_discard(tmp_path, server, code):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A), snap_lua(200, ITEMS_B)])
    server.script = [(code, {}), (201, {"status": "ok"})]
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert sum(map(len, st["sent"].values())) == 2 and len(server.bodies) == 2
    U.run_pass([root], U.Sender(server.url), st, logger()[1])
    assert len(server.bodies) == 2                         # dropped one never comes back


def test_duplicate_200_counts_as_sent(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)], names=NAMES, quality=QUAL)
    server.script = [(200, {"status": "duplicate"})]
    st = U.load_state()
    lines, log = logger()
    assert U.PASS_OK == U.run_pass([root], U.Sender(server.url), st, log)
    assert sum(map(len, st["sent"].values())) == 1
    assert st["names_sent"][U.names_key(server.url, "deDE")] == "1000-1001"
    assert "duplicate" in lines[-1]


def test_unreachable_server_retries(tmp_path):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)])
    st = U.load_state()
    s = U.Sender("http://127.0.0.1:9/api/v1/snapshots", timeout=2)
    assert U.run_pass([root], s, st, logger()[1]) == U.PASS_RETRY
    assert not any(st["sent"].values())


def test_watcher_backoff_and_force(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)])
    server.script = [(503, {}), (201, {"status": "ok"})]
    w = U.Watcher([root], U.Sender(server.url), logger()[1], interval=30)
    assert w.step() == 60 and w.backoff == 60          # first failure: 60 s
    assert w.step() == 30 and w.backoff == 0           # retry due -> sent
    assert len(server.bodies) == 2
    assert w.step() == 30 and len(server.bodies) == 2  # nothing changed
    w.step(force=True)                                 # forced pass, nothing new
    assert len(server.bodies) == 2


def test_outbox_mode(tmp_path):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)], names=NAMES, quality=QUAL)
    out = tmp_path / "out"
    st = U.load_state()
    U.run_pass([root], U.Sender("unused", out), st, logger()[1])
    files = list(out.glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["names"]["items"]["1000"] == ["Großer Heiltrank", 1]
