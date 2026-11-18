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
