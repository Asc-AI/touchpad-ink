"""Manual probe: check the live tray's auto hand-off to the typing view.

Creates a throw-away WinForms text box, forces it to the foreground, pulses
Alt and then prints the auto log lines the running tray wrote. Only
``keybd_event`` is used for Alt; the mouse is never moved and no text is
typed. Run the tray with ``touchpad_tray.bat`` first. Manual only.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'data', 'auto.log')
U = C.WinDLL('user32', use_last_error=True)
EnumProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
U.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
U.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
U.GetWindowThreadProcessId.restype = W.DWORD
U.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
U.EnumWindows.argtypes = [EnumProc, W.LPARAM]
U.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]


def title(hwnd):
    text = C.create_unicode_buffer(256)
    U.GetWindowTextW(hwnd, text, 256)
    return text.value


def find_window(name):
    hits = []

    def visit(hwnd, _param):
        if title(hwnd) == name:
            hits.append(hwnd)
        return True

    U.EnumWindows(EnumProc(visit), 0)
    return hits[0] if hits else 0


def force_foreground(hwnd):
    foreground = U.GetForegroundWindow()
    source = U.GetWindowThreadProcessId(foreground, None)
    target = U.GetWindowThreadProcessId(hwnd, None)
    U.AttachThreadInput(source, target, True)
    U.SetForegroundWindow(hwnd)
    U.SetFocus(hwnd)
    U.AttachThreadInput(source, target, False)


def alt(up):
    U.keybd_event(0x12, 0, 0x0002 if up else 0, 0)


def main():
    before = 0
    if os.path.exists(LOG):
        with open(LOG, 'r', encoding='utf-8') as handle:
            before = len(handle.readlines())
    target = subprocess.Popen(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         os.path.join(ROOT, 'data', 'probe_auto_target.ps1')])
    result = 1
    try:
        hwnd = 0
        for _ in range(60):
            hwnd = find_window('TouchpadAutoProbe')
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            print('probe: the WinForms window never appeared')
            return 1
        time.sleep(0.4)
        force_foreground(hwnd)
        time.sleep(0.4)
        print('probe: foreground is now', title(U.GetForegroundWindow()))
        alt(False)
        time.sleep(0.4)
        alt(True)
        time.sleep(0.6)
        U.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    finally:
        target.terminate()
        try:
            target.wait(timeout=5)
        except subprocess.TimeoutExpired:
            target.kill()
    if os.path.exists(LOG):
        with open(LOG, 'r', encoding='utf-8') as handle:
            lines = handle.readlines()[before:]
        for line in lines:
            print('log:', line.rstrip())
        result = 0 if any('handed the press to typing' in line
                          for line in lines) else 1
    print('probe: ok=%s' % (result == 0))
    return result


if __name__ == '__main__':
    sys.exit(main())
