"""
Receive WM_QUERYENDSESSION / WM_ENDSESSION (logoff, restart, shutdown).

Console Ctrl handler alone is unreliable during Windows Update / fast reboot:
this message-only window runs a tiny message loop on a background thread so the
session manager can notify us before the network stack is torn down.

Best-effort only: still write logs/shutdown_hook.json from main.py as backup.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DESTROY = 0x0002
HWND_MESSAGE = -3


def write_shutdown_hook(reason: str) -> None:
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "shutdown_hook.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"reason": reason, "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass


def start_windows_session_listener(*, on_session_end, class_suffix: str | None = None) -> threading.Thread | None:
    """
    ``on_session_end(reason: str) -> None`` should be fast (sync Push + disk); called at most once per event.
    Returns the pump thread or None if not Windows / setup failed.
    """
    if os.name != "nt":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t if ctypes.sizeof(ctypes.c_void_p) >= 8 else ctypes.c_long

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )

    fired = {"done": False}

    def fire(reason: str) -> None:
        if fired["done"]:
            return
        fired["done"] = True
        write_shutdown_hook(reason)
        try:
            on_session_end(reason)
        except Exception:
            pass

    @WNDPROC
    def wndproc(_hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            fire("WM_QUERYENDSESSION")
            return 1  # TRUE: allow shutdown to proceed
        if msg == WM_ENDSESSION and int(wparam) != 0:
            fire("WM_ENDSESSION")
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump() -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls = f"TradePilotSess_{os.getpid()}_{class_suffix or time.time_ns()}"[:126]
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = cls
        if user32.RegisterClassW(ctypes.byref(wc)) == 0:
            return
        hwnd = user32.CreateWindowExW(
            0,
            cls,
            "TradePilot",
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            None,
            hinst,
            None,
        )
        if not hwnd:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    globals()["_TP_KEEP_WNDPROC"] = wndproc  # prevent GC of callback while thread runs
    th = threading.Thread(target=pump, name="TradePilotSessionPump", daemon=True)
    th.start()
    return th
