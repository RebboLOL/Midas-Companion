import json
import os

import pytest

import uploader as U
from test_uploader import ITEMS_A, ITEMS_B, logger, snap_lua, write_sv


# --------------------------------------------------------------------------- #
# 1. unreadable SV file -> retry with backoff, other files still processed
# --------------------------------------------------------------------------- #
def test_broken_sv_retried_other_files_sent(tmp_path, server):
    root = tmp_path / "wow"
    bad = write_sv(root, [snap_lua(100, ITEMS_A)], flavor="_forever_")
    bad.write_text("MidasUplinkDB = { [\"queue\"] = { { ", encoding="utf-8")   # half written
    write_sv(root, [snap_lua(200, ITEMS_B)], flavor="_classic_era_")
    lines, log = logger()
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_RETRY
    assert [b["snapshot"]["ts"] for b in server.bodies] == [200]
    assert any("retrying later" in l for l in lines)


def test_all_flavors_found(tmp_path, server):
    root = tmp_path / "wow"
    assert "_anniversary_" in U.FLAVORS
    for i, fl in enumerate(("_classic_era_", "_forever_", "_anniversary_", "_classic_era_ptr_")):
        write_sv(root, [snap_lua(100 + i, ITEMS_A, realm=f"R{i}")], flavor=fl)
    assert len(list(U.sv_files([root]))) == 4
    assert U.run_pass([root], U.Sender(server.url), U.load_state(), logger()[1]) == U.PASS_OK
    assert sorted(b["snapshot"]["ts"] for b in server.bodies) == [100, 101, 102, 103]


def test_watcher_rereads_broken_file_without_mtime_change(tmp_path, server):
    root = tmp_path / "wow"
    f = write_sv(root, [snap_lua(100, ITEMS_A)])
    good = f.read_bytes()
    st0 = f.stat()
    f.write_text("MidasUplinkDB = { ", encoding="utf-8")
    os.utime(f, ns=(st0.st_atime_ns, st0.st_mtime_ns))
    w = U.Watcher([root], U.Sender(server.url), logger()[1], interval=30)
    assert w.step() == 60 and w.backoff == 60
    assert server.bodies == []
    f.write_bytes(good)                                   # WoW finished writing,
    os.utime(f, ns=(st0.st_atime_ns, st0.st_mtime_ns))    # same mtime as before
    assert w.step() == 30 and w.backoff == 0              # backoff forces a re-read
    assert [b["snapshot"]["ts"] for b in server.bodies] == [100]


# --------------------------------------------------------------------------- #
# 4. redirects: never followed, reported as configuration error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_config_error(tmp_path, server, code):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)])
    server.script = [(code, {}, {"Location": "/elsewhere"})]
    lines, log = logger()
    st = U.load_state()
    assert U.run_pass([root], U.Sender(server.url), st, log) == U.PASS_CONFIG
    assert server.requests == [("POST", "/api/v1/snapshots")]        # no GET follow-up
    assert not any(st["sent"].values())
    assert any("CONFIGURATION ERROR" in l for l in lines)
    assert any("/elsewhere" in l for l in lines)


def test_redirect_backs_off_to_maximum(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)])
    server.script = [(301, {}, {"Location": "https://other/"})]
    w = U.Watcher([root], U.Sender(server.url), logger()[1], interval=30)
    assert w.step() == U.MAX_BACKOFF
    assert w.last_result == U.PASS_CONFIG


# --------------------------------------------------------------------------- #
# 5. state robustness
# --------------------------------------------------------------------------- #
def test_corrupt_state_kept_aside(own_appdata):
    d = own_appdata / "MidasUplink"
    d.mkdir()
    (d / "state.json").write_text("{ not json", encoding="utf-8")
    st = U.load_state()
    assert len(st["client_id"]) == 32
    kept = list(d.glob("state_corrupt_*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{ not json"
    assert not (d / "state.json").exists()


def test_state_wrong_types_repaired(own_appdata):
    d = own_appdata / "MidasUplink"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({
        "client_id": 123, "sent": "x", "names_sent": [1], "stats": {"uploads": "7", "last": True}}),
        encoding="utf-8")
    st = U.load_state()
    assert isinstance(st["client_id"], str) and len(st["client_id"]) == 32
    assert st["sent"] == {} and st["names_sent"] == {}
    assert st["stats"] == {"uploads": 0, "last": 0, "last_full": 0, "last_prices": 0}
    (d / "state.json").write_text(json.dumps([1, 2]), encoding="utf-8")   # not an object
    U.load_state()
    assert len(list(d.glob("state_corrupt_*.json"))) == 1


def test_state_valid_is_kept(own_appdata):
    st = U.load_state()
    U.sent_list(st, "u").append("k1")
    st["names_sent"]["u|deDE"] = "1-3"
    assert U.save_state(st)
    st2 = U.load_state()
    assert st2["client_id"] == st["client_id"] and st2["sent"] == {"u": ["k1"]}
    assert st2["names_sent"] == {"u|deDE": "1-3"}


def test_save_state_retries_locked_file(own_appdata, monkeypatch):
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    real = os.replace
    fails = {"n": 2}

    def flaky(a, b):
        if fails["n"]:
            fails["n"] -= 1
            raise PermissionError("locked")
        return real(a, b)

    monkeypatch.setattr(U.os, "replace", flaky)
    st = U.load_state()
    assert U.save_state(st) is True
    assert (own_appdata / "MidasUplink" / "state.json").is_file()


def test_save_state_gives_up_without_crash(own_appdata, monkeypatch):
    monkeypatch.setattr(U.time, "sleep", lambda s: None)

    def locked(a, b):
        raise PermissionError("locked")

    monkeypatch.setattr(U.os, "replace", locked)
    assert U.save_state(U.load_state()) is False


# --------------------------------------------------------------------------- #
# 8. sent keys per target, old keys belong to the local default URL
# --------------------------------------------------------------------------- #
def test_old_sent_list_migrates_to_default_url(own_appdata):
    d = own_appdata / "MidasUplink"
    d.mkdir()
    cid = "ab" * 16
    (d / "state.json").write_text(json.dumps({"client_id": cid, "sent": ["k1", "k2"]}), encoding="utf-8")
    st = U.load_state()
    assert st["client_id"] == cid
    assert st["sent"] == {U.LOCAL_URL: ["k1", "k2"]}         # 0.1.0 only knew the local server
    assert U.LOCAL_URL.startswith("http://127.0.0.1:8000/")


def test_guard_blocks_real_server():
    body = U.payload({"ts": 1, "market": {"client": "era", "region": "EU", "realm": "R", "faction": "Horde"},
                      "items": {}}, "0" * 32)
    with pytest.raises(AssertionError, match="tried to reach"):
        U.Sender(U.DEFAULT_URL).send(body)


def test_default_url_and_overrides(monkeypatch):
    assert U.DEFAULT_URL == "https://midasmarket.app/api/v1/snapshots"
    seen = {}

    class Stop(Exception):
        pass

    def fake_watcher(roots, sender, log, interval):
        seen["url"] = sender.url
        raise Stop

    monkeypatch.setattr(U, "Watcher", fake_watcher)
    monkeypatch.setattr(U, "wow_roots", lambda extra: [object()])
    monkeypatch.delenv("MIDAS_UPLINK_URL", raising=False)
    for argv, env, want in ((["--once"], None, U.DEFAULT_URL),
                            (["--once"], "http://127.0.0.1:1/x", "http://127.0.0.1:1/x"),
                            (["--once", "--url", "http://127.0.0.1:2/y"], "http://127.0.0.1:1/x",
                             "http://127.0.0.1:2/y")):
        if env:
            monkeypatch.setenv("MIDAS_UPLINK_URL", env)
        with pytest.raises(Stop):
            U.main(argv)
        assert seen["url"] == want


def test_sent_is_per_target(tmp_path, server):
    root = tmp_path / "wow"
    write_sv(root, [snap_lua(100, ITEMS_A)])
    st = U.load_state()
    out = tmp_path / "out"
    assert U.run_pass([root], U.Sender("unused", out), st, logger()[1]) == U.PASS_OK
    assert len(list(out.glob("*.json"))) == 1
    # the outbox run must not hide the snapshot from the real server
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert len(server.bodies) == 1
    assert U.run_pass([root], U.Sender(server.url), st, logger()[1]) == U.PASS_OK
    assert len(server.bodies) == 1
    assert set(st["sent"]) == {f"outbox:{out}", server.url}


def test_sent_list_capped_per_target(own_appdata):
    st = U.load_state()
    U.sent_list(st, "a").extend(f"k{i}" for i in range(U.SENT_KEEP + 50))
    U.sent_list(st, "b").append("x")
    U.save_state(st)
    st2 = U.load_state()
    assert len(st2["sent"]["a"]) == U.SENT_KEEP and st2["sent"]["a"][-1] == f"k{U.SENT_KEEP + 49}"
    assert st2["sent"]["b"] == ["x"]
