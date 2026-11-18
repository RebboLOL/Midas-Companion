# -*- coding: utf-8 -*-
"""
Package helpers for the MSIX build (ctypes only, no extra packages).

- packaged()               True when the process runs with package identity
- package_family_name()    e.g. "Rebo.MidasUplink_abc123xyz"
- packaged_home()          %LOCALAPPDATA%\\Packages\\<PFN>\\LocalState\\MidasUplink
                           (real, not virtualized: Explorer/Notepad can open it)
- StartupTask              windows.startupTask of the manifest (TaskId below)
"""
import ctypes
import os
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

TASK_ID = "MidasUplinkStartup"
APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_INSUFFICIENT_BUFFER = 122

# StartupTaskState
DISABLED, DISABLED_BY_USER, ENABLED, DISABLED_BY_POLICY, ENABLED_BY_POLICY = 0, 1, 2, 3, 4

IID_ISTARTUPTASKSTATICS = "ee5b60bd-a148-41a7-b26e-e8b88a1e62f8"
IID_IASYNCINFO = "00000036-0000-0000-c000-000000000046"


def _pkg_string(fn_name):
    try:
        fn = getattr(ctypes.WinDLL("kernel32"), fn_name)
    except (AttributeError, OSError):
        return None
    n = wintypes.UINT(0)
    rc = fn(ctypes.byref(n), None)
    if rc == APPMODEL_ERROR_NO_PACKAGE or rc not in (0, ERROR_INSUFFICIENT_BUFFER):
        return None
    buf = ctypes.create_unicode_buffer(n.value)
    if fn(ctypes.byref(n), buf) != 0:
        return None
    return buf.value


def package_family_name():
    return _pkg_string("GetCurrentPackageFamilyName")


def packaged():
    return package_family_name() is not None


def packaged_home():
    pfn = package_family_name()
    if not pfn:
        return None
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Packages" / pfn / "LocalState" / "MidasUplink"


# --------------------------------------------------------------------------- #
# WinRT StartupTask via raw vtables
# --------------------------------------------------------------------------- #
class _GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


def _guid(s):
    g = _GUID()
    ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16)
    return g


HRESULT = ctypes.c_long


def _vcall(obj, index, restype, *argtypes_and_args):
    """Call vtable slot `index` of COM object `obj` (c_void_p)."""
    argtypes = argtypes_and_args[0::2]
    args = argtypes_and_args[1::2]
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(obj, *args)


def _release(obj):
    if obj and obj.value:
        _vcall(obj, 2, ctypes.c_ulong)


def _check(hr, what):
    if hr < 0:
        raise OSError(f"{what} failed: 0x{hr & 0xffffffff:08x}")


class _HString:
    def __init__(self, text):
        self.cb = ctypes.WinDLL("combase")
        self.h = ctypes.c_void_p()
        _check(self.cb.WindowsCreateString(ctypes.c_wchar_p(text), len(text), ctypes.byref(self.h)),
               "WindowsCreateString")

    def close(self):
        self.cb.WindowsDeleteString(self.h)


def _await(op, timeout=15.0):
    """Wait for an IAsyncOperation<T> and return its result pointer."""
    info = ctypes.c_void_p()
    iid = _guid(IID_IASYNCINFO)
    _check(_vcall(op, 0, HRESULT, ctypes.POINTER(_GUID), ctypes.byref(iid),
                  ctypes.POINTER(ctypes.c_void_p), ctypes.byref(info)), "QueryInterface(IAsyncInfo)")
    try:
        end = time.monotonic() + timeout
        while True:
            st = ctypes.c_int(0)
            _check(_vcall(info, 7, HRESULT, ctypes.POINTER(ctypes.c_int), ctypes.byref(st)), "get_Status")
            if st.value == 1:                    # Completed
                break
            if st.value in (2, 3):               # Canceled / Error
                code = HRESULT(0)
                _vcall(info, 8, HRESULT, ctypes.POINTER(HRESULT), ctypes.byref(code))
                raise OSError(f"async operation failed: 0x{code.value & 0xffffffff:08x}")
            if time.monotonic() > end:
                raise TimeoutError("async operation timed out")
            time.sleep(0.02)
    finally:
        _release(info)
    res = ctypes.c_void_p()
    _check(_vcall(op, 8, HRESULT, ctypes.POINTER(ctypes.c_void_p), ctypes.byref(res)), "GetResults")
    return res


def _with_task(fn):
    """Run fn(task_ptr) on a fresh MTA thread (independent of the tray's apartment)."""
    out = {}

    def run():
        cb = ctypes.WinDLL("combase")
        cb.RoInitialize(1)                       # RO_INIT_MULTITHREADED
        statics = ctypes.c_void_p()
        task = ctypes.c_void_p()
        op = ctypes.c_void_p()
        cls = _HString("Windows.ApplicationModel.StartupTask")
        tid = _HString(TASK_ID)
        try:
            iid = _guid(IID_ISTARTUPTASKSTATICS)
            _check(cb.RoGetActivationFactory(cls.h, ctypes.byref(iid), ctypes.byref(statics)),
                   "RoGetActivationFactory")
            # IStartupTaskStatics: 6 GetForCurrentPackageAsync, 7 GetAsync(taskId)
            _check(_vcall(statics, 7, HRESULT, ctypes.c_void_p, tid.h,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.byref(op)), "GetAsync")
            task = _await(op)
            out["value"] = fn(task)
        except Exception as e:  # noqa: BLE001 -- handed to the caller
            out["error"] = e
        finally:
            _release(op)
            _release(task)
            _release(statics)
            cls.close()
            tid.close()
            cb.RoUninitialize()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(30)
    if "error" in out:
        raise out["error"]
    return out.get("value")


def _state(task):
    # IStartupTask: 6 RequestEnableAsync, 7 Disable, 8 get_State, 9 get_TaskId
    st = ctypes.c_int(0)
    _check(_vcall(task, 8, HRESULT, ctypes.POINTER(ctypes.c_int), ctypes.byref(st)), "get_State")
    return st.value


def startup_state():
    return _with_task(_state)


def startup_enabled():
    return startup_state() in (ENABLED, ENABLED_BY_POLICY)


def set_startup(on):
    """-> new state. DISABLED_BY_USER means Windows settings block it (user must
    re-enable it under Settings > Apps > Startup)."""
    def run(task):
        if on:
            op = ctypes.c_void_p()
            _check(_vcall(task, 6, HRESULT, ctypes.POINTER(ctypes.c_void_p), ctypes.byref(op)),
                   "RequestEnableAsync")
            try:
                r = _await(op)                   # result is an enum, returned in the pointer slot
                return int(r.value or 0)
            finally:
                _release(op)
        _check(_vcall(task, 7, HRESULT), "Disable")
        return _state(task)
    return _with_task(run)
