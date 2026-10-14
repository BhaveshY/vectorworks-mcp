"""First IFC export of a document through Vectorworks' own export dialog (Windows only).

Live 2026-10-14 (VW 2024): IIFCSupport::ExportFile / IFC_ExportNoUI produce no file in a document whose IFC
export has never run; after ONE export through the dialog both work. This drives that dialog by control id
(no screen coordinates, no typing outside Vectorworks): "Export IFC Project" -> Export (id 1), the standard
Save dialog -> file name + Save (id 1). The "IFC Export Report" afterwards is closed by the bridge.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Optional

BM_CLICK = 0x00F5
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D

if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumWindowsProc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def _text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _cls(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _dialogs(pid: int) -> list:
    found = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd) and _cls(hwnd) == "#32770":
            found.append(hwnd)
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def _children(hwnd) -> list:
    kids = []

    def cb(child, _):
        kids.append(child)
        return True
    user32.EnumChildWindows(hwnd, EnumWindowsProc(cb), 0)
    return kids


def _is_file_dialog(hwnd) -> bool:
    return any(_cls(c) == "DUIViewWndClassName" for c in _children(hwnd))


def _is_export_dialog(hwnd) -> bool:
    # "Export IFC Project": owns IFC Version / Model View combos; title is localised, the controls are not.
    labels = {_text(c) for c in _children(hwnd) if _cls(c) == "Static"}
    return bool(labels & {"IFC Version:", "Model View:", "IFC-Version:"}) or "IFC" in _text(hwnd)


def _click(dialog, control_id: int) -> bool:
    button = user32.GetDlgItem(dialog, control_id)
    if not button:
        return False
    user32.PostMessageW(button, BM_CLICK, 0, 0)  # owner-drawn VW buttons react to BM_CLICK, not WM_COMMAND
    return True


def _file_name_edit(dialog):
    for c in _children(dialog):
        if _cls(c) == "Edit" and user32.GetDlgCtrlID(c) == 1001:  # the File name box (ComboBoxEx32 > ComboBox > Edit)
            return c
    edits = [c for c in _children(dialog) if _cls(c) == "Edit"]
    return edits[0] if edits else None


class DialogDriver(threading.Thread):
    """Presses Export, then fills the Save dialog with `path`. Records what it did in .log."""

    def __init__(self, pid: int, path: str, timeout_s: float = 90.0):
        super().__init__(daemon=True)
        self.pid, self.path, self.timeout_s = pid, path, timeout_s
        self.log: list[str] = []
        self.stop = threading.Event()
        self.stuck_on_story_mapping = False

    def run(self) -> None:
        pressed_export: dict = {}
        saved = False
        deadline = time.monotonic() + self.timeout_s
        while not self.stop.is_set() and time.monotonic() < deadline:
            for dlg in _dialogs(self.pid):
                if _is_file_dialog(dlg):
                    if saved:
                        continue
                    edit = _file_name_edit(dlg)
                    if edit:
                        text = ctypes.create_unicode_buffer(self.path)
                        user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.addressof(text))
                        time.sleep(0.2)
                        _click(dlg, 1)
                        saved = True
                        self.log.append("save dialog: file name set, Save pressed")
                elif _is_export_dialog(dlg) and "Report" not in _text(dlg):
                    presses = pressed_export.get(dlg, 0)
                    if presses >= 2:
                        # Export pressed twice and the dialog stays: it switched to the story/layer mapping pane.
                        self.stuck_on_story_mapping = True
                        _click(dlg, 2)  # Cancel
                        self.log.append("export dialog stayed open (no layers mapped to stories): cancelled")
                        return
                    if time.monotonic() - pressed_export.get((dlg, "t"), 0) > 2.0:
                        _click(dlg, 1)
                        pressed_export[dlg] = presses + 1
                        pressed_export[(dlg, "t")] = time.monotonic()
                        self.log.append("export dialog: Export pressed")
            time.sleep(0.1)


def export_with_dialog(host, target, path: str, ctx=None) -> dict:
    """Run vs.IFC_ExportWithUI(False) while DialogDriver answers the dialogs. Returns {"ok", "log", ...}."""
    if os.name != "nt":
        return {"ok": False, "log": ["dialog export needs Windows"]}
    pid = int(getattr(target.inst, "pid", 0) or (target.inst.status or {}).get("pid") or 0)
    if not pid:
        return {"ok": False, "log": ["Vectorworks process id unknown"]}
    driver = DialogDriver(pid, path)
    driver.start()
    error = None
    try:
        host.call(target, "run_script", {"language": "python", "code": "vs.IFC_ExportWithUI(False)\nresult = True",
                                         "undo_label": "MCP IFC export", "transaction": False},
                  kind="long", ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        error = str(exc)
    finally:
        driver.stop.set()
        driver.join(5)
    ok = os.path.exists(path) and os.path.getsize(path) > 0
    return {"ok": ok, "log": driver.log, "error": error, "needs_stories": driver.stuck_on_story_mapping}
