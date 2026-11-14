"""Record raw Precision Touchpad contact data through the Raw Input API.

Produces:
  data/session_*.jsonl   one line per HID frame (contact count, scan time, contacts)
  data/session_*.strokes.csv  one line per sampled contact point, grouped by stroke
  data/session_*.meta.json    capture metadata / HID caps

Usage:
  python -u tools/ptp_capture.py --seconds 30 --delay 8
  python -u tools/ptp_capture.py --seconds 20 --out data/test.jsonl
"""

import argparse
import csv
import ctypes
import datetime as dt
import json
import os
import sys
import time
from ctypes import wintypes as w

import hid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ptp_probe as P  # noqa: E402
import raw_probe as R  # noqa: E402

REPORT_ID = 3
MAX_CONTACTS = 5
CONTACT_BYTES = 5


def u16(buf, off):
    return buf[off] | (buf[off + 1] << 8)


def parse_report(report):
    if len(report) < 1 + MAX_CONTACTS * CONTACT_BYTES + 4:
        return None
    if report[0] != REPORT_ID:
        return None
    contacts = []
    for i in range(MAX_CONTACTS):
        base = 1 + i * CONTACT_BYTES
        flags = report[base]
        if flags == 0:
            continue
        contacts.append({
            "id": (flags >> 2) & 0x07,
            "x": u16(report, base + 1),
            "y": u16(report, base + 3),
            "tip": bool(flags & 0x01),
            "inrange": bool(flags & 0x02),
        })
    base_scan = 1 + MAX_CONTACTS * CONTACT_BYTES
    return {
        "scan": u16(report, base_scan),
        "count": report[base_scan + 2],
        "buttons": report[base_scan + 3],
        "contacts": contacts,
    }


class Recorder:
    def __init__(self, out_jsonl, keep_hex=False):
        self.out_jsonl = out_jsonl
        self.keep_hex = keep_hex
        self.start = None
        self.frames = 0
        self.reports = 0
        self.strokes = []
        self.active = {}
        self.f = open(out_jsonl, "w", encoding="utf-8")
        self.last_flush = time.time()
        self.bad_reports = 0
        self.first_scan = None
        self.last_scan = None
        self.scan_unwrapped = 0

    def begin(self):
        self.start = time.perf_counter()

    def _new_stroke(self, contact_id):
        s = {"id": len(self.strokes), "contact_id": contact_id, "points": []}
        self.strokes.append(s)
        self.active[contact_id] = s
        return s

    def _close_missing(self, present):
        for cid in list(self.active):
            if cid not in present:
                del self.active[cid]

    def on_report(self, report):
        rec = parse_report(report)
        if rec is None:
            self.bad_reports += 1
            return
        self.reports += 1
        now = time.perf_counter()
        t = now - self.start
        rec["t"] = round(t, 6)
        if self.keep_hex:
            rec["hex"] = bytes(report).hex()
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.frames += 1

        scan = rec["scan"]
        if self.first_scan is None:
            self.first_scan = scan
        elif self.last_scan is not None:
            self.scan_unwrapped += (scan - self.last_scan) & 0xFFFF
        self.last_scan = scan

        present = set()
        for c in rec["contacts"]:
            cid = c["id"]
            if not (c["tip"] and c["inrange"]):
                continue
            present.add(cid)
            stroke = self.active.get(cid)
            if stroke is None:
                stroke = self._new_stroke(cid)
            stroke["points"].append((rec["t"], c["x"], c["y"], scan))
        self._close_missing(present)

        if now - self.last_flush > 0.5:
            self.f.flush()
            self.last_flush = now

    def finish(self):
        self.f.flush()
        self.f.close()

    def write_strokes(self, path):
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["stroke_id", "contact_id", "t", "x", "y", "scan"])
            for s in self.strokes:
                for (t, x, y, scan) in s["points"]:
                    wr.writerow([s["id"], s["contact_id"], f"{t:.6f}", x, y, scan])
                    n += 1
        return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-hex", action="store_true")
    ap.add_argument("--no-beep", action="store_true")
    args = ap.parse_args()

    if args.out:
        out = args.out
    else:
        os.makedirs(os.path.join(HERE, "..", "data"), exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(HERE, "..", "data", f"session_{stamp}.jsonl")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    base, _ = os.path.splitext(out)
    strokes_path = base + ".strokes.csv"
    meta_path = base + ".meta.json"

    devs = [d for d in hid.enumerate()
            if d.get("usage_page") == 0x0D and d.get("usage") == 0x05]
    if not devs:
        print("PTP collection not found")
        return 1
    dev = devs[0]
    path = dev["path"].decode("ascii", "replace")
    h = P.open_path(path, 0, "query")
    preparsed, caps = P.get_caps(h) if h else (None, None)
    if not caps:
        print("could not read PTP caps")
        return 1

    if args.delay > 0:
        print(f"starting in {args.delay:.0f}s ...")
        time.sleep(args.delay)

    recorder = Recorder(out, keep_hex=args.keep_hex)

    def on_input(lparam):
        size = w.UINT(0)
        R.user32.GetRawInputData(lparam, R.RID_INPUT, None, ctypes.byref(size), 24)
        if size.value == 0:
            return
        raw = ctypes.create_string_buffer(size.value)
        got = R.user32.GetRawInputData(
            lparam, R.RID_INPUT, raw, ctypes.byref(size), 24
        )
        if got == 0xFFFFFFFF or got < 32:
            return
        data = raw.raw[:got]
        if R.u32(data, 0) != R.RIM_TYPEHID:
            return
        size_hid = R.u32(data, 24)
        count = R.u32(data, 28)
        payload = data[32:32 + size_hid * count]
        for i in range(count):
            recorder.on_report(payload[i * size_hid:(i + 1) * size_hid])

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == R.WM_INPUT:
            try:
                on_input(lparam)
            except Exception as e:
                print("input error:", type(e).__name__, e, flush=True)
            return 0
        return R.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc_cb = R.WNDPROC(wndproc)
    P.kernel32.GetModuleHandleW.restype = w.HINSTANCE
    hinst = P.kernel32.GetModuleHandleW(None)
    cls = "QoderPtpCapture"
    wc = R.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(R.WNDCLASSEXW)
    wc.lpfnWndProc = wndproc_cb
    wc.hInstance = hinst
    wc.lpszClassName = cls
    if not R.user32.RegisterClassExW(ctypes.byref(wc)):
        print("RegisterClassExW failed:", ctypes.get_last_error())
        return 1
    hwnd = R.user32.CreateWindowExW(0, cls, "ptp-capture", 0, 0, 0, 1, 1,
                                    None, None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed:", ctypes.get_last_error())
        return 1

    rid = R.RAWINPUTDEVICE(0x0D, 0x05, R.RIDEV_INPUTSINK, hwnd)
    if not R.user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
        print("RegisterRawInputDevices failed:", ctypes.get_last_error())
        return 1

    recorder.begin()
    print("CAPTURING %.0fs - draw on the touchpad now" % args.seconds, flush=True)
    if not args.no_beep:
        try:
            import winsound
            winsound.Beep(880, 150)
        except Exception:
            pass

    t_end = time.time() + args.seconds
    msg = R.MSG()
    while time.time() < t_end:
        while R.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, R.PM_REMOVE):
            R.user32.TranslateMessage(ctypes.byref(msg))
            R.user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.002)

    if not args.no_beep:
        try:
            import winsound
            winsound.Beep(660, 120)
            winsound.Beep(440, 180)
        except Exception:
            pass

    recorder.finish()
    n_points = recorder.write_strokes(strokes_path)
    duration = (time.perf_counter() - recorder.start) if recorder.start else 0
    scan_span = None
    if recorder.first_scan is not None:
        scan_span = recorder.scan_unwrapped

    meta = {
        "device_path": path,
        "vendor_id": dev.get("vendor_id"),
        "product_id": dev.get("product_id"),
        "product": dev.get("product_string"),
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "seconds_requested": args.seconds,
        "duration_s": round(duration, 3),
        "reports": recorder.reports,
        "frames": recorder.frames,
        "bad_reports": recorder.bad_reports,
        "strokes": len(recorder.strokes),
        "points": n_points,
        "input_report_bytes": caps.InputReportByteLength,
        "x_max": 1332,
        "y_max": 828,
        "scan_delta": scan_span,
        "approx_hz": round(recorder.frames / duration, 1) if duration else None,
        "data_jsonl": out,
        "strokes_csv": strokes_path,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print("saved:", out)
    if preparsed:
        P.hid_dll.HidD_FreePreparsedData(preparsed)
    if h:
        P.kernel32.CloseHandle(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
