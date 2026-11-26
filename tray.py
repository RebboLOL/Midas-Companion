# -*- coding: utf-8 -*-
"""
Midas Companion (formerly Midas Uplink) -- runs the uploader loop in the
background, shows a notification-area icon and an app window in the style of
midasmarket.app (local page on 127.0.0.1, see ui_server.py).

  pythonw tray.py [--url URL] [--wow DIR] [--only-wow DIR] [--interval S]
                  [--no-window] [--autostart]

Left click / double click on the icon or "Open" opens the window; it opens by
itself on the first start and when the app is started by hand.
URL default: MIDAS_UPLINK_URL or uploader.DEFAULT_URL.
Log: %APPDATA%\\MidasUplink\\uploader.log (rotating, small).
"""
import argparse
import collections
import ctypes
import hashlib
import locale
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import package_env
import uploader as U

APP_NAME = U.APP_NAME                # "Midas Companion" (package/Store name stays "Midas Uplink")
LNK_NAME = "Midas Uplink.lnk"        # unchanged: existing autostart shortcuts keep working
LOG_BYTES = 256 * 1024
LOG_KEEP = 2
CF_URL = "https://www.curseforge.com/wow/addons/midas"
WEB_URL = "https://midasmarket.app"
LINKS = {"cf": CF_URL, "web": WEB_URL, "download": WEB_URL + "/#/download"}
FLAVOR_LABEL = {"_classic_era_": "Classic Era", "_forever_": "Forever",
                "_anniversary_": "Anniversary", "_classic_era_ptr_": "Era PTR"}


# --------------------------------------------------------------------------- #
# Texts (German on German Windows, English otherwise)
# --------------------------------------------------------------------------- #
TEXT = {
    "de": {"last": "Letzter Upload: {}", "never": "noch keiner", "count": "Gesendet: {}",
           "state": "Status: {}", "send": "Jetzt senden", "auto": "Autostart beim Windows-Start",
           "log": "Log öffnen", "folder": "Ordner öffnen", "quit": "Beenden",
           "idle": "bereit", "nowow": "keine WoW-Installation gefunden",
           "prices": "Serverpreise laden", "plast": "Serverpreise: {}", "poff": "aus",
           "open": "Öffnen", "pick": "World-of-Warcraft-Ordner wählen",
           "kind_full": "voller Scan", "kind_prices": "Preis-Scan"},
    "en": {"last": "Last upload: {}", "never": "none yet", "count": "Sent: {}",
           "state": "Status: {}", "send": "Send now", "auto": "Start with Windows",
           "log": "Open log", "folder": "Open folder", "quit": "Quit",
           "idle": "ready", "nowow": "no WoW install found",
           "prices": "Load server prices", "plast": "Server prices: {}", "poff": "off",
           "open": "Open", "pick": "Choose the World of Warcraft folder",
           "kind_full": "full scan", "kind_prices": "price scan"},
}


def ui_lang():
    try:
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "de" if (lid & 0x3FF) == 0x07 else "en"
    except (AttributeError, OSError):
        loc = (locale.getlocale()[0] or "").lower()
        return "de" if loc.startswith(("de", "german")) else "en"


# --------------------------------------------------------------------------- #
# Paths, log, autostart
# --------------------------------------------------------------------------- #
def log_file():
    return U.state_dir() / "uploader.log"


class RingLog(logging.Handler):
    """Last lines of the log in memory for the live log of the window."""

    def __init__(self, keep=400):
        super().__init__(logging.INFO)
        self.lines = collections.deque(maxlen=keep)
        self.seq = 0
        self.lock_ = threading.Lock()

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        with self.lock_:
            self.seq += 1
            self.lines.append({"n": self.seq, "t": int(record.created), "m": msg[:500]})

    def since(self, after):
        with self.lock_:
            return self.seq, [x for x in self.lines if x["n"] > after]


RING = RingLog()


def setup_log():
    U.state_dir().mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("midasuplink")
    lg.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers):
        h = logging.handlers.RotatingFileHandler(log_file(), maxBytes=LOG_BYTES,
                                                 backupCount=LOG_KEEP, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(h)
    if RING not in lg.handlers:
        lg.addHandler(RING)
    return lg


def startup_dir():
    return (Path(os.environ.get("APPDATA") or Path.home()) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def startup_link():
    return startup_dir() / LNK_NAME


def launch_target():
    """(target, arguments) that start this app again (autostart)."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--autostart"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe), f'"{Path(__file__).resolve()}" --autostart'


_task_state = {}          # packaged: cached StartupTask state (menu redraws often)


def autostart_enabled():
    if package_env.packaged():
        if "on" not in _task_state:
            try:
                _task_state["on"] = package_env.startup_enabled()
            except (OSError, TimeoutError):
                _task_state["on"] = False
        return _task_state["on"]
    return startup_link().exists()


# --------------------------------------------------------------------------- #
# Shortcut via IShellLinkW + IPersistFile, in process (no helper process)
# --------------------------------------------------------------------------- #
CLSID_SHELLLINK = "00021401-0000-0000-c000-000000000046"
IID_ISHELLLINKW = "000214f9-0000-0000-c000-000000000046"
IID_IPERSISTFILE = "0000010b-0000-0000-c000-000000000046"
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
# IShellLinkW slots: 7 SetDescription, 9 SetWorkingDirectory, 11 SetArguments, 20 SetPath
# IPersistFile slots: 6 Save
SHORTCUT_TIMEOUT = 15.0
_lnk_lock = threading.Lock()      # one shortcut write at a time, late writers included
_want = {"on": None}              # last autostart choice (a late write follows it)


def _com_thread(fn, timeout=None):
    """Run fn(ole32) on a fresh STA thread (independent of the tray's apartment).
    On timeout TimeoutError is raised; the thread may still finish later."""
    out = {}

    def run():
        ole, hr = None, -1
        try:
            ole = ctypes.WinDLL("ole32")
            ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            ole.CoInitializeEx.restype = package_env.HRESULT
            ole.CoCreateInstance.argtypes = [ctypes.POINTER(package_env._GUID), ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(package_env._GUID),
                                             ctypes.POINTER(ctypes.c_void_p)]
            ole.CoCreateInstance.restype = package_env.HRESULT
            ole.CoUninitialize.argtypes = []
            ole.CoUninitialize.restype = None
            hr = ole.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            out["value"] = fn(ole)
        except Exception as e:  # noqa: BLE001 -- handed to the caller
            out["error"] = e
        finally:
            if ole is not None and hr >= 0:
                ole.CoUninitialize()

    th = threading.Thread(target=run, daemon=True, name="midas-shortcut")
    th.start()
    th.join(SHORTCUT_TIMEOUT if timeout is None else timeout)
    if th.is_alive():
        raise TimeoutError("shortcut: timed out")
    if "error" in out:
        raise out["error"]
    return out.get("value")


def write_shortcut(lnk, target, args="", workdir="", description=""):
    """Write the .lnk `lnk` (raises OSError/TimeoutError on failure). The shortcut
    is removed again when the last autostart choice is "off" by the time the write
    finishes (a slow write after a timeout, or "off" chosen meanwhile)."""
    P = package_env

    def run(ole):
        with _lnk_lock:
            _write(ole)
            if _want["on"] is False:
                try:
                    Path(lnk).unlink()
                except OSError:
                    pass

    def _write(ole):
        link = ctypes.c_void_p()
        pf = ctypes.c_void_p()
        try:
            clsid, iid = P._guid(CLSID_SHELLLINK), P._guid(IID_ISHELLLINKW)
            P._check(ole.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                          ctypes.byref(iid), ctypes.byref(link)),
                     "CoCreateInstance(ShellLink)")
            for slot, value, what in ((20, target, "SetPath"), (11, args, "SetArguments"),
                                      (9, workdir, "SetWorkingDirectory"),
                                      (7, description, "SetDescription")):
                P._check(P._vcall(link, slot, P.HRESULT, ctypes.c_wchar_p, str(value)), what)
            iid_pf = P._guid(IID_IPERSISTFILE)
            P._check(P._vcall(link, 0, P.HRESULT, ctypes.POINTER(P._GUID), ctypes.byref(iid_pf),
                              ctypes.POINTER(ctypes.c_void_p), ctypes.byref(pf)),
                     "QueryInterface(IPersistFile)")
            P._check(P._vcall(pf, 6, P.HRESULT, ctypes.c_wchar_p, str(lnk), wintypes.BOOL, True),
                     "IPersistFile.Save")
        finally:
            P._release(pf)
            P._release(link)

    _com_thread(run)


def set_autostart(on):
    if package_env.packaged():                    # MSIX: windows.startupTask
        try:
            st = package_env.set_startup(on)
        except (OSError, TimeoutError):
            _task_state.pop("on", None)
            return False
        _task_state["on"] = st in (package_env.ENABLED, package_env.ENABLED_BY_POLICY)
        return _task_state["on"] == on
    lnk = startup_link()
    _want["on"] = bool(on)
    if not on:
        try:
            lnk.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:                      # e.g. held open by a scanner
            logging.getLogger("midasuplink").warning(f"autostart shortcut not removed: {e}")
            return False
        return True
    target, args = launch_target()
    existed = lnk.exists()
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        write_shortcut(lnk, target, args, Path(target).parent, APP_NAME)
    except Exception as e:  # noqa: BLE001 -- menu and window threads must survive
        logging.getLogger("midasuplink").warning(f"autostart shortcut not written: {e}")
        if not existed:                           # no half-written shortcut left behind
            try:
                lnk.unlink()
            except OSError:
                pass
        return False
    return lnk.exists()


def resource(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def load_icon():
    from PIL import Image
    for p in (resource("assets", "midas_uplink.ico"),
              Path(__file__).resolve().parent.parent / "Brand" / "coinup_round_256.png"):
        try:
            if p.exists():
                return Image.open(p).convert("RGBA").resize((64, 64))
        except OSError:
            pass
    # fallback: plain gold disc, drawn without ImageDraw (keeps the package lean)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    px = img.load()
    for y in range(64):
        for x in range(64):
            if (x - 31.5) ** 2 + (y - 31.5) ** 2 <= 28 ** 2:
                px[x, y] = (232, 180, 74, 255)
    return img


GLOBAL_LOCK = "Local\\MidasUplinkTray_any"
OPEN_EVENT = "Local\\MidasUplinkOpen_any"


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    k.CreateEventW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
    k.OpenEventW.restype = ctypes.c_void_p
    k.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    k.SetEvent.argtypes = (ctypes.c_void_p,)
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k.WaitForSingleObject.restype = ctypes.c_uint32
    return k


def _mutex(name):
    """Create/open a named mutex. -> (handle, already_existed)"""
    k = _k32()
    h = k.CreateMutexW(None, False, name)
    return h, ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS


def _state_tag():
    return hashlib.sha1(str(U.state_dir()).lower().encode("utf-8")).hexdigest()[:12]


def open_event_name():
    """Second start -> the running instance opens its window. Start tests
    (MIDAS_UPLINK_SECOND_INSTANCE=1) use a name of their own state folder, so they
    never reach a real running instance."""
    if os.environ.get("MIDAS_UPLINK_SECOND_INSTANCE") == "1":
        return "Local\\MidasUplinkOpen_" + _state_tag()
    return OPEN_EVENT


def single_instance():
    """True when no other tray instance runs in this session.
    Two locks: a fixed one across all versions and installs (EXE and Store
    app must not both upload) and one per state folder.
    MIDAS_UPLINK_SECOND_INSTANCE=1 skips the fixed lock (start tests only,
    always together with an own APPDATA)."""
    try:
        handles, busy = [], False
        if os.environ.get("MIDAS_UPLINK_SECOND_INSTANCE") != "1":
            h, ex = _mutex(GLOBAL_LOCK)
            handles.append(h)
            busy |= ex
        h, ex = _mutex("Local\\MidasUplinkTray_" + _state_tag())
        handles.append(h)
        busy |= ex
        single_instance.handles = handles
        single_instance.handle = handles[-1]
        return not busy
    except (AttributeError, OSError):
        return True


def signal_running_instance():
    """Ask the running instance to open its window. -> True when it was reached."""
    try:
        k = _k32()
        h = k.OpenEventW(0x0002, False, open_event_name())     # EVENT_MODIFY_STATE
        if not h:
            return False
        k.SetEvent(h)
        k.CloseHandle(h)
        return True
    except (AttributeError, OSError):
        return False


def remove_legacy_autostart(log):
    """Packaged app: the StartupTask replaces the shortcut of the EXE install.
    A leftover shortcut would start the EXE as well -> remove it once."""
    if not package_env.packaged():
        return False
    lnk = startup_link()
    if not lnk.exists():
        return False
    try:
        lnk.unlink()
        log.info(f"removed autostart shortcut of the EXE install: {lnk}")
        return True
    except OSError as e:
        log.warning(f"could not remove autostart shortcut {lnk}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
def find_edge():
    cands = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    cands.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    except ImportError:
        pass
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            pass
    return None


def open_window(url):
    """Edge app window (no tabs, no address bar); default browser as fallback.
    -> "edge" | "browser" | None"""
    edge = find_edge()
    if edge:
        try:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1060,860", "--no-first-run",
                              "--no-default-browser-check"], close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "edge"
        except OSError:
            pass
    try:
        os.startfile(url)
        return "browser"
    except OSError:
        return None


def pick_folder(title):
    """Windows folder dialog (shell32, no extra packages). -> path or None."""
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    ole32, shell32, user32 = ctypes.windll.ole32, ctypes.windll.shell32, ctypes.windll.user32
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = (ctypes.POINTER(BROWSEINFOW),)
    shell32.SHGetPathFromIDListW.argtypes = (ctypes.c_void_p, wintypes.LPWSTR)
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    user32.GetForegroundWindow.restype = wintypes.HWND
    ole32.CoInitializeEx(None, 0x2)                       # apartment threaded
    try:
        name = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = user32.GetForegroundWindow()
        bi.pszDisplayName = ctypes.cast(name, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = 0x0001 | 0x0040 | 0x0200             # only folders, new style, no "new folder"
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        path = ctypes.create_unicode_buffer(1024)
        ok = shell32.SHGetPathFromIDListW(pidl, path)
        ole32.CoTaskMemFree(pidl)
        return path.value if ok else None
    finally:
        ole32.CoUninitialize()


def system_uptime():
    try:
        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_uint64
        return k.GetTickCount64() / 1000.0
    except (AttributeError, OSError):
        return 1e9


# --------------------------------------------------------------------------- #
# Status for the window
# --------------------------------------------------------------------------- #
class SvCache:
    """MidasUplinkDB summary per SavedVariables file, re-read only on change
    (Midas.lua can be ~50 MB)."""

    def __init__(self):
        self.d = {}
        self.lock = threading.Lock()
        self.wtf = {}                       # realm folders per flavor (30 s, see U.extra_markets)

    def get(self, path):
        key = str(path)
        try:
            s = path.stat()
            sig = (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
        with self.lock:
            hit = self.d.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        try:
            db = U.load_uplink_db(path)
        except (OSError, U.LuaParseError):
            return hit[1] if hit else None
        summ = summarize_db(db)
        with self.lock:
            self.d[key] = (sig, summ)
        return summ


def summarize_db(db):
    if not isinstance(db, dict):
        return {"present": False, "enabled": False, "addon": None, "queue": 0, "seen": []}
    q = db.get("queue")
    return {"present": True, "enabled": db.get("enabled") is True,
            "addon": db.get("addon") if isinstance(db.get("addon"), str) else None,
            "queue": len(q) if isinstance(q, (list, dict)) else 0,
            "seen": U.seen_of_db(db)}


def prices_file_info(addon_dir):
    """-> {"path", "app": bool, "markets": n|None, "written": ts} of a Prices.lua."""
    f = Path(addon_dir) / U.PRICE_FILE
    out = {"path": str(f), "app": False, "markets": None, "written": 0}
    try:
        with open(f, "rb") as fh:
            head = fh.read(300)
        size = f.stat().st_size
    except OSError:
        return out
    out["app"] = b"app = " in head
    m = U._WRITTEN_RE.search(head)
    if m:
        out["written"] = int(m.group(0).split(b"=")[1])
    if out["app"]:
        if size > 4 * 1024 * 1024:
            out["markets"] = -1                         # big file: not counted
        else:
            try:
                out["markets"] = f.read_bytes().count(b"(function() return")
            except OSError:
                pass
    return out


def _copy(d):
    return dict(d) if isinstance(d, dict) else {}


def build_status(roots, st, url, cache, now=None, manual=None):
    """Everything the window shows, as plain JSON data. Markets carry their
    source: "seen" (Midas), "wtf" (game folder) or "manual" (added by hand)."""
    now = int(time.time()) if now is None else now
    manual = list(st.get("manual_markets") or []) if manual is None else list(manual)
    flavors, share_on, any_midas_ok = [], False, False
    markets = {}
    base_url = U.export_url(url)

    def server_list():                      # cache only: the window never asks the server
        return U.server_forever_markets(base_url, st, now, fetch=False) if st.get("markets_list") else []
    for root in roots:
        for fl in U.FLAVORS:
            base = Path(root) / fl
            if not base.is_dir():
                continue
            addons = base / "Interface" / "AddOns"
            toc = U.read_toc(addons / U.MIDAS_ADDON, U.MIDAS_ADDON)
            old = U.read_toc(addons / U.PRICE_ADDON, U.PRICE_ADDON)
            midas_v = toc["version"] if toc else None
            pv = U.parse_version(midas_v)
            midas_ok = bool(pv) and pv >= U.MIDAS_MIN
            any_midas_ok |= midas_ok
            fshare = None
            accounts = 0
            fseen = {}
            for f in U.sv_sources([root], (fl,)):
                summ = cache.get(f)
                if not summ or not summ["present"]:
                    continue
                accounts += 1
                fshare = bool(fshare) or summ["enabled"]
                for mk in summ["seen"]:
                    if now - mk["last"] > U.SEEN_DAYS * 86400:
                        continue
                    k = U.raw_key(mk)
                    if k not in fseen or fseen[k]["last"] < mk["last"]:
                        fseen[k] = mk
                    if k not in markets or markets[k]["last"] < mk["last"]:
                        markets[k] = dict(mk, flavor=fl, src="seen")
            share_on |= bool(fshare)
            pdirs = U.flavor_price_dirs(base) if fl in U.PRICE_FLAVORS else []
            finfo = None
            if fl in U.PRICE_FLAVORS:
                try:
                    extra, finfo = U.extra_markets(root, fl, list(fseen.values()), manual, now,
                                                     getattr(cache, "wtf", None), server_list)
                except OSError:
                    extra = []
                for mk in extra:
                    k = U.raw_key(mk)
                    if k not in markets:
                        markets[k] = dict(mk, flavor=fl, last=0 if mk["src"] == "manual" else mk["last"])
            flavors.append({
                "flavor": fl, "label": FLAVOR_LABEL.get(fl, fl.strip("_")), "root": str(root),
                "midas": midas_v, "midas_ok": midas_ok,
                "midas_prices": bool(toc) and U.PRICE_FILE.lower() in toc["files"],
                "old_uplink": old["version"] if old else None,
                "share": fshare, "accounts": accounts,
                "prices": [prices_file_info(d) for d in pdirs],
                "found": ({"region": finfo["region"], "realms": len(finfo["realms"]),
                           "numbered": finfo["numbered"], "no_region": finfo["no_region"],
                           "listed": finfo["listed"]}
                          if finfo else None)})
    pmap = _copy(st.get("prices_market"))
    pinfo = _copy(st.get("prices_info"))
    um = _copy(st.get("upload_markets"))
    rows = []
    order = {"seen": 0, "manual": 1, "wtf": 2, "list": 3}
    for k, mk in sorted(markets.items(), key=lambda kv: (order[kv[1]["src"]], -kv[1]["last"])):
        server = pmap.get(U.query_key(base_url, mk)) if base_url else None
        info = pinfo.get(server) if server else None
        up = um.get("|".join(k))
        rows.append({"client": mk["client"], "region": mk["region"], "realm": mk["realm"],
                     "faction": mk["faction"], "ruleset": mk.get("ruleset"), "flavor": mk["flavor"],
                     "last_seen": mk["last"] if mk["src"] == "seen" else 0,
                     "folder_at": mk["last"] if mk["src"] == "wtf" else 0,
                     "src": mk["src"], "server": server,
                     "server_ts": info["ts"] if info else 0, "items": info["items"] if info else 0,
                     "upload_at": up["at"] if up else 0, "upload_ts": up["ts"] if up else 0,
                     "upload_kind": up.get("kind", "full") if up else None})
    stats = _copy(st.get("stats"))
    written = [p for f in flavors for p in f["prices"] if p["app"]]
    return {
        "app": {"name": APP_NAME, "version": U.VERSION},
        "now": now,
        "wow": {"found": bool(roots), "roots": [str(r) for r in roots]},
        "flavors": flavors,
        "midas_ok": any_midas_ok,
        "share_on": share_on,
        "uploads": stats.get("uploads", 0), "last_upload": stats.get("last", 0),
        "last_kind": st.get("last_upload_kind") if st.get("last_upload_kind") in ("full", "prices") else None,
        "last_full": stats.get("last_full", 0), "last_prices": stats.get("last_prices", 0),
        "prices_enabled": st.get("prices_enabled", True) is True,
        "prices_last": st.get("prices_last", 0),
        "prices_written": bool(written),
        "prices_with_data": any((p["markets"] or 0) != 0 for p in written),
        "markets": rows[:40],
        "manual": [{"id": U.manual_id(m), "flavor": m["flavor"],
                    "label": FLAVOR_LABEL.get(m["flavor"], m["flavor"]), "region": m["region"],
                    "realm": m["realm"], "faction": m["faction"]} for m in manual],
        "manual_max": U.MANUAL_MAX,
    }


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, url, wow, interval, only_wow=None, lang=None):
        self.lang = lang or ui_lang()
        self.t = TEXT[self.lang]
        self.log = setup_log()
        remove_legacy_autostart(self.log)
        self.url, self.wow, self.interval = url, list(wow), interval
        self.only_wow = [str(d) for d in (only_wow or [])]
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.force = False
        self.watcher = None
        self.status = self.t["idle"]
        self.icon = None
        self.ui = None
        self.cache = SvCache()
        self._dir_req = []
        self._picked = []                            # folders chosen in the window during this run
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()          # watcher (re)creation vs. direct state writes
        self._pick_lock = threading.Lock()
        self._last_open = 0.0
        self.roots = []

    # uploader log callback
    def note(self, msg):
        self.log.info(msg)
        self.status = msg if len(msg) <= 60 else msg[:57] + "..."
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001 -- menu refresh is cosmetic
                pass

    def state(self):
        return self.watcher.st if self.watcher else U.load_state()

    def find_roots(self):
        if self.only_wow:
            return [Path(d) for d in self.only_wow if U.is_dir(d)]
        st = self.state()
        dirs = self.wow + st.get("wow_dirs", [])
        return U.wow_roots(dirs + [d for d in self._picked if d not in dirs])

    def _apply_dir_requests(self):
        with self._lock:
            reqs, self._dir_req = self._dir_req, []
        if not reqs:
            return
        # kept in memory too: the choice counts for this run even when the state
        # cannot be written
        self._picked += [d for d in reqs if d not in self._picked]
        with self._state_lock:
            try:
                if self.watcher:
                    self.watcher._apply_request()   # pending window changes go into the state first
                st = self.state()
                for d in reqs:
                    if d not in st["wow_dirs"]:
                        st["wow_dirs"].append(d)
                        self.log.info(f"World of Warcraft folder added: {d}")
                U.save_state(st)
            finally:
                self.watcher = None                  # rebuilt with the new roots

    def loop(self):
        self.log.info(f"{APP_NAME} {U.VERSION} start, url {self.url}")
        try:
            while not self.stop.is_set():
                try:
                    wait = self._pass()
                except Exception as e:  # noqa: BLE001 -- keep the loop alive
                    self.log.exception("pass failed")
                    self.status = str(e)[:60]
                    wait = 60
                self.wake.wait(wait)
                self.wake.clear()
        finally:
            self.log.info("stop")

    def _pass(self):
        """One round of the background thread -> seconds until the next one. The
        folder search runs inside the loop's error handling as well: an error
        there must never end the thread (the window would stay on "folder not
        found" and ignore every folder chosen by hand)."""
        self._apply_dir_requests()
        if self.watcher is None:
            roots = self.find_roots()
            self.roots = roots
            if not roots:
                self.note(self.t["nowow"])
                return 300
            self.log.info("watching: " + "; ".join(str(r) for r in roots))
            with self._state_lock:                      # no direct state write in between
                self.watcher = U.Watcher(roots, U.Sender(self.url), self.note, self.interval)
                dirs = self.watcher.st["wow_dirs"]      # a choice that could not be saved yet
                dirs += [d for d in self._picked if d not in dirs]      # goes out with the next save
        force, self.force = self.force, False
        return self.watcher.step(force=force)

    # ---- window backend (ui_server) ---------------------------------------- #
    def manual_markets(self):
        w = self.watcher
        return w.manual_markets() if w else list(U.load_state()["manual_markets"])

    def _write_state(self, key, value):
        """Change one state field: through the watcher when it runs (its thread owns
        the state), otherwise directly - under the same lock that creates a watcher,
        so a new watcher always loads the change and never overwrites it."""
        with self._state_lock:
            w = self.watcher
            if w is not None:
                (w.set_manual if key == "manual_markets" else w.set_prices)(value)
            else:
                st = U.load_state()
                st[key] = value
                U.save_state(st)
        if w is not None:
            self.wake.set()
        return w is None

    def set_manual(self, markets):
        self._write_state("manual_markets", list(markets))

    def add_market(self, data):
        with self._lock:
            try:
                new = U.add_manual_market(self.manual_markets(), {
                    "flavor": data.get("flavor"), "region": data.get("region"),
                    "realm": data.get("realm"), "faction": data.get("faction")})
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            self.set_manual(new)
        m = new[-1]
        self.log.info(f'market added by hand: {m["flavor"]} {m["region"]}/{m["realm"]}/{m["faction"]}')
        return {"ok": True, "id": U.manual_id(m)}

    def remove_market(self, mid):
        with self._lock:
            cur = self.manual_markets()
            new = U.remove_manual_market(cur, mid) if isinstance(mid, str) else cur
            if len(new) == len(cur):
                return {"ok": False, "error": "unknown"}
            self.set_manual(new)
        self.log.info("market added by hand removed")
        return {"ok": True}

    def status_data(self):
        st = self.state()
        d = build_status(list(self.roots), st, self.url, self.cache, manual=self.manual_markets())
        d["status"] = self.status
        d["autostart"] = autostart_enabled()
        d["packaged"] = package_env.packaged()
        d["lang"] = self.lang
        d["only_wow"] = bool(self.only_wow)
        d["client_id"] = st.get("client_id")        # shown for access/deletion requests
        return d

    def log_since(self, after):
        return RING.since(after)

    def add_wow_dir(self, path):
        root = U.normalize_wow_dir(path) if path else None
        if root is None:
            return {"ok": False, "error": "no_wow"}
        with self._lock:
            self._dir_req.append(str(root))
        self.wake.set()
        return {"ok": True, "root": str(root)}

    def action(self, name, data):
        if name == "send":
            self.send_now()
            return {"ok": True}
        if name == "autostart":
            ok = set_autostart(bool(data.get("on")))
            self.log.info(f"autostart {'on' if data.get('on') else 'off'}{'' if ok else ' FAILED'}")
            return {"ok": ok, "on": autostart_enabled()}
        if name == "prices":
            self.set_prices(bool(data.get("on")))
            return {"ok": True}
        if name == "folder":
            p = data.get("path")
            return self.add_wow_dir(p if isinstance(p, str) and len(p) < 1000 else None)
        if name == "pick":
            if not self._pick_lock.acquire(blocking=False):
                return {"ok": False, "error": "busy"}
            try:
                p = pick_folder(self.t["pick"])
            finally:
                self._pick_lock.release()
            if not p:
                return {"ok": False, "error": "cancel"}
            return self.add_wow_dir(p)
        if name == "market_add":
            return self.add_market(data)
        if name == "market_del":
            return self.remove_market(data.get("id"))
        if name == "open":
            what = data.get("what")
            if what == "log":
                self.open_log()
            elif what == "folder":
                self.open_folder()
            elif what in LINKS:
                os.startfile(LINKS[what])
            else:
                return {"ok": False, "error": "unknown"}
            return {"ok": True}
        return {"ok": False, "error": "unknown"}

    def start_ui(self):
        import ui_server
        if self.ui is None:
            self.ui = ui_server.UIServer(_Backend(self), self.lang).start()
        return self.ui

    def show_window(self, icon=None, item=None):
        now = time.monotonic()
        if now - self._last_open < 2.0:              # click + double click: one window
            return None
        self._last_open = now
        ui = self.start_ui()
        how = open_window(ui.new_url())           # one-time ticket, useless once opened
        self.log.info(f"window opened ({how or 'failed'})")
        return how

    def watch_open_requests(self):
        """Second start of the app -> open the window here."""
        try:
            k = _k32()
            h = k.CreateEventW(None, False, False, open_event_name())
        except (AttributeError, OSError):
            return
        if not h:
            return
        while not self.stop.is_set():
            if k.WaitForSingleObject(h, 1000) == 0:
                self.show_window()
        k.CloseHandle(h)

    # ---- menu actions -------------------------------------------------------- #
    def send_now(self, icon=None, item=None):
        self.force = True
        self.wake.set()

    def toggle_autostart(self, icon=None, item=None):
        on = not autostart_enabled()
        ok = set_autostart(on)
        self.log.info(f"autostart {'on' if on else 'off'}{'' if ok else ' FAILED'}")

    def prices_enabled(self, item=None):
        if self.watcher:
            return self.watcher.prices_on()
        return U.load_state()["prices_enabled"]

    def set_prices(self, on):
        if self._write_state("prices_enabled", bool(on)):    # no loop running: written directly
            self.log.info(f"server prices {'on' if on else 'off'}")

    def toggle_prices(self, icon=None, item=None):
        self.set_prices(not self.prices_enabled())

    def prices_text(self, item=None):
        if not self.prices_enabled():
            return self.t["plast"].format(self.t["poff"])
        st = self.state()
        last = st.get("prices_last") or 0
        when = time.strftime("%d.%m. %H:%M", time.localtime(last)) if last else self.t["never"]
        return self.t["plast"].format(when)

    def open_log(self, icon=None, item=None):
        f = log_file()
        if f.exists():
            os.startfile(f)

    def open_folder(self, icon=None, item=None):
        os.startfile(U.state_dir())

    def quit(self, icon=None, item=None):
        self.stop.set()
        self.wake.set()
        if self.icon:
            self.icon.stop()

    def last_text(self, item=None):
        st = self.state()
        last = st.get("stats", {}).get("last") or 0
        when = time.strftime("%d.%m. %H:%M", time.localtime(last)) if last else self.t["never"]
        kind = st.get("last_upload_kind")
        if last and kind in ("full", "prices"):
            when += f' ({self.t["kind_" + kind]})'
        return self.t["last"].format(when)

    def count_text(self, item=None):
        st = self.state()
        return self.t["count"].format(st.get("stats", {}).get("uploads", 0))

    def first_start(self):
        """True once: the window opens by itself on the very first start."""
        st = U.load_state()
        if st.get("window_shown"):
            return False
        st["window_shown"] = True
        U.save_state(st)
        return True

    def run(self, show=False):
        import pystray
        M = pystray.MenuItem
        menu = pystray.Menu(
            M(self.t["open"], self.show_window, default=True),
            pystray.Menu.SEPARATOR,
            M(lambda i: self.last_text(), None, enabled=False),
            M(lambda i: self.count_text(), None, enabled=False),
            M(lambda i: self.prices_text(), None, enabled=False),
            M(lambda i: self.t["state"].format(self.status), None, enabled=False),
            pystray.Menu.SEPARATOR,
            M(self.t["send"], self.send_now),
            M(self.t["prices"], self.toggle_prices, checked=lambda i: self.prices_enabled()),
            M(self.t["auto"], self.toggle_autostart, checked=lambda i: autostart_enabled()),
            M(self.t["log"], self.open_log),
            M(self.t["folder"], self.open_folder),
            pystray.Menu.SEPARATOR,
            M(self.t["quit"], self.quit),
        )
        self.icon = pystray.Icon("MidasUplink", load_icon(), APP_NAME, menu)
        th = threading.Thread(target=self.loop, name="uplink", daemon=True)
        th.start()
        threading.Thread(target=self.watch_open_requests, name="open", daemon=True).start()
        self.start_ui()
        if show:
            threading.Timer(0.5, self.show_window).start()
        self.icon.run()
        self.stop.set()
        self.wake.set()
        th.join(timeout=5)
        if self.ui:
            self.ui.close()


class _Backend:
    """Adapter for ui_server.UIServer."""

    def __init__(self, app):
        self.app = app

    def status(self):
        return self.app.status_data()

    def log_since(self, after):
        return self.app.log_since(after)

    def action(self, name, data):
        return self.app.action(name, data)


def should_open_window(app, args):
    if args.no_window:
        return False
    if app.first_start():
        return True
    if args.autostart:
        return False
    # StartupTask (Store app) and shortcuts of older versions pass no argument:
    # no window right after Windows start
    return system_uptime() >= 600


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--url", default=os.environ.get("MIDAS_UPLINK_URL", U.DEFAULT_URL))
    ap.add_argument("--wow", action="append", default=[])
    ap.add_argument("--only-wow", action="append", default=[], metavar="DIR",
                    help="use only this World of Warcraft folder, no auto-detection (tests)")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--no-window", action="store_true", help="do not open the window at start")
    ap.add_argument("--autostart", action="store_true", help="started by Windows autostart")
    a = ap.parse_args(argv)
    use_packaged_home()
    if not single_instance():
        if not a.autostart and not a.no_window:
            signal_running_instance()
        return 0
    app = App(a.url, a.wow, a.interval, only_wow=a.only_wow)
    app.run(show=should_open_window(app, a))
    return 0


def use_packaged_home():
    """MSIX: keep state + log in the package's LocalState (a real folder that
    Explorer and Notepad can open; %APPDATA% writes would be virtualized).
    The first start takes over the state of a former unpackaged install."""
    if os.environ.get("MIDAS_UPLINK_HOME"):
        return None
    home = package_env.packaged_home()
    if not home:
        return None
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MIDAS_UPLINK_HOME"] = str(home)
    old = Path(os.environ.get("APPDATA") or Path.home()) / "MidasUplink" / "state.json"
    new = home / "state.json"
    if not new.exists() and old.is_file():
        try:
            new.write_bytes(old.read_bytes())
        except OSError:
            pass
    return home


if __name__ == "__main__":
    sys.exit(main())
