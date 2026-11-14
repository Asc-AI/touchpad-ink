"""Capture Precision Touchpad HID reports through the Raw Input API.

Raw Input taps the HID stream without opening the (exclusively used) PTP
collection, so this is the supported user-mode way to see contacts while the
normal touchpad driver keeps working.

Run: python -u tools/raw_probe.py --seconds 20
Then draw on the touchpad while it listens.
"""

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes as w

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptp_probe as P  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_INPUT = 0x00FF
PM_REMOVE = 0x0001
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEHID = 2
HIDP_STATUS_SUCCESS = 0x00110000

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
LRESULT = ctypes.c_ssize_t


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", w.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", w.USHORT),
        ("usUsage", w.USHORT),
        ("dwFlags", w.DWORD),
        ("hwndTarget", w.HWND),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", w.HWND), ("message", w.UINT),
        ("wParam", w.WPARAM), ("lParam", w.LPARAM),
        ("time", w.DWORD), ("pt", w.POINT),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = w.WORD
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.CreateWindowExW.restype = w.HWND
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT,
]
user32.RegisterRawInputDevices.restype = w.BOOL
user32.GetRawInputData.argtypes = [
    w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT,
]
user32.GetRawInputData.restype = w.UINT
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(MSG), w.HWND, w.UINT, w.UINT, w.UINT,
]
user32.PeekMessageW.restype = w.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT

P.hid_dll.HidP_GetUsageValue.argtypes = [
    ctypes.c_int, w.USHORT, w.USHORT, w.USHORT, ctypes.POINTER(w.ULONG),
    ctypes.c_void_p, ctypes.c_char_p, w.ULONG,
]
P.hid_dll.HidP_GetUsageValue.restype = ctypes.c_long


def u32(buf, off):
    return int.from_bytes(buf[off:off + 4], "little")


def decode_report(report, preparsed):
    """Decode one PTP input report using the HID preparsed data."""
    buf = ctypes.create_string_buffer(report, len(report))

    def value(page, link, usage):
        out = w.ULONG(0)
        st = P.hid_dll.HidP_GetUsageValue(
            P.HidP_Input, page, link, usage, ctypes.byref(out),
            preparsed, buf, len(report),
        )
        if st != HIDP_STATUS_SUCCESS:
            return None
        return out.value

    contacts = []
    for link in range(1, 6):
        cid = value(0x0D, link, 0x51)
        x = value(0x01, link, 0x30)
        y = value(0x01, link, 0x31)
        if cid is not None or x is not None or y is not None:
            contacts.append({"link": link, "id": cid, "x": x, "y": y})
    return {
        "report_id": report[0],
        "count": value(0x0D, 0, 0x54),
        "scan": value(0x0D, 0, 0x56),
        "contacts": contacts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    # Preparsed data from a query-only open of the PTP collection.
    devs = [d for d in __import__("hid").enumerate()
            if d.get("usage_page") == 0x0D and d.get("usage") == 0x05]
    if not devs:
        print("PTP collection not found")
        return 1
    h = P.open_path(devs[0]["path"].decode("ascii", "replace"), 0, "query")
    preparsed, caps = P.get_caps(h) if h else (None, None)
    if not caps:
        print("could not get PTP caps")
        return 1
    print("PTP caps: inputLen=%d reportID(s) from caps, max contacts feature=5"
          % caps.InputReportByteLength)

    reports = []

    def on_input(lparam):
        size = w.UINT(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), 24)
        if size.value == 0:
            return
        raw = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(
            lparam, RID_INPUT, raw, ctypes.byref(size), 24
        )
        if got == 0xFFFFFFFF or got < 32:
            return
        data = raw.raw[:got]
        if u32(data, 0) != RIM_TYPEHID:
            return
        size_hid = u32(data, 24)
        count = u32(data, 28)
        payload = data[32:32 + size_hid * count]
        for i in range(count):
            report = payload[i * size_hid:(i + 1) * size_hid]
            rec = decode_report(report, preparsed)
            rec["t"] = time.time()
            rec["hex"] = bytes(report).hex()
            reports.append(rec)
            cs = " ".join(
                f"L{c['link']}:id={c['id']},x={c['x']},y={c['y']}"
                for c in rec["contacts"]
            )
            print(f"{rec['t']:.3f} rid={rec['report_id']} count={rec['count']} "
                  f"scan={rec['scan']} | {cs}")
            print(f"    {rec['hex']}")

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                on_input(lparam)
            except Exception as e:  # keep the window alive
                print("decode error:", type(e).__name__, e)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc_cb = WNDPROC(wndproc)
    P.kernel32.GetModuleHandleW.restype = w.HINSTANCE
    hinst = P.kernel32.GetModuleHandleW(None)
    cls = "QoderPtpRawProbe"
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = wndproc_cb
    wc.hInstance = hinst
    wc.lpszClassName = cls
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        print("RegisterClassExW failed:", ctypes.get_last_error())
        return 1
    hwnd = user32.CreateWindowExW(0, cls, "ptp-raw", 0, 0, 0, 1, 1,
                                  None, None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed:", ctypes.get_last_error())
        return 1

    rid = RAWINPUTDEVICE(0x0D, 0x05, RIDEV_INPUTSINK, hwnd)
    if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
        print("RegisterRawInputDevices failed:", ctypes.get_last_error())
        return 1
    print("raw input registered; draw on the touchpad for %.0f s..." % args.seconds)

    t_end = time.time() + args.seconds
    msg = MSG()
    while time.time() < t_end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.002)

    print("total PTP reports:", len(reports))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in reports:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("saved:", args.out)
    if preparsed:
        P.hid_dll.HidD_FreePreparsedData(preparsed)
    if h:
        P.kernel32.CloseHandle(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
