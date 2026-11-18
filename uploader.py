#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midas Companion (formerly Midas Uplink) uploader -- sends queued offer-book
snapshots to midasmarket.app.

WoW writes WTF\\Account\\<acc>\\SavedVariables\\Midas.lua (Midas 1.3.0+) and
Midas_Uplink.lua (old sub-addon) on /reload or logout. This tool finds every such file under the known WoW installs, reads the
queue, and posts each snapshot it has not sent before. Item names (client
language) ride along once per URL + locale.

Downlink (0.3.0): the addon remembers the markets the player logged into
(MidasUplinkDB.seen, independent of the upload opt-in). For each market seen in
the last 30 days the tool fetches GET .../api/v1/export and writes
<flavor>\\Interface\\AddOns\\Midas_Uplink\\Prices.lua, which the game loads on
login or /reload. Nothing else in the game folder is ever written.

Price history (0.4.0): with sharing ON the addon prepares, once per market,
the daily values Midas kept for the last 90 days (MidasUplinkDB.history). The
tool posts each block once to .../api/v1/history (same client_id, retry and
backoff as snapshots) and remembers it in the state (history_sent).

90 days of server prices (0.4.1): the export is fetched with days=90 (off 0..89),
so a fresh install gets the full price history after the first sync. A server
without days=90 answers 422; the tool then asks once more without days (14).

Datalink 1.3.0 (Midas Companion): Midas 1.3.0 keeps MidasUplinkDB in its own
SavedVariables file Midas.lua (next to MidasDB, up to ~50 MB). Only the
MidasUplinkDB block is cut out and parsed; the old Midas_Uplink.lua is still read.
Prices.lua goes to AddOns\\Midas (when Midas.toc lists Prices.lua) and to
AddOns\\Midas_Uplink as long as that folder exists. The file carries app = <version>
and checked = <last successful server query> and is written even without prices
(markets = {}), so the addon can tell that the app is installed.

Usage:
  python uploader.py --once                one pass, then exit
  python uploader.py                       watch (poll every --interval s)
  python uploader.py --outbox DIR          write payloads as JSON instead of POSTing
  python uploader.py --prices-now          fetch server prices once, then exit
  python uploader.py --wow "D:\\Games\\World of Warcraft"   additional install (plus auto-detection)
  python uploader.py --only-wow "D:\\Test\\WoW" --prices-now   only this folder, no auto-detection

Only the standard library is used (PyInstaller-friendly).
Sent state + a random installation id live in %APPDATA%\\MidasUplink\\state.json.
"""
import argparse
import gzip
import hashlib
import http.client
import json
import logging
import os
import re
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path

VERSION = "1.3.0"
APP_NAME = "Midas Companion"
DEFAULT_URL = "https://midasmarket.app/api/v1/snapshots"
LOCAL_URL = "http://127.0.0.1:8000/api/v1/snapshots"     # 0.1.0/0.2.0 default
FLAVORS = ("_classic_era_", "_forever_", "_anniversary_", "_classic_era_ptr_")
SV_NAME = "Midas_Uplink.lua"            # old sub-addon (until Midas 1.3.0)
SV_MIDAS = "Midas.lua"                  # Midas 1.3.0+: MidasDB + MidasUplinkDB
SV_NAMES = (SV_MIDAS, SV_NAME)
UPLINK_GLOBAL = "MidasUplinkDB"
MIDAS_MIN = (1, 3, 0)                   # first Midas with the built-in datalink
SENT_KEEP = 500             # remembered snapshot keys
UPLOAD_MARKETS_KEEP = 50    # last upload per market (window)
MAX_BACKOFF = 30 * 60
NAME_MAX = 120

# HTTP outcome classes
SENT, DROPPED, RETRY, CONFIG = "sent", "dropped", "retry", "config"
DROP_CODES = (400, 413, 422)
HISTORY_UNSUPPORTED = (404, 405)         # server without /history: try again later
HISTORY_RETRY_UNSUPPORTED = 6 * 3600
HISTORY_KEEP = 100                       # remembered history block keys per target
_HIST_ROWS_RE = re.compile(r"[0-9]+:[0-9]+:[0-9]+(;[0-9]+:[0-9]+:[0-9]+)*", re.ASCII)

_log = logging.getLogger("midasuplink")


def state_dir():
    """MIDAS_UPLINK_HOME if set (packaged app), else %APPDATA%\\MidasUplink.
    Read on every call so tests can redirect it."""
    home = os.environ.get("MIDAS_UPLINK_HOME")
    if home:
        return Path(home)
    return Path(os.environ.get("APPDATA") or Path.home()) / "MidasUplink"


def state_file():
    return state_dir() / "state.json"


# --------------------------------------------------------------------------- #
# Lua SavedVariables parser (the subset WoW writes: tables, strings, numbers,
# booleans, nil, comments). Not a general Lua parser.
# --------------------------------------------------------------------------- #
class LuaParseError(ValueError):
    pass


_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'",
        "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\n": "\n"}


class _Parser:
    def __init__(self, text, raw=False):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.raw = raw          # text is latin-1 decoded bytes -> strings are UTF-8

    def ws(self):
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif s.startswith("--[[", self.i):
                j = s.find("]]", self.i + 4)
                self.i = n if j < 0 else j + 2
            elif s.startswith("--", self.i):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            else:
                break

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise LuaParseError(f"{msg} (line {line})")

    def expect(self, ch):
        self.ws()
        if self.s.startswith(ch, self.i):
            self.i += len(ch)
        else:
            self.err(f"expected {ch!r}")

    def name(self):
        self.ws()
        j = self.i
        s = self.s
        while j < self.n and (s[j].isalnum() or s[j] == "_"):
            j += 1
        if j == self.i:
            self.err("expected name")
        out = s[self.i:j]
        self.i = j
        return out

    def string(self):
        q = self.s[self.i]
        self.i += 1
        out = []
        s = self.s
        while True:
            if self.i >= self.n:
                self.err("unterminated string")
            c = s[self.i]
            if c == q:
                self.i += 1
                txt = "".join(out)
                if self.raw:
                    try:
                        txt = txt.encode("latin-1").decode("utf-8")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        txt = txt.encode("latin-1", "replace").decode("utf-8", "replace")
                return txt
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    self.err("unterminated string")
                e = s[self.i]
                if e.isdigit():
                    j = self.i
                    while j < self.i + 3 and j < self.n and s[j].isdigit():
                        j += 1
                    code = int(s[self.i:j])
                    if code > 255:
                        self.err("bad escape")
                    out.append(chr(code))
                    self.i = j
                    continue
                out.append(_ESC.get(e, e))
                self.i += 1
            else:
                out.append(c)
                self.i += 1

    def number(self):
        s = self.s
        j = self.i
        if s[j] in "+-":
            j += 1
        if s.startswith(("0x", "0X"), j):
            j += 2
            while j < self.n and s[j] in string.hexdigits:
                j += 1
            v = int(s[self.i:j], 16)
        else:
            while j < self.n and (s[j].isdigit() or s[j] in ".eE" or
                                  (s[j] in "+-" and s[j - 1] in "eE")):
                j += 1
            txt = s[self.i:j]
            try:
                v = float(txt) if any(c in txt for c in ".eE") else int(txt)
            except ValueError:
                self.err(f"bad number {txt!r}")
        self.i = j
        return v

    def value(self):
        self.ws()
        if self.i >= self.n:
            self.err("unexpected end")
        c = self.s[self.i]
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string()
        if c.isdigit() or c in "+-.":
            return self.number()
        word = self.name()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "nil":
            return None
        self.err(f"unexpected {word!r}")

    def table(self):
        self.expect("{")
        arr, hsh = [], {}
        while True:
            self.ws()
            if self.i >= self.n:
                self.err("unterminated table")
            if self.s.startswith("}", self.i):
                self.i += 1
                break
            if self.s.startswith("[", self.i):
                self.i += 1
                k = self.value()
                self.expect("]")
                self.expect("=")
                hsh[k] = self.value()
            else:
                # bare  name = value  or positional value
                j = self.i
                if self.s[j].isalpha() or self.s[j] == "_":
                    nm = self.name()
                    self.ws()
                    if self.s.startswith("=", self.i) and not self.s.startswith("==", self.i):
                        self.i += 1
                        hsh[nm] = self.value()
                    else:
                        self.i = j
                        arr.append(self.value())
                else:
                    arr.append(self.value())
            self.ws()
            if self.i < self.n and self.s[self.i] in ",;":
                self.i += 1
        if not hsh:
            return arr
        for idx, v in enumerate(arr, 1):          # mixed table: positional keys
            hsh.setdefault(idx, v)
        # pure integer keys 1..n -> list
        if all(isinstance(k, int) for k in hsh) and sorted(hsh) == list(range(1, len(hsh) + 1)):
            return [hsh[k] for k in range(1, len(hsh) + 1)]
        return hsh


def parse_saved_variables(text):
    """Return {globalName: value} for a SavedVariables file given as str."""
    return _parse(_Parser(text))


def parse_saved_variables_bytes(data):
    """Same as parse_saved_variables, for raw file bytes (UTF-8 with \\ddd byte escapes)."""
    return _parse(_Parser(data.decode("latin-1"), raw=True))


def _parse(p):
    out = {}
    while True:
        p.ws()
        if p.i >= p.n:
            return out
        name = p.name()
        p.expect("=")
        out[name] = p.value()


_TOP_RE = re.compile(rb"(?m)^[A-Za-z_][A-Za-z0-9_]*[ \t]*=(?!=)")


def _line_start_is_top(data, pos):
    """pos starts a line. False when the line break before it is escaped, i.e. it
    belongs to a string ("...\\<newline>..." as Lua %q writes it)."""
    if pos == 0:
        return True
    j = pos - 1                                    # data[j] == '\n'
    if j > 0 and data[j - 1:j] == b"\r":
        j -= 1
    return j == 0 or data[j - 1:j] != b"\\"


def _find_global(data, name):
    """Offset of the top-level line  <name> = ...  in SavedVariables bytes, or -1."""
    key = name.encode("ascii")
    pat = re.compile(rb"(?m)^" + re.escape(key) + rb"[ \t]*=(?!=)")
    for m in pat.finditer(data):
        if _line_start_is_top(data, m.start()):
            return m.start()
    return -1


def extract_global(data, name=UPLINK_GLOBAL):
    """Value of one top-level global of a SavedVariables file, given as bytes,
    without parsing the other globals (Midas.lua: MidasDB can be ~50 MB, before or
    after MidasUplinkDB). None when the global is missing or nil.
    Raises LuaParseError when the block is cut off (WoW still writing)."""
    start = _find_global(data, name)
    if start < 0:
        return None
    end = len(data)
    pos = start + 1
    while True:
        m = _TOP_RE.search(data, pos)
        if m is None:
            break
        if _line_start_is_top(data, m.start()):
            end = m.start()
            break
        pos = m.start() + 1

    def parse(chunk):
        p = _Parser(chunk.decode("latin-1"), raw=True)
        p.name()
        p.expect("=")
        return p.value()

    try:
        return parse(data[start:end])
    except LuaParseError:
        if end == len(data):
            raise
        return parse(data[start:])             # next "global" was inside a string after all


def load_uplink_db(path):
    """MidasUplinkDB of Midas.lua (1.3.0+) or Midas_Uplink.lua (old sub-addon)."""
    return extract_global(Path(path).read_bytes(), UPLINK_GLOBAL)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def wow_roots(extra):
    cands = [Path(e) for e in extra]
    for d in string.ascii_uppercase:
        base = Path(f"{d}:\\")
        if not base.exists():
            continue
        for rel in ("Program Files (x86)\\World of Warcraft", "Program Files\\World of Warcraft",
                    "World of Warcraft", "Games\\World of Warcraft", "Blizzard\\World of Warcraft",
                    "Battle.net\\World of Warcraft"):
            cands.append(base / rel)
    seen, roots = set(), []
    for c in cands:
        key = str(c).lower()
        if key not in seen and c.is_dir():
            seen.add(key)
            roots.append(c)
    return roots


def sv_files(roots, flavors=FLAVORS):
    """Midas.lua (1.3.0+) and Midas_Uplink.lua (old sub-addon) of every account."""
    for root in roots:
        for flavor in flavors:
            acc = Path(root) / flavor / "WTF" / "Account"
            if acc.is_dir():
                for name in SV_NAMES:
                    yield from sorted(acc.glob(f"*/SavedVariables/{name}"))


OLD_SV_SLACK = 60           # both files are written at the same logout


def authoritative_sv(acc_sv_dir, old_addon_present):
    """The one SavedVariables file of an account that counts, or None.
    WoW never deletes the SV file of a removed addon, so an old Midas_Uplink.lua
    would keep e.g. enabled = true after sharing was turned off in Midas 1.3.0.
    Midas_Uplink.lua counts only while AddOns\\Midas_Uplink exists in that flavor
    AND (with both files) it was written at the last logout (not older than
    Midas.lua); otherwise Midas.lua counts (if present)."""
    d = Path(acc_sv_dir)
    new, old = d / SV_MIDAS, d / SV_NAME
    try:
        new_m = new.stat().st_mtime if new.is_file() else None
        old_m = old.stat().st_mtime if old.is_file() else None
    except OSError:
        return None
    if old_m is not None and old_addon_present and (new_m is None or old_m >= new_m - OLD_SV_SLACK):
        return old
    if new_m is not None:
        return new
    return None


def sv_sources(roots, flavors=FLAVORS):
    """Per account the file that counts (authoritative_sv): used for uploads,
    seen markets and the window status. sv_files() still lists both for the
    change watcher."""
    for root in roots:
        for flavor in flavors:
            base = Path(root) / flavor
            acc = base / "WTF" / "Account"
            if not acc.is_dir():
                continue
            old_present = (base / "Interface" / "AddOns" / "Midas_Uplink").is_dir()
            for d in sorted(p for p in acc.glob("*/SavedVariables") if p.is_dir()):
                f = authoritative_sv(d, old_present)
                if f is not None:
                    yield f


def normalize_wow_dir(d):
    """A folder picked by the user -> the World of Warcraft root (the folder that
    contains _classic_era_, _forever_, ...), or None. Accepts the root itself, a
    flavor folder or the WoW executable's folder one level down."""
    try:
        p = Path(d).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for cand in (p, p.parent, p.parent.parent):
        try:
            if cand.is_dir() and any((cand / fl).is_dir() for fl in FLAVORS):
                return cand
        except OSError:
            continue
    return None


def parse_version(v):
    """"1.2.28" -> (1, 2, 28); None when there is no leading number."""
    if not isinstance(v, str):
        return None
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", v)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def read_toc(addon_dir, name):
    """-> {"version": str|None, "files": [lower-case file lines]} or None when the
    .toc is missing. Only the first 64 KB are read."""
    f = Path(addon_dir) / f"{name}.toc"
    try:
        with open(f, "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError:
        return None
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    version, files = None, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("##"):
            k, _, v = s[2:].partition(":")
            if k.strip().lower() == "version" and version is None:
                version = v.strip() or None
        elif s and not s.startswith("#"):
            files.append(s.replace("/", "\\").lower())
    return {"version": version, "files": files}


# --------------------------------------------------------------------------- #
# Snapshots + names
# --------------------------------------------------------------------------- #
REQUIRED_MARKET = ("client", "region", "realm", "faction")


def as_map(t):
    """Lua table -> dict (a table with keys 1..n arrives as a list)."""
    if isinstance(t, dict):
        return t
    if isinstance(t, list):
        return {i: v for i, v in enumerate(t, 1)}
    return {}


def valid_snapshot(s):
    if not isinstance(s, dict) or s.get("v") != 1:
        return False
    mk = s.get("market")
    if not isinstance(mk, dict) or any(not mk.get(k) for k in REQUIRED_MARKET):
        return False
    if not isinstance(s.get("ts"), int) or not isinstance(s.get("items"), (dict, list)):
        return False
    s["items"] = as_map(s["items"])
    return True


def snap_key(s):
    mk = s["market"]
    return f'{mk["client"]}|{mk["region"]}|{mk["realm"]}|{mk["faction"]}|{s["ts"]}'


def valid_history(b):
    """Prepared price-history block (addon 0.4.0) -> cleaned dict or None.
    Items with a malformed key or value are left out."""
    if not isinstance(b, dict) or b.get("v") != 1 or type(b.get("created")) is not int:
        return None
    mk = b.get("market")
    if not isinstance(mk, dict) or any(not isinstance(mk.get(k), str) or not mk.get(k)
                                       for k in REQUIRED_MARKET):
        return None
    day0 = b.get("day0")
    if type(day0) is not int or day0 < 0 or day0 % 86400:
        return None
    items = {}
    for k, v in as_map(b.get("items")).items():
        if (type(k) is int and 1 <= k <= 10_000_000 and isinstance(v, str)
                and len(v) <= 4000 and _HIST_ROWS_RE.fullmatch(v)):
            items[str(k)] = v
    if not items:
        return None
    return {"created": b["created"], "addon": b.get("addon") if isinstance(b.get("addon"), str) else None,
            "market": {k: mk.get(k) for k in ("client", "region", "realm", "faction", "ruleset")
                       if isinstance(mk.get(k), str)},
            "day0": day0, "items": items}


def history_key(b):
    mk = b["market"]
    return f'{mk["client"]}|{mk["region"]}|{mk["realm"]}|{mk["faction"]}|{b["created"]}'


def history_payload(b, client_id):
    return {"format": 1, "uploader": VERSION, "client_id": client_id,
            "history": {"v": 1, "addon": b["addon"], "market": b["market"], "day0": b["day0"],
                        "items": b["items"]}}


def read_sv(path):
    """-> {"queue": [...], "history": [...], "names": {id: str}, "quality": {id: int},
    "locale": str|None}. Empty queue and history when sharing is off (opt-in respected
    even if a queue is left)."""
    data = load_uplink_db(path) or {}
    out = {"queue": [], "history": [], "names": {}, "quality": {}, "locale": None}
    if not isinstance(data, dict) or data.get("enabled") is not True:
        return out
    out["queue"] = [s for s in (data.get("queue") or []) if valid_snapshot(s)]
    hist = data.get("history")
    if isinstance(hist, dict):
        out["history"] = [b for b in (valid_history(v) for _k, v in sorted(hist.items(), key=lambda kv: str(kv[0])))
                          if b is not None]
    out["names"] = as_map(data.get("names"))
    out["quality"] = as_map(data.get("quality"))
    loc = data.get("locale")
    out["locale"] = loc if isinstance(loc, str) and loc else None
    return out


def read_queue(path):
    return read_sv(path)["queue"]


def clean_name(nm):
    if not isinstance(nm, str):
        return None
    nm = nm.strip()
    if not nm or len(nm) > NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in nm):
        return None
    return nm


def names_for(s, sv, already):
    """Names block for snapshot s: only items of s, with name + quality known,
    not yet in `already` (set of int ids). None when nothing is new."""
    loc = sv.get("locale")
    if not loc:
        return None
    names, qual = sv.get("names") or {}, sv.get("quality") or {}
    items = {}
    for k in s["items"]:
        try:
            iid = int(k)
        except (TypeError, ValueError):
            continue
        if iid in already:
            continue
        nm = clean_name(names.get(iid))
        q = qual.get(iid)
        if nm is None or not isinstance(q, int) or isinstance(q, bool) or not 0 <= q <= 7:
            continue
        items[str(iid)] = [nm, q]
    return {"locale": loc, "items": items} if items else None


def payload(s, client_id, names=None):
    body = {
        "format": 1,
        "uploader": VERSION,
        "client_id": client_id,
        "snapshot": {
            "ts": s["ts"],
            "addon": s.get("addon"),
            "market": {k: s["market"].get(k) for k in ("client", "region", "realm", "faction", "ruleset")},
            "count": sum(1 for v in s["items"].values() if isinstance(v, str)),
            "items": {str(k): v for k, v in s["items"].items() if isinstance(v, str)},
        },
    }
    if names:
        body["names"] = names
    return body


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def encode_ids(ids):
    """{1,2,3,7,9,10} -> "1-3,7,9-10" (compact storage for names_sent)."""
    out, run = [], None
    for i in sorted(ids):
        if run and i == run[1] + 1:
            run[1] = i
        else:
            if run:
                out.append(run)
            run = [i, i]
    if run:
        out.append(run)
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def decode_ids(txt):
    ids = set()
    for part in (txt or "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            continue
        ids.update(range(lo, hi + 1))
    return ids


def names_key(url, locale):
    return f"{url}|{locale}"


def _is_hex32(v):
    return isinstance(v, str) and len(v) == 32 and all(c in string.hexdigits for c in v)


def _keep_corrupt(f, why):
    """Move an unreadable state file aside instead of silently starting over."""
    dst = f.with_name(f"state_corrupt_{time.strftime('%Y%m%d_%H%M%S')}.json")
    try:
        os.replace(f, dst)
        _log.warning("state.json unreadable (%s) - kept as %s, starting fresh", why, dst.name)
    except OSError as e:
        _log.warning("state.json unreadable (%s), could not move it aside: %s", why, e)


def load_state():
    f = state_file()
    st = {}
    raw = None
    for attempt in range(5):
        try:
            raw = f.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)
    if raw is not None:
        try:
            st = json.loads(raw)
            if not isinstance(st, dict):
                raise ValueError("not an object")
        except ValueError as e:
            _keep_corrupt(f, e)
            st = {}
    # type checks, field by field
    if not _is_hex32(st.get("client_id")):
        st["client_id"] = uuid.uuid4().hex
    sent = st.get("sent")
    if isinstance(sent, list):                     # 0.1.0 layout: no target yet
        sent = {LOCAL_URL: sent}
    if not isinstance(sent, dict):
        sent = {}
    st["sent"] = {t: [k for k in v if isinstance(k, str)] for t, v in sent.items()
                  if isinstance(t, str) and isinstance(v, list)}
    ns = st.get("names_sent")
    st["names_sent"] = ({k: v for k, v in ns.items() if isinstance(k, str) and isinstance(v, str)}
                        if isinstance(ns, dict) else {})
    stats = st.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    st["stats"] = {k: (stats.get(k) if isinstance(stats.get(k), int) and not isinstance(stats.get(k), bool)
                       else 0) for k in ("uploads", "last")}
    pe = st.get("prices_enabled")
    st["prices_enabled"] = pe if isinstance(pe, bool) else True
    tags = st.get("prices_etag")
    st["prices_etag"] = ({k: v for k, v in tags.items() if isinstance(k, str) and isinstance(v, str)}
                         if isinstance(tags, dict) else {})
    pm = st.get("prices_market")
    st["prices_market"] = ({k: v for k, v in pm.items() if isinstance(k, str) and isinstance(v, str)}
                           if isinstance(pm, dict) else {})
    pc = st.get("prices_checked")
    st["prices_checked"] = ({k: v for k, v in pc.items()
                             if isinstance(k, str) and type(v) is int and v >= 0}
                            if isinstance(pc, dict) else {})
    nb = st.get("prices_not_before")
    st["prices_not_before"] = nb if type(nb) is int and nb >= 0 else 0
    hs = st.get("history_sent")
    st["history_sent"] = ({t: [k for k in v if isinstance(k, str)] for t, v in hs.items()
                           if isinstance(t, str) and isinstance(v, list)}
                          if isinstance(hs, dict) else {})
    hn = st.get("history_not_before")
    st["history_not_before"] = hn if type(hn) is int and hn >= 0 else 0
    pl = st.get("prices_last")
    st["prices_last"] = pl if isinstance(pl, int) and not isinstance(pl, bool) and pl >= 0 else 0
    # 1.3.0: window / status
    um = st.get("upload_markets")
    st["upload_markets"] = ({k: {"ts": v["ts"], "at": v["at"]} for k, v in um.items()
                             if isinstance(k, str) and isinstance(v, dict)
                             and type(v.get("ts")) is int and type(v.get("at")) is int}
                            if isinstance(um, dict) else {})
    pi = st.get("prices_info")
    st["prices_info"] = ({k: {"ts": v["ts"], "items": v["items"]} for k, v in pi.items()
                          if isinstance(k, str) and isinstance(v, dict)
                          and type(v.get("ts")) is int and type(v.get("items")) is int}
                         if isinstance(pi, dict) else {})
    wd = st.get("wow_dirs")
    st["wow_dirs"] = [d for d in wd if isinstance(d, str) and d][:10] if isinstance(wd, list) else []
    st["window_shown"] = st.get("window_shown") is True
    return st


def sent_list(st, target):
    return st["sent"].setdefault(target, [])


def save_state(st):
    """Atomic write (flush + fsync + replace). Returns False when Windows keeps
    the file locked (virus scanner, sync client) after a few tries."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    for lst in st["sent"].values():
        del lst[:-SENT_KEEP]                      # in place: callers hold the list
    for lst in st.get("history_sent", {}).values():
        del lst[:-HISTORY_KEEP]
    f = state_file()
    tmp = f.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, f)
            return True
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    _log.warning("could not write %s (locked) - will retry with the next change", f)
    return False


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would turn the POST into a GET and the
    snapshot would be lost silently. A 3xx is reported as configuration error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_deadline = threading.local()       # .end (monotonic) while a price request runs


def _arm_deadline(conn):
    """Price requests: close the socket when the overall deadline passes, so
    slowly dripping headers or bodies cannot hold the request open."""
    end = getattr(_deadline, "end", None)
    if end is None or conn.sock is None:
        return
    sock = conn.sock

    def cut():
        try:
            sock.shutdown(2)                    # SHUT_RDWR: pending recv returns at once
        except OSError:
            pass

    t = threading.Timer(max(0.0, end - _clock()), cut)
    t.daemon = True
    t.start()
    _deadline.timer = t


def _limit_connect(conn):
    end = getattr(_deadline, "end", None)
    if end is not None:
        left = max(0.1, end - _clock())
        conn.timeout = left if conn.timeout is None else min(conn.timeout, left)


class _DLHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        _limit_connect(self)
        super().connect()
        _arm_deadline(self)


class _DLHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        _limit_connect(self)
        super().connect()
        _arm_deadline(self)


class _DLHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_DLHTTPConnection, req)


class _DLHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_DLHTTPSConnection, req, context=self._context)


_opener = urllib.request.build_opener(_NoRedirect, _DLHTTPHandler, _DLHTTPSHandler)


class Sender:
    def __init__(self, url, outbox=None, timeout=30):
        self.url = url
        self.outbox = Path(outbox) if outbox else None
        self.timeout = timeout

    @property
    def target(self):
        return f"outbox:{self.outbox}" if self.outbox else self.url

    @property
    def history_url(self):
        return history_url(self.url)

    def send(self, body):
        """-> (SENT|DROPPED|RETRY|CONFIG, info)"""
        if self.outbox:
            self.outbox.mkdir(parents=True, exist_ok=True)
            s = body["snapshot"]
            mk = s["market"]
            name = f'{s["ts"]}_{mk["client"]}_{mk["region"]}_{mk["faction"]}.json'
            (self.outbox / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            return SENT, "outbox " + name
        result, info, _code = self._post(self.url, body)
        return result, info

    def send_history(self, body):
        """Price-history block -> (SENT|DROPPED|RETRY|CONFIG, info, HTTP code or None)."""
        if self.outbox:
            self.outbox.mkdir(parents=True, exist_ok=True)
            mk = body["history"]["market"]
            name = f'history_{mk["client"]}_{mk["region"]}_{mk["faction"]}_{body["history"]["day0"]}.json'
            (self.outbox / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            return SENT, "outbox " + name, None
        url = self.history_url
        if not url:
            return DROPPED, "no history URL for " + self.url, None
        return self._post(url, body)

    def _post(self, url, body):
        """-> (SENT|DROPPED|RETRY|CONFIG, info, HTTP code or None)"""
        raw = gzip.compress(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        req = urllib.request.Request(url, data=raw, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip",
            "User-Agent": f"MidasUplink/{VERSION}"})
        try:
            with _opener.open(req, timeout=self.timeout) as r:
                info = f"HTTP {r.status}"
                try:
                    st = json.loads(r.read() or b"{}").get("status")
                    if st:
                        info += f" {st}"
                except (ValueError, AttributeError):
                    pass
                if 200 <= r.status < 300:
                    return SENT, info, r.status
                if 300 <= r.status < 400:
                    return CONFIG, info + " (redirect)", r.status
                return RETRY, info, r.status
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                loc = e.headers.get("Location") if e.headers else None
                return CONFIG, (f"HTTP {e.code} redirect to {loc or '?'} - upload URL is wrong, "
                                f"check --url / MIDAS_UPLINK_URL"), e.code
            if e.code in DROP_CODES:                 # invalid: never retry
                return DROPPED, f"HTTP {e.code} (dropped)", e.code
            return RETRY, f"HTTP {e.code}", e.code   # 429, 5xx, anything else: later
        except (urllib.error.URLError, OSError) as e:
            return RETRY, str(getattr(e, "reason", e)), None


# run_pass results
PASS_OK, PASS_RETRY, PASS_CONFIG = "ok", "retry", "config"


def run_pass(roots, sender, st, log):
    """One pass over all SV files.
    -> PASS_OK (nothing left), PASS_RETRY (server busy/down or a file could not
    be read yet), PASS_CONFIG (redirect: wrong URL)."""
    target = sender.target
    sent_l = sent_list(st, target)
    sent = set(sent_l)
    result_all = PASS_OK
    for path in sv_sources(roots):
        try:
            sv = read_sv(path)
        except (OSError, LuaParseError) as e:
            log(f"cannot read {path} yet ({e}) - retrying later")
            result_all = PASS_RETRY                  # WoW may still be writing it
            continue
        nkey = names_key(target, sv["locale"]) if sv["locale"] else None
        for s in sv["queue"]:
            k = snap_key(s)
            if k in sent:
                continue
            already = decode_ids(st["names_sent"].get(nkey, "")) if nkey else set()
            names = names_for(s, sv, already)
            result, info = sender.send(payload(s, st["client_id"], names))
            mk = s["market"]
            extra = f', {len(names["items"])} names' if names else ""
            log(f'{result} {mk["client"]}/{mk["region"]}/{mk.get("ruleset")}/'
                f'{mk["faction"]} {time.strftime("%d.%m. %H:%M", time.localtime(s["ts"]))} '
                f'{len(s["items"])} items{extra} - {info}')
            if result == RETRY:
                return PASS_RETRY                    # server busy/down: retry later
            if result == CONFIG:
                log("CONFIGURATION ERROR: the server answered with a redirect. "
                    "Nothing is sent until the upload URL is fixed.")
                return PASS_CONFIG
            sent_l.append(k)
            sent.add(k)
            if result == SENT:
                if names:
                    already.update(int(i) for i in names["items"])
                    st["names_sent"][nkey] = encode_ids(already)
                st["stats"]["uploads"] = st["stats"].get("uploads", 0) + 1
                st["stats"]["last"] = int(time.time())
                um = st.setdefault("upload_markets", {})
                um["|".join(str(mk[f]) for f in REQUIRED_MARKET)] = {"ts": s["ts"], "at": st["stats"]["last"]}
                while len(um) > UPLOAD_MARKETS_KEEP:
                    del um[min(um, key=lambda q: um[q]["at"])]
            save_state(st)
        res = send_history_blocks(sv["history"], sender, st, log)
        if res != PASS_OK:
            return res
    return result_all


def send_history_blocks(blocks, sender, st, log):
    """Post every prepared price-history block not sent to this target yet (once each).
    -> PASS_OK, PASS_RETRY or PASS_CONFIG (like snapshots)."""
    if not blocks or int(time.time()) < st.get("history_not_before", 0):
        return PASS_OK
    done = st["history_sent"].setdefault(sender.target, [])
    for b in blocks:
        k = history_key(b)
        if k in done:
            continue
        result, info, code = sender.send_history(history_payload(b, st["client_id"]))
        mk = b["market"]
        log(f'{result} history {mk["client"]}/{mk["region"]}/{mk.get("ruleset")}/{mk["faction"]} '
            f'{len(b["items"])} items - {info}')
        if code in HISTORY_UNSUPPORTED:
            st["history_not_before"] = int(time.time()) + HISTORY_RETRY_UNSUPPORTED
            save_state(st)
            log("server does not accept price history yet - next try in 6 h")
            return PASS_OK
        if result == RETRY:
            return PASS_RETRY
        if result == CONFIG:
            log("CONFIGURATION ERROR: the server answered with a redirect. "
                "Nothing is sent until the upload URL is fixed.")
            return PASS_CONFIG
        done.append(k)
        save_state(st)
    return PASS_OK


# --------------------------------------------------------------------------- #
# Downlink: server prices -> <flavor>\Interface\AddOns\Midas_Uplink\Prices.lua
# --------------------------------------------------------------------------- #
PRICE_FLAVORS = ("_forever_", "_classic_era_", "_anniversary_")
PRICE_ADDON = "Midas_Uplink"            # old sub-addon: written as long as the folder exists
MIDAS_ADDON = "Midas"                   # 1.3.0+: written when Midas.toc lists Prices.lua
PRICE_FILE = "Prices.lua"
PRICE_CHECKED_STEP = 3 * 3600           # unchanged data: refresh `checked` in the file at most this often
SEEN_FIELDS = ("client", "region", "realm", "faction")
SEEN_DAYS = 30
PRICES_EVERY = 30 * 60          # regular check; also after every upload pass
PRICES_MIN_GAP = 60             # no second check within a minute
PRICE_TIMEOUT = 20              # per socket operation
PRICE_DEADLINE = 60             # whole request incl. body (slow-drip servers)
PRICE_MAX_PACKED = 8 * 1024 * 1024    # per answer as transferred (gzip)
PRICE_MAX_BYTES = 32 * 1024 * 1024    # per answer unpacked (7,100 items x 90 days ~ 11 MB)
PRICE_MAX_ITEMS = 30_000        # per market
PRICE_MAX_MARKETS = 5           # most recently seen markets per flavor
PRICE_FILE_MAX = 32 * 1024 * 1024   # whole Prices.lua; oldest-seen markets are left out
EXPORT_DAYS = 90                # asked for with days=; servers before 17.11.2026 only know 14
PRICE_RECHECK = 24 * 3600       # a raw market is fetched itself at least this often
RETRY_AFTER_MAX = 24 * 3600     # cap for a server's Retry-After
PRICE_CACHE_DAYS = 35
PRICE_TMP_AGE = 3600            # leftover tmp files older than this are removed
ITEM_ID_MAX = 10_000_000
MAX_DAY_OFF = 119               # server sends 90 days (off 0..89) or 14 (0..13); up to 119 tolerated

# fetch outcomes: P_NONE = no prices for this market (404/400/422), go on
P_OK, P_SAME, P_NONE, P_ERROR = "ok", "same", "none", "error"
SKIP_CODES = (400, 404, 422)
_MARKET_RE = re.compile(r"[A-Za-z0-9/_.-]{1,200}", re.ASCII)
_ITEM_KEY_RE = re.compile(r"[1-9][0-9]{0,7}", re.ASCII)
_ITEMS_RE = re.compile(r"[0-9]+:[0-9]+:[0-9]+(;[0-9]+:[0-9]+:[0-9]+)*", re.ASCII)
_WRITTEN_RE = re.compile(rb"written = [0-9]+")
_CHECKED_RE = re.compile(rb"checked = ([0-9]+)")
_clock = time.monotonic         # own binding: tests may patch time.monotonic


def _is_int(v, lo=None, hi=None):
    return type(v) is int and (lo is None or v >= lo) and (hi is None or v <= hi)


def _item_id(k):
    """Strict item key: ASCII digits, no sign/space/leading zero, 1..ITEM_ID_MAX."""
    if not isinstance(k, str) or not _ITEM_KEY_RE.fullmatch(k):
        return None
    iid = int(k)
    return iid if iid <= ITEM_ID_MAX else None


def export_url(upload_url):
    """.../api/v1/snapshots -> .../api/v1/export (None when it cannot be derived)."""
    parts = urllib.parse.urlsplit(upload_url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/snapshots"):
        path = path[: -len("snapshots")] + "export"
    elif not path:
        path = "/api/v1/export"
    else:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def history_url(upload_url):
    """.../api/v1/snapshots -> .../api/v1/history (None when it cannot be derived)."""
    ex = export_url(upload_url)
    return ex[: -len("export")] + "history" if ex else None


def parse_seen(path):
    """All market entries of MidasUplinkDB.seen (unfiltered, opt-in not needed)."""
    return seen_of_db(load_uplink_db(path))


def seen_of_db(data):
    if not isinstance(data, dict):
        return []
    out = []
    for e in as_map(data.get("seen")).values():
        if not isinstance(e, dict):
            continue
        mk = {k: e.get(k) for k in SEEN_FIELDS}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in mk.values()):
            continue
        if not _is_int(e.get("last"), 0):
            continue
        rs = e.get("ruleset")
        mk["ruleset"] = rs if isinstance(rs, str) and rs.strip() and len(rs) <= 40 else None
        mk["last"] = e["last"]
        out.append(mk)
    return out


def read_seen(path, now=None):
    """Markets of one SV file seen in the last SEEN_DAYS days."""
    now = int(time.time()) if now is None else now
    return [m for m in parse_seen(path) if now - m["last"] <= SEEN_DAYS * 86400]


def raw_key(mk):
    return tuple(mk[k] for k in SEEN_FIELDS)


def _seen_of(path, cache):
    """parse_seen with a (mtime, size) cache. None when the file cannot be read
    and nothing is known about it yet (WoW may still be writing)."""
    key = str(path)
    hit = cache.get(key) if cache is not None else None
    try:
        s = path.stat()
        sig = (s.st_mtime_ns, s.st_size)
    except OSError:
        return hit[1] if hit else None
    if hit and hit[0] == sig:
        return hit[1]
    try:
        seen = parse_seen(path)
    except (OSError, LuaParseError):
        return hit[1] if hit else None
    if cache is not None:
        cache[key] = (sig, seen)
    return seen


def midas_lists_prices(addon_dir):
    """True when AddOns\\Midas\\Midas.toc loads Prices.lua (Midas 1.3.0+)."""
    toc = read_toc(addon_dir, MIDAS_ADDON)
    return bool(toc) and PRICE_FILE.lower() in toc["files"]


def flavor_price_dirs(base):
    """Prices.lua folders of one flavor folder: AddOns\\Midas when its .toc lists
    Prices.lua, AddOns\\Midas_Uplink as long as it exists."""
    addons = Path(base) / "Interface" / "AddOns"
    out = []
    midas = addons / MIDAS_ADDON
    if midas.is_dir() and midas_lists_prices(midas):
        out.append(midas)
    old = addons / PRICE_ADDON
    if old.is_dir():
        out.append(old)
    return out


def price_targets(roots, now=None, cache=None):
    """-> ([(addon folder, [raw market, ...]), ...], complete).
    Every Prices.lua folder (flavor_price_dirs) of the price flavors, with the
    markets seen there in the last 30 days (may be empty: the file is then written
    with markets = {}). complete=False when a SV file could not be read (that
    flavor is skipped this time)."""
    now = int(time.time()) if now is None else now
    out, complete = [], True
    for root in roots:
        for flavor in PRICE_FLAVORS:
            base = Path(root) / flavor
            dirs = flavor_price_dirs(base)
            if not dirs:
                continue
            markets, skip = {}, False
            for f in sv_sources([root], (flavor,)):
                seen = _seen_of(f, cache)
                if seen is None:
                    skip = True
                    break
                for mk in seen:
                    if now - mk["last"] > SEEN_DAYS * 86400:
                        continue
                    k = raw_key(mk)
                    if k not in markets or markets[k]["last"] < mk["last"]:
                        markets[k] = mk
            if skip:
                complete = False
                continue
            recent = sorted(markets.values(), key=lambda m: (-m["last"], raw_key(m)))
            chosen = sorted(recent[:PRICE_MAX_MARKETS], key=raw_key)
            for d in dirs:
                out.append((d, chosen))
    return out, complete


def price_addon_dirs(roots):
    for root in roots:
        for flavor in PRICE_FLAVORS:
            yield from flavor_price_dirs(Path(root) / flavor)


def clean_price_tmp(roots, max_age=PRICE_TMP_AGE, now=None):
    """Remove leftover .Prices.lua.*.tmp (crash during a write) older than max_age,
    only inside the Prices.lua folders (Midas, Midas_Uplink). -> number removed."""
    now = time.time() if now is None else now
    n = 0
    for d in price_addon_dirs(roots):
        try:
            leftovers = list(d.glob(f".{PRICE_FILE}.*.tmp"))
        except OSError:
            continue
        for f in leftovers:
            try:
                if f.is_file() and now - f.stat().st_mtime > max_age:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n


def export_params(mk):
    params = [(k, mk[k]) for k in SEEN_FIELDS]
    if mk.get("ruleset"):
        params.append(("ruleset", mk["ruleset"]))
    return params


def query_key(base, mk):
    return base + "?" + urllib.parse.urlencode(export_params(mk))


class TooManyItems(Exception):
    """Export of one market exceeds PRICE_MAX_ITEMS: the market is skipped."""


def clean_export(obj):
    """Validated export answer -> {market, ts, day0, items: {"<id>": "off:low:qty;..."}}
    or None. Bad points/items are dropped, a bad envelope rejects the answer.
    Raises TooManyItems above PRICE_MAX_ITEMS items."""
    if not isinstance(obj, dict) or not _is_int(obj.get("v")) or obj["v"] != 1:
        return None
    market, ts, day0, items = obj.get("market"), obj.get("ts"), obj.get("day0"), obj.get("items")
    if (not isinstance(market, str) or not _MARKET_RE.fullmatch(market)
            or not _is_int(ts, 0) or not _is_int(day0, 0) or not isinstance(items, dict)):
        return None
    if len(items) > PRICE_MAX_ITEMS:
        raise TooManyItems(len(items))
    days = obj.get("days")
    # off 0..days-1 of the answer's own period (90 or 14); without a usable days 0..MAX_DAY_OFF
    max_off = days - 1 if _is_int(days, 1, MAX_DAY_OFF + 1) else MAX_DAY_OFF
    out = {}
    for k, pts in items.items():
        iid = _item_id(k)
        if iid is None or not isinstance(pts, list):
            continue
        per_day = {}
        for p in pts:
            if not (isinstance(p, list) and len(p) == 3 and _is_int(p[0], 0, max_off)
                    and _is_int(p[1], 1) and _is_int(p[2], 0)):
                continue
            off, low, qty = p
            if off in per_day:
                low, qty = min(low, per_day[off][0]), max(qty, per_day[off][1])
            per_day[off] = (low, qty)
        if per_day:
            out[str(iid)] = ";".join(f"{o}:{lo}:{q}" for o, (lo, q) in sorted(per_day.items()))
    return {"market": market, "ts": ts, "day0": day0, "items": out}


def _valid_cached(d):
    return (isinstance(d, dict) and isinstance(d.get("market"), str) and _MARKET_RE.fullmatch(d["market"])
            and _is_int(d.get("ts"), 0) and _is_int(d.get("day0"), 0)
            and isinstance(d.get("items"), dict) and len(d["items"]) <= PRICE_MAX_ITEMS
            and all(_item_id(k) is not None and isinstance(v, str) and _ITEMS_RE.fullmatch(v)
                    for k, v in d["items"].items()))


class _Deadline(Exception):
    pass


def _read_body(r, deadline):
    """Read at most PRICE_MAX_PACKED bytes from the wire and PRICE_MAX_BYTES unpacked,
    unpacking gzip on the fly; stop at the deadline."""
    gz = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
    wire_max = PRICE_MAX_PACKED if gz else PRICE_MAX_BYTES
    raw_n, out = 0, []
    size = 0
    while True:
        if _clock() > deadline:
            raise _Deadline()
        chunk = r.read1(64 * 1024)          # one recv: a slow drip cannot outlast the deadline
        if not chunk:
            break
        raw_n += len(chunk)
        if raw_n > wire_max:
            raise ValueError("export too large")
        if dec is not None:
            chunk = dec.decompress(chunk, PRICE_MAX_BYTES + 1 - size)
            if dec.unconsumed_tail:
                raise ValueError("export too large")
        size += len(chunk)
        if size > PRICE_MAX_BYTES:
            raise ValueError("export too large")
        out.append(chunk)
    if dec is not None:
        tail = dec.flush()
        size += len(tail)
        if size > PRICE_MAX_BYTES:
            raise ValueError("export too large")
        out.append(tail)
    return b"".join(out)


def retry_after_seconds(value, now=None):
    """Retry-After header (seconds or HTTP date) -> seconds >= 0, None if unusable."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value, re.ASCII):
        return int(value)
    try:
        import email.utils
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    now = time.time() if now is None else now
    return max(0, int(when.timestamp() - now))


def fetch_export(base, mk, etag=None, timeout=None, deadline=None, days=EXPORT_DAYS):
    """GET the export of one raw market (days=90; a server that rejects it with 422 is
    asked once more without days, i.e. its 14-day default).
    -> (P_OK|P_SAME|P_NONE|P_ERROR, data, etag, info, retry_after seconds or None)
    P_NONE (404/400/422, too many items): no prices for this market, others go on.
    One deadline covers connect, headers and body of BOTH requests (the fallback does not
    get a fresh 60 s)."""
    deadline = PRICE_DEADLINE if deadline is None else deadline
    end = _clock() + deadline
    res = _fetch_export(base, mk, etag, timeout, deadline, end, days)
    if days and res[0] == P_NONE and res[3].startswith("HTTP 422"):
        if _clock() >= end:
            return P_ERROR, None, None, f"no complete answer within {deadline} s", None
        res = _fetch_export(base, mk, etag, timeout, deadline, end, None)
    return res


def _fetch_export(base, mk, etag, timeout, deadline, end, days):
    timeout = PRICE_TIMEOUT if timeout is None else timeout
    url = query_key(base, mk)
    if days:
        url += "&" + urllib.parse.urlencode([("days", days)])
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip",
               "User-Agent": f"MidasUplink/{VERSION}"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers, method="GET")
    _deadline.end = end
    late = f"no complete answer within {deadline} s"
    try:
        with _opener.open(req, timeout=timeout) as r:
            if r.status != 200:
                return P_ERROR, None, None, f"HTTP {r.status}", None
            raw = _read_body(r, end)
            new_etag = r.headers.get("ETag")
        data = clean_export(json.loads(raw.decode("utf-8")))
        if data is None:
            return P_ERROR, None, None, "invalid export answer", None
        return P_OK, data, new_etag, f"HTTP 200, {len(data['items'])} items", None
    except TooManyItems as e:
        return (P_NONE, None, None,
                f"export has {e.args[0]} items (limit {PRICE_MAX_ITEMS}) - market skipped", None)
    except _Deadline:
        return P_ERROR, None, None, late, None
    except urllib.error.HTTPError as e:
        wait = retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
        e.close()
        if e.code == 304:
            return P_SAME, None, etag, "HTTP 304", None
        if e.code in SKIP_CODES:
            return P_NONE, None, None, f"HTTP {e.code} (no prices for this market)", None
        if 300 <= e.code < 400:
            return P_ERROR, None, None, f"HTTP {e.code} redirect - check --url / MIDAS_UPLINK_URL", None
        return P_ERROR, None, None, f"HTTP {e.code}", (wait if e.code == 429 else None)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, zlib.error,
            RecursionError) as e:
        if _clock() >= end:
            return P_ERROR, None, None, late, None
        return P_ERROR, None, None, str(getattr(e, "reason", e)) or type(e).__name__, None
    finally:
        _deadline.end = None
        t = getattr(_deadline, "timer", None)
        if t is not None:
            t.cancel()
            _deadline.timer = None


def price_cache_dir():
    return state_dir() / "prices_cache"


def _cache_file(q):
    return price_cache_dir() / (hashlib.sha1(q.encode("utf-8")).hexdigest()[:20] + ".json")


def load_price_cache(q):
    try:
        d = json.loads(_cache_file(q).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("q") != q or not _valid_cached(d.get("data")):
        return None
    return d["data"]


def save_price_cache(q, data):
    f = _cache_file(q)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"q": q, "data": data}, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, f)
    os.utime(f)


def prune_price_cache(keep, now=None):
    """Remove cache files not in use for PRICE_CACHE_DAYS."""
    now = time.time() if now is None else now
    keep_names = {_cache_file(q).name for q in keep}
    try:
        files = list(price_cache_dir().glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            if f.name in keep_names:
                os.utime(f)
            elif now - f.stat().st_mtime > PRICE_CACHE_DAYS * 86400:
                f.unlink()
        except OSError:
            pass


def merge_markets(results):
    """[(raw market, data), ...] -> one entry per server market; `match` collects
    the raw fields of every raw market that maps to it. Newest ts wins."""
    groups = {}
    for mk, data in results:
        g = groups.get(data["market"])
        if g is None or data["ts"] > g["ts"]:
            g = {"market": data["market"], "ts": data["ts"], "day0": data["day0"],
                 "items": data["items"], "match": g["match"] if g else []}
            groups[data["market"]] = g
        m = {k: mk[k] for k in SEEN_FIELDS}
        if m not in g["match"]:
            g["match"].append(m)
        g["last"] = max(g.get("last", 0), mk.get("last") or 0)
    for g in groups.values():
        g["match"].sort(key=raw_key)
    return [groups[k] for k in sorted(groups)]


def fit_file_size(merged, limit=None):
    """Drop the markets seen longest ago until Prices.lua stays within the
    limit. -> (kept markets, dropped market keys)"""
    limit = PRICE_FILE_MAX if limit is None else limit
    kept, dropped = list(merged), []
    while kept and len(render_prices(kept, 0).encode("utf-8")) > limit:
        oldest = min(kept, key=lambda g: (g.get("last", 0), g["market"]))
        kept.remove(oldest)
        dropped.append(oldest["market"])
    return kept, dropped


def lua_quote(s):
    """Lua string literal: escape backslash, quote and newline, drop other
    control characters."""
    out = []
    for c in str(s):
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif ord(c) < 32 or ord(c) == 127:
            continue
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def render_prices(markets, written, checked=None):
    """Prices.lua text (SPEC 9.3, Datalink 1.3.0: app + checked in the head).
    Integers only, strings via lua_quote. Each market is built inside its own
    function, so the Lua 5.1 constant limit (2^18 per function) applies per
    market, not to the whole file."""
    checked = written if checked is None else checked
    lines = [f"MidasUplinkPrices = {{ v = 1, app = {lua_quote(VERSION)}, written = {int(written)}, "
             f"checked = {int(checked)}, markets = {{"]
    for g in markets:
        lines.append(f"  (function() return {{ market = {lua_quote(g['market'])}, "
                     f"ts = {int(g['ts'])}, day0 = {int(g['day0'])},")
        match = "".join("{ " + ", ".join(f"{k}={lua_quote(m[k])}" for k in SEEN_FIELDS) + " }, "
                        for m in g["match"])
        lines.append("    match = { " + match + "},")
        lines.append("    items = {")
        for iid in sorted(g["items"], key=int):
            lines.append(f"      [{int(iid)}] = {lua_quote(g['items'][iid])},")
        lines.append("    } } end)(),")
    lines.append("} }")
    return "\n".join(lines) + "\n"


def has_prices(addon_dir):
    """True when Prices.lua in this folder holds at least one market (written by the
    app). A placeholder, a missing file or markets = {} -> False."""
    try:
        return b"(function() return" in (Path(addon_dir) / PRICE_FILE).read_bytes()
    except OSError:
        return False


def write_prices(addon_dir, markets, now=None, checked=None):
    """Write Prices.lua atomically into an existing addon folder.
    `written` and `checked` are ignored when comparing with the file on disk;
    `checked` alone is refreshed at most every PRICE_CHECKED_STEP seconds.
    -> "written" | "unchanged" | "missing". Raises OSError when it cannot write
    (the old file stays)."""
    addon_dir = Path(addon_dir)
    if not addon_dir.is_dir():
        return "missing"
    now = int(time.time()) if now is None else int(now)
    checked = now if checked is None else int(checked)
    target = addon_dir / PRICE_FILE
    new = render_prices(markets, 0, 0).encode("utf-8")
    try:
        old = target.read_bytes()
    except OSError:
        old = None
    if old is not None:
        m = _CHECKED_RE.search(old[:400])
        norm = _WRITTEN_RE.sub(b"written = 0", old, count=1)
        norm = _CHECKED_RE.sub(b"checked = 0", norm, count=1)
        if norm == new and m and abs(checked - int(m.group(1))) < PRICE_CHECKED_STEP:
            return "unchanged"
    data = render_prices(markets, now, checked).encode("utf-8")
    fd, tmpname = tempfile.mkstemp(dir=addon_dir, prefix=f".{PRICE_FILE}.", suffix=".tmp")
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return "written"
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return "written"


def run_prices(roots, url, st, log, now=None, cache=None):
    """One downlink pass. -> (PASS_OK|PASS_RETRY|PASS_CONFIG, summary text).
    On any fetch error the affected Prices.lua files stay as they are."""
    now = int(time.time()) if now is None else now
    base = export_url(url)
    if not base:
        msg = f"server prices: cannot derive the export URL from {url}"
        log(msg)
        return PASS_CONFIG, msg
    if now < st["prices_not_before"] <= now + RETRY_AFTER_MAX:     # clock set back: ignore
        msg = ("server prices: server asked to wait until "
               + time.strftime("%d.%m. %H:%M", time.localtime(st["prices_not_before"])))
        _log.info(msg)
        return PASS_RETRY, msg
    targets, complete = price_targets(roots, now, cache)
    etags = st["prices_etag"]
    pmap = st["prices_market"]          # query key -> server market key (last answer)
    checked = st["prices_checked"]      # query key -> last real request (unix)
    fresh = {}                          # server market -> data answered in this pass
    fetched, used = {}, set()
    result = PASS_OK if complete else PASS_RETRY
    failed_all = False
    n_written = n_same = 0
    info_out = {}                       # server market -> {ts, items} (window)
    for addon, markets in targets:
        results, failed = [], False
        for mk in markets:
            q = query_key(base, mk)
            used.add(q)
            if q not in fetched:
                known = pmap.get(q)
                recent = 0 <= now - checked.get(q, 0) < PRICE_RECHECK
                if known in fresh and recent:   # sibling raw market already brought it
                    fetched[q] = (P_OK, fresh[known])
                elif failed_all:
                    fetched[q] = (P_ERROR, None)
                else:
                    cached = load_price_cache(q)
                    status, data, etag, info, wait = fetch_export(base, mk, etags.get(q) if cached else None)
                    if status != P_ERROR:
                        checked[q] = now
                    elif wait is not None and wait > PRICES_EVERY:
                        st["prices_not_before"] = now + min(wait, RETRY_AFTER_MAX)
                        info += f", Retry-After {wait} s"
                    if status == P_OK:
                        try:
                            save_price_cache(q, data)
                        except OSError:
                            pass
                        if etag:
                            etags[q] = etag
                        else:
                            etags.pop(q, None)
                    elif status in (P_SAME, P_NONE):
                        data = cached                 # 404/400/422: keep the last known prices
                        if status == P_SAME and data is None:
                            status, info = P_ERROR, "HTTP 304 without cached data"
                    if status in (P_OK, P_SAME):
                        pmap[q] = data["market"]
                        fresh[data["market"]] = data
                    elif status == P_NONE:
                        _log.info(f'server prices: {mk["client"]}/{mk["region"]}/{mk["realm"]}/'
                                  f'{mk["faction"]} - {info}')
                    else:
                        failed_all = True             # offline / server trouble: stop here
                        log(f'server prices: {mk["client"]}/{mk["region"]}/{mk["realm"]}/'
                            f'{mk["faction"]} - {info}; old files stay')
                    fetched[q] = (status, data)
            status, data = fetched[q]
            if status == P_ERROR:
                failed = True
                break
            if data:
                results.append((mk, data))
        if failed:
            result = PASS_RETRY
            continue
        try:
            merged = []
            if results:
                merged, dropped = fit_file_size(merge_markets(results))
                if dropped:
                    log(f"server prices: {addon / PRICE_FILE} would exceed {PRICE_FILE_MAX // (1024 * 1024)} MB"
                        f" - left out (seen longest ago): {', '.join(dropped)}")
            if merged:
                res = write_prices(addon, merged, now)
                for g in merged:
                    info_out[g["market"]] = {"ts": int(g["ts"]), "items": len(g["items"])}
            elif has_prices(addon):
                continue                              # nothing known now: keep the last prices
            else:                                     # no prices yet: markets = {} (app detected)
                res = write_prices(addon, [], now)    # this folder's pass had no error: checked = now
        except Exception as e:  # noqa: BLE001 -- one folder must not stop the others
            log(f"server prices: cannot write {addon / PRICE_FILE}: {e}")
            result = PASS_RETRY
            continue
        if res == "written":
            n_written += 1
            log(f"server prices: wrote {addon / PRICE_FILE} ({len(merged)} markets, "
                f"{sum(len(g['items']) for g in merged)} items)")
        elif res == "unchanged":
            n_same += 1
    if result == PASS_OK:
        st["prices_last"] = now
        st["prices_etag"] = {q: e for q, e in etags.items() if q in used}
        st["prices_market"] = {q: m for q, m in pmap.items() if q in used}
        st["prices_checked"] = {q: t for q, t in checked.items() if q in used}
        st["prices_info"] = info_out
        prune_price_cache(used, now)
    else:
        st["prices_info"].update(info_out)
    save_state(st)
    if not targets:
        summary = "server prices: no Midas (1.3.0+) or Midas_Uplink addon folder found"
    else:
        summary = (f"server prices: {n_written} written, {n_same} unchanged"
                   + ("" if result == PASS_OK else ", retry later"))
    return result, summary


class Watcher:
    """Polling loop state shared by the CLI and the tray app."""

    def __init__(self, roots, sender, log, interval=30, prices_every=PRICES_EVERY):
        self.roots = roots
        self.sender = sender
        self.log = log
        self.interval = interval
        self.st = load_state()
        save_state(self.st)
        self.mtimes = {}
        self.backoff = 0
        self.last_result = None
        self.prices_every = prices_every
        self.prices_due = 0.0               # monotonic time of the next server-price check
        self.prices_ran = None              # monotonic time of the last check
        self.prices_result = None
        self.prices_summary = None
        self.prices_sig = None
        self.seen_cache = {}
        self._req = None                    # pending switch from the tray thread
        self._req_lock = threading.Lock()
        clean_price_tmp(roots)

    def prices_on(self):
        with self._req_lock:
            return self.st["prices_enabled"] if self._req is None else self._req

    def set_prices(self, on):
        """Thread-safe switch (tray menu); applied by the next step()."""
        with self._req_lock:
            self._req = bool(on)

    def _apply_request(self):
        with self._req_lock:
            req, self._req = self._req, None
            if req is None or req == self.st["prices_enabled"]:
                return
            self.st["prices_enabled"] = req
        save_state(self.st)
        self.log(f"server prices {'on' if req else 'off'}")
        if req:
            self.prices_due = 0.0

    def price_files_sig(self):
        """Cheap fingerprint of every Prices.lua folder: an addon update (placeholder
        back), a new Midas 1.3.0 or a deleted file shows up within one step."""
        out = []
        for d in price_addon_dirs(self.roots):
            try:
                s = (d / PRICE_FILE).stat()
                out.append((str(d), s.st_mtime_ns, s.st_size))
            except OSError:
                out.append((str(d), None, None))
        return tuple(out)

    def check_prices(self):
        self.prices_ran = time.monotonic()
        self.prices_due = self.prices_ran + self.prices_every
        try:
            self.prices_result, summary = run_prices(self.roots, self.sender.url, self.st,
                                                     self.log, cache=self.seen_cache)
            self.prices_summary = summary
            _log.info(summary)
        except Exception as e:  # noqa: BLE001 -- the upload loop must keep running
            _log.exception("server prices failed")
            self.prices_result = PASS_RETRY
            self.log(f"server prices failed: {e}")
        self.prices_sig = self.price_files_sig()

    def step(self, force=False):
        """Run a pass if a file changed, a retry is due or force; check server
        prices after every pass, every prices_every seconds and when a Prices.lua
        folder changed (addon installed/updated). -> seconds to wait."""
        self._apply_request()
        wait = self._upload_step(force)
        if self.st["prices_enabled"] and self.sender.outbox is None:
            now = time.monotonic()
            gap_ok = self.prices_ran is None or now - self.prices_ran >= PRICES_MIN_GAP
            after_pass = self.last_ran and gap_ok
            files_changed = (gap_ok and self.prices_sig is not None
                             and self.price_files_sig() != self.prices_sig)
            if after_pass or files_changed or now >= self.prices_due:
                self.check_prices()
        return wait

    def _upload_step(self, force):
        self.last_ran = False
        changed = False
        for p in sv_files(self.roots):
            try:
                m = p.stat().st_mtime
            except OSError:
                self.mtimes.pop(p, None)             # read again next time
                continue
            if self.mtimes.get(p) != m:
                self.mtimes[p] = m
                changed = True
        if changed or self.backoff or force:
            res = run_pass(self.roots, self.sender, self.st, self.log)
            self.last_result = res
            self.last_ran = True
            if res == PASS_OK:
                self.backoff = 0
            elif res == PASS_CONFIG:
                self.backoff = MAX_BACKOFF           # no point hammering a wrong URL
            else:
                self.backoff = min(MAX_BACKOFF, max(self.interval, self.backoff * 2 or 60))
        return self.backoff or self.interval


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"{APP_NAME} {VERSION} (uploader)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--url", default=os.environ.get("MIDAS_UPLINK_URL", DEFAULT_URL))
    ap.add_argument("--outbox")
    ap.add_argument("--wow", action="append", default=[], metavar="DIR",
                    help="additional World of Warcraft folder, searched together with the "
                         "auto-detected installs (repeatable)")
    ap.add_argument("--only-wow", action="append", default=[], metavar="DIR",
                    help="use only this World of Warcraft folder, no auto-detection "
                         "(repeatable; for manual checks)")
    ap.add_argument("--prices-now", action="store_true",
                    help="fetch server prices once (writes Midas\\Prices.lua and/or "
                         "Midas_Uplink\\Prices.lua), then exit")
    a = ap.parse_args(argv)

    def log(m):
        print(time.strftime("%H:%M:%S"), m, flush=True)

    if a.only_wow:
        roots = [Path(d) for d in a.only_wow if Path(d).is_dir()]
    else:
        roots = wow_roots(a.wow)
    if not roots:
        log("no World of Warcraft install found - pass --wow <folder>")
        return 2
    if a.prices_now:
        clean_price_tmp(roots)
        st = load_state()
        res, summary = run_prices(roots, a.url, st, log)
        log(summary)
        return 0 if res == PASS_OK else 1
    log("watching: " + "; ".join(str(r) for r in roots))
    w = Watcher(roots, Sender(a.url, a.outbox), log, a.interval)
    while True:
        wait = w.step()
        if a.once:
            return 0 if not w.backoff else 1
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
