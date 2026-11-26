import logging

import tray as T
import uploader as U


def test_log_goes_to_own_appdata(own_appdata):
    lg = T.setup_log()
    lg.info("hello")
    for h in lg.handlers:
        h.flush()
    f = own_appdata / "MidasUplink" / "uploader.log"
    assert f.is_file() and "hello" in f.read_text(encoding="utf-8")
    h = lg.handlers[0]
    assert isinstance(h, logging.handlers.RotatingFileHandler) and h.maxBytes <= 512 * 1024
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


def test_autostart_default_off_and_toggle(own_appdata):
    assert str(T.startup_dir()).startswith(str(own_appdata))
    assert not T.autostart_enabled()
    assert T.set_autostart(True)
    assert T.autostart_enabled() and T.startup_link().name == "Midas Uplink.lnk"
    assert T.set_autostart(False)
    assert not T.autostart_enabled()
    assert T.set_autostart(False)          # idempotent


def _read_lnk(lnk):
    """(path, arguments, working dir, description) of a .lnk via IShellLinkW (test only)."""
    import ctypes
    from ctypes import wintypes
    import package_env as P
    WSTR = ctypes.POINTER(ctypes.c_wchar)

    def run(ole):
        link, pf = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            clsid, iid = P._guid(T.CLSID_SHELLLINK), P._guid(T.IID_ISHELLLINKW)
            P._check(ole.CoCreateInstance(ctypes.byref(clsid), None, T.CLSCTX_INPROC_SERVER,
                                          ctypes.byref(iid), ctypes.byref(link)), "create")
            iid_pf = P._guid(T.IID_IPERSISTFILE)
            P._check(P._vcall(link, 0, P.HRESULT, ctypes.POINTER(P._GUID), ctypes.byref(iid_pf),
                              ctypes.POINTER(ctypes.c_void_p), ctypes.byref(pf)), "qi")
            P._check(P._vcall(pf, 5, P.HRESULT, ctypes.c_wchar_p, str(lnk), wintypes.DWORD, 0), "Load")
            buf = ctypes.create_unicode_buffer(1024)
            P._check(P._vcall(link, 3, P.HRESULT, WSTR, buf, ctypes.c_int, 1024,       # GetPath
                              ctypes.c_void_p, None, wintypes.DWORD, 4), "GetPath")  # SLGP_RAWPATH
            out = [buf.value]
            for slot in (10, 8, 6):      # GetArguments, GetWorkingDirectory, GetDescription
                buf = ctypes.create_unicode_buffer(1024)
                P._check(P._vcall(link, slot, P.HRESULT, WSTR, buf, ctypes.c_int, 1024), str(slot))
                out.append(buf.value)
            return tuple(out)
        finally:
            P._release(pf)
            P._release(link)

    return T._com_thread(run)


def test_autostart_shortcut_content(own_appdata):
    import os
    from pathlib import Path
    assert T.set_autostart(True)
    lnk = T.startup_link()
    data = lnk.read_bytes()
    # MS-SHLLINK header: size 0x4C + CLSID_ShellLink, read without the Shell API
    assert data[:4] == b"\x4c\x00\x00\x00"
    assert data[4:20] == bytes.fromhex("0114020000000000c000000000000046")
    assert "--autostart".encode("utf-16-le") in data
    target, args = T.launch_target()
    path, a, wd, desc = _read_lnk(lnk)
    assert os.path.normcase(path) == os.path.normcase(target)
    assert a == args and a.endswith("--autostart")
    assert os.path.normcase(wd) == os.path.normcase(str(Path(target).parent))
    assert desc == T.APP_NAME


def test_autostart_starts_no_helper_process(own_appdata, monkeypatch):
    import os
    import subprocess

    def boom(*a, **kw):
        raise AssertionError("autostart must not start a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, boom)
    monkeypatch.setattr(os, "system", boom)
    assert T.set_autostart(True) and T.autostart_enabled()
    assert T.set_autostart(False) and not T.autostart_enabled()


def test_autostart_unicode_and_quote_in_paths(own_appdata, monkeypatch, tmp_path):
    import sys
    d = tmp_path / "Bäcker's Ordner (x86) $env;"
    d.mkdir()
    monkeypatch.setenv("APPDATA", str(d))
    monkeypatch.setattr(T, "launch_target", lambda: (sys.executable, '"C:\\Spiele\\Bäcker\'s\\tray.py" --autostart'))
    assert T.set_autostart(True)
    lnk = T.startup_link()
    assert str(lnk).startswith(str(d)) and lnk.is_file()
    path, a, wd, desc = _read_lnk(lnk)
    assert a == '"C:\\Spiele\\Bäcker\'s\\tray.py" --autostart'


def test_late_shortcut_write_after_timeout_is_removed(own_appdata, monkeypatch):
    import threading
    import time
    monkeypatch.setattr(T, "SHORTCUT_TIMEOUT", 0.3)
    T._lnk_lock.acquire()                     # stand-in for a Save that hangs (e.g. AV scan)
    try:
        assert T.set_autostart(True) is False         # caller gives up after the timeout
        assert not T.startup_link().exists()
        assert T.set_autostart(False) is True         # user switches it off meanwhile
    finally:
        T._lnk_lock.release()                 # the late write goes through now ...
    end = time.monotonic() + 10
    while time.monotonic() < end and any(t.name == "midas-shortcut" for t in threading.enumerate()):
        time.sleep(0.05)
    assert not any(t.name == "midas-shortcut" for t in threading.enumerate())
    assert not T.startup_link().exists()      # ... and removes its shortcut again
    monkeypatch.setattr(T, "SHORTCUT_TIMEOUT", 15.0)
    assert T.set_autostart(True) and T.autostart_enabled()     # next try works normally


def test_late_write_keeps_shortcut_that_was_there_before(own_appdata, monkeypatch):
    import threading
    import time
    assert T.set_autostart(True)              # autostart already on
    monkeypatch.setattr(T, "SHORTCUT_TIMEOUT", 0.3)
    T._lnk_lock.acquire()
    try:
        assert T.set_autostart(True) is False         # "on" again, the write hangs
        assert T.startup_link().exists()
    finally:
        T._lnk_lock.release()
    end = time.monotonic() + 10
    while time.monotonic() < end and any(t.name == "midas-shortcut" for t in threading.enumerate()):
        time.sleep(0.05)
    assert T.startup_link().exists() and T.autostart_enabled()


def _wait_shortcut_threads():
    import threading
    import time
    end = time.monotonic() + 10
    while time.monotonic() < end and any(t.name == "midas-shortcut" for t in threading.enumerate()):
        time.sleep(0.05)
    assert not any(t.name == "midas-shortcut" for t in threading.enumerate())


def test_late_write_follows_off_even_if_shortcut_was_there(own_appdata, monkeypatch):
    assert T.set_autostart(True)              # on
    monkeypatch.setattr(T, "SHORTCUT_TIMEOUT", 0.3)
    T._lnk_lock.acquire()
    try:
        assert T.set_autostart(True) is False         # on again, the write hangs
        assert T.set_autostart(False) is True         # then off
        assert not T.startup_link().exists()
    finally:
        T._lnk_lock.release()
    _wait_shortcut_threads()
    assert not T.startup_link().exists() and not T.autostart_enabled()   # last choice wins


def test_late_write_after_timeout_keeps_shortcut_when_still_on(own_appdata, monkeypatch):
    monkeypatch.setattr(T, "SHORTCUT_TIMEOUT", 0.3)
    T._lnk_lock.acquire()
    try:
        assert T.set_autostart(True) is False         # reported as failed for now
    finally:
        T._lnk_lock.release()
    _wait_shortcut_threads()
    assert T.autostart_enabled()              # still the user's last choice -> kept


def test_autostart_off_reports_locked_shortcut(own_appdata, monkeypatch):
    from pathlib import Path
    assert T.set_autostart(True)
    real = Path.unlink

    def locked(self, *a, **kw):
        if self.name == T.LNK_NAME:
            raise PermissionError(32, "in use")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", locked)
    assert T.set_autostart(False) is False
    monkeypatch.setattr(Path, "unlink", real)
    assert T.set_autostart(False) is True and not T.autostart_enabled()


def test_autostart_unexpected_error_does_not_escape(own_appdata, monkeypatch):
    def fail(*a, **kw):
        raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(T, "write_shortcut", fail)
    assert T.set_autostart(True) is False


def test_autostart_failure_returns_false_and_logs(own_appdata, monkeypatch):
    lg = T.setup_log()

    def fail(*a, **kw):
        raise OSError("IPersistFile.Save failed: 0x80070005")

    monkeypatch.setattr(T, "write_shortcut", fail)
    assert T.set_autostart(True) is False
    assert not T.startup_link().exists()
    for h in lg.handlers:
        h.flush()
    assert "autostart shortcut not written" in T.log_file().read_text(encoding="utf-8")


def test_single_instance_state_lock(own_appdata, monkeypatch):
    monkeypatch.setenv("MIDAS_UPLINK_SECOND_INSTANCE", "1")      # state lock only
    assert T.single_instance() is True and T.single_instance.handle
    assert T.single_instance() is False                          # same state folder again


def test_single_instance_global_lock(own_appdata, monkeypatch, tmp_path):
    import uuid
    monkeypatch.setattr(T, "GLOBAL_LOCK", "Local\\MidasUplinkTray_test_" + uuid.uuid4().hex)
    assert T.single_instance() is True
    # other state folder (e.g. Store app vs. EXE) -> still blocked by the fixed lock
    monkeypatch.setenv("APPDATA", str(tmp_path / "other"))
    assert T.single_instance() is False
    assert len(T.single_instance.handles) == 2


def test_load_icon_without_optional_pil_modules(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    # the Store package ships without ImageDraw/ImageFont/codec plugins
    for m in ("PIL.ImageDraw", "PIL.ImageFont", "PIL._imagingft", "PIL._avif", "PIL._webp"):
        monkeypatch.setitem(sys.modules, m, None)
    img = T.load_icon()
    assert img.size == (64, 64)
    # fallback disc when no icon file is found
    monkeypatch.setattr(T, "resource", lambda *p: tmp_path / "missing.ico")
    monkeypatch.setattr(T, "__file__", str(tmp_path / "x" / "tray.py"))
    img = T.load_icon()
    assert img.size == (64, 64) and img.getpixel((32, 32)) == (232, 180, 74, 255)
    assert img.getpixel((0, 0))[3] == 0


def test_global_lock_name():
    assert T.GLOBAL_LOCK == "Local\\MidasUplinkTray_any"


def test_legacy_shortcut_removed_when_packaged(own_appdata, monkeypatch):
    import package_env as P
    lg = T.setup_log()
    T.startup_dir().mkdir(parents=True)
    T.startup_link().write_bytes(b"x")
    assert T.remove_legacy_autostart(lg) is False            # unpackaged: untouched
    assert T.startup_link().exists()
    monkeypatch.setattr(P, "packaged", lambda: True)
    assert T.remove_legacy_autostart(lg) is True
    assert not T.startup_link().exists()
    assert T.remove_legacy_autostart(lg) is False               # nothing left
    for h in lg.handlers:
        h.flush()
    assert "removed autostart shortcut" in T.log_file().read_text(encoding="utf-8")


def test_status_texts(own_appdata):
    app = T.App(U.DEFAULT_URL, [], 30)
    assert app.t["never"] in app.last_text()
    assert app.count_text().endswith("0")
    for h in list(app.log.handlers):
        h.close()
        app.log.removeHandler(h)
