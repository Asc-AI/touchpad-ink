"""Probe the Windows Precision Touchpad HID collection.

Goal: find out whether user-mode code can read raw PTP reports on this laptop,
and dump the HID capabilities (report ids, usages, report lengths).

Run: python tools/ptp_probe.py
"""

import ctypes
import sys
import threading
import time
from ctypes import wintypes as w

import hid

# ---------------------------------------------------------------- Win32 setup
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid_dll = ctypes.WinDLL("hid.dll", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

FILE_DEVICE_HID = 0x000B
METHOD_BUFFERED = 0
METHOD_NEITHER = 3
FILE_ANY_ACCESS = 0

IOCTL_HID_GET_REPORT_DESCRIPTOR_A = 0x000B0003  # METHOD_NEITHER variant
IOCTL_HID_GET_REPORT_DESCRIPTOR_B = 0x000B0192  # seen in some headers

HIDP_STATUS_SUCCESS = 0x00110000
HidP_Input, HidP_Output, HidP_Feature = 0, 1, 2


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", w.USHORT),
        ("UsagePage", w.USHORT),
        ("InputReportByteLength", w.USHORT),
        ("OutputReportByteLength", w.USHORT),
        ("FeatureReportByteLength", w.USHORT),
        ("Reserved", w.USHORT * 17),
        ("NumberLinkCollectionNodes", w.USHORT),
        ("NumberInputButtonCaps", w.USHORT),
        ("NumberInputValueCaps", w.USHORT),
        ("NumberInputDataIndices", w.USHORT),
        ("NumberOutputButtonCaps", w.USHORT),
        ("NumberOutputValueCaps", w.USHORT),
        ("NumberOutputDataIndices", w.USHORT),
        ("NumberFeatureButtonCaps", w.USHORT),
        ("NumberFeatureValueCaps", w.USHORT),
        ("NumberFeatureDataIndices", w.USHORT),
    ]


class _Range(ctypes.Structure):
    _fields_ = [
        ("UsageMin", w.USHORT),
        ("UsageMax", w.USHORT),
        ("StringMin", w.USHORT),
        ("StringMax", w.USHORT),
        ("DesignatorMin", w.USHORT),
        ("DesignatorMax", w.USHORT),
        ("DataIndexMin", w.USHORT),
        ("DataIndexMax", w.USHORT),
    ]


class _NotRange(ctypes.Structure):
    _fields_ = [
        ("Usage", w.USHORT),
        ("Reserved1", w.USHORT),
        ("StringIndex", w.USHORT),
        ("Reserved2", w.USHORT),
        ("DesignatorIndex", w.USHORT),
        ("Reserved3", w.USHORT),
        ("DataIndex", w.USHORT),
        ("Reserved4", w.USHORT),
    ]


class _ValueCapsUnion(ctypes.Union):
    _fields_ = [("Range", _Range), ("NotRange", _NotRange)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("UsagePage", w.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", w.USHORT),
        ("LinkCollection", w.USHORT),
        ("LinkUsage", w.USHORT),
        ("LinkUsagePage", w.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", w.USHORT),
        ("ReportCount", w.USHORT),
        ("Reserved2", w.USHORT * 5),
        ("UnitsExp", w.ULONG),
        ("Units", w.ULONG),
        ("LogicalMin", w.LONG),
        ("LogicalMax", w.LONG),
        ("PhysicalMin", w.LONG),
        ("PhysicalMax", w.LONG),
        ("u", _ValueCapsUnion),
    ]


kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE,
]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.ReadFile.argtypes = [
    w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID,
]
kernel32.DeviceIoControl.argtypes = [
    w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
    ctypes.POINTER(w.DWORD), w.LPVOID,
]

hid_dll.HidD_GetPreparsedData.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid_dll.HidD_GetPreparsedData.restype = w.BOOLEAN
hid_dll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid_dll.HidD_FreePreparsedData.restype = w.BOOLEAN
hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid_dll.HidP_GetCaps.restype = ctypes.c_long
hid_dll.HidP_GetValueCaps.argtypes = [
    ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS),
    ctypes.POINTER(w.USHORT), ctypes.c_void_p,
]
hid_dll.HidP_GetValueCaps.restype = ctypes.c_long
hid_dll.HidD_GetFeature.argtypes = [w.HANDLE, w.LPVOID, w.ULONG]
hid_dll.HidD_GetFeature.restype = w.BOOLEAN
hid_dll.HidD_GetProductString.argtypes = [w.HANDLE, w.LPVOID, w.ULONG]
hid_dll.HidD_GetProductString.restype = w.BOOLEAN


def open_path(path, access, label):
    err_before = ctypes.get_last_error()
    h = kernel32.CreateFileW(
        path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    err = ctypes.get_last_error()
    ok = h not in (None, INVALID_HANDLE_VALUE)
    print(f"[open:{label}] access=0x{access:X} -> {'OK' if ok else 'FAIL'}"
          f" err={err}" + (f" (prev {err_before})" if err_before else ""))
    return h if ok else None


def get_caps(h):
    preparsed = ctypes.c_void_p()
    if not hid_dll.HidD_GetPreparsedData(h, ctypes.byref(preparsed)):
        print("  HidD_GetPreparsedData failed err=", ctypes.get_last_error())
        return None, None
    caps = HIDP_CAPS()
    status = hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps))
    if status != HIDP_STATUS_SUCCESS:
        print(f"  HidP_GetCaps failed status=0x{status & 0xFFFFFFFF:08X}")
        hid_dll.HidD_FreePreparsedData(preparsed)
        return None, None
    return preparsed, caps


def dump_value_caps(preparsed, caps, report_type, name, count):
    if count == 0:
        print(f"  {name}: none")
        return []
    arr = (HIDP_VALUE_CAPS * count)()
    length = w.USHORT(count)
    status = hid_dll.HidP_GetValueCaps(
        report_type, arr, ctypes.byref(length), preparsed
    )
    if status != HIDP_STATUS_SUCCESS:
        print(f"  {name}: HidP_GetValueCaps failed 0x{status & 0xFFFFFFFF:08X}")
        return []
    print(f"  {name}: {length.value} value caps")
    out = []
    for i in range(length.value):
        c = arr[i]
        if c.IsRange:
            usage = f"0x{c.u.Range.UsageMin:04X}-0x{c.u.Range.UsageMax:04X}"
        else:
            usage = f"0x{c.u.NotRange.Usage:04X}"
        print(
            f"    page=0x{c.UsagePage:04X} usage={usage} reportID={c.ReportID}"
            f" linkColl={c.LinkCollection} linkUsage=0x{c.LinkUsagePage:04X}/"
            f"0x{c.LinkUsage:04X} bits={c.BitSize} count={c.ReportCount}"
            f" logical={c.LogicalMin}..{c.LogicalMax}"
            f" abs={int(bool(c.IsAbsolute))} null={int(bool(c.HasNull))}"
        )
        out.append(c)
    return out


def try_descriptor(h, ioctl, label):
    buf = ctypes.create_string_buffer(4096)
    returned = w.DWORD(0)
    ok = kernel32.DeviceIoControl(
        h, ioctl, None, 0, buf, len(buf), ctypes.byref(returned), None
    )
    err = ctypes.get_last_error()
    print(f"[descriptor:{label}] ok={bool(ok)} err={err} bytes={returned.value}")
    if ok and returned.value:
        data = bytes(buf.raw[:returned.value])
        print("  " + data.hex())
        return data
    return None


def parse_descriptor_usages(data):
    """Tiny HID descriptor walker, only prints report id boundaries and usages.

    Not a full parser; good enough to see the PTP field layout.
    """
    if not data:
        return
    i = 0
    report_id = 0
    usages = []
    while i < len(data):
        item = data[i]
        if item == 0xFE:  # long item
            size = data[i + 1]
            i += 3 + size
            continue
        size = {0: 0, 1: 1, 2: 2, 3: 4}[item & 0x03]
        tag = item & 0xFC
        value = int.from_bytes(data[i + 1:i + 1 + size], "little") if size else 0
        if tag == 0x84:  # report id
            report_id = value
            print(f"  report id {report_id} starts at byte {i}")
        elif tag == 0x04:  # usage
            page = None
            usages.append((report_id, value))
        elif tag == 0x08:  # usage page
            page = value
        elif tag == 0x80:  # input
            if usages:
                print(f"    input report {report_id}: usages {usages}")
                usages = []
        i += 1 + size


def read_thread(h, length, seconds, out):
    buf = ctypes.create_string_buffer(length)
    got = w.DWORD(0)
    t0 = time.time()
    while time.time() - t0 < seconds:
        ok = kernel32.ReadFile(h, buf, length, ctypes.byref(got), None)
        err = ctypes.get_last_error()
        if not ok:
            out.append(("ERR", err, 0, b""))
            break
        out.append(("OK", 0, got.value, bytes(buf.raw[:got.value])))
        if len(out) > 50:
            break


def main():
    devs = [d for d in hid.enumerate() if d.get("usage_page") == 0x0D
            and d.get("usage") == 0x05]
    if not devs:
        print("No Precision Touchpad (0x0D/0x05) HID collection found.")
        return 1
    d = devs[0]
    path = d["path"].decode("ascii", "replace")
    print("PTP collection:")
    print("  path:", path)
    print("  mfg:", d.get("manufacturer_string"), "product:", d.get("product_string"))
    print("  vid/pid: 0x%04X/0x%04X" % (d.get("vendor_id", 0), d.get("product_id", 0)))

    # 1) query-only open
    h0 = open_path(path, 0, "query")
    if h0:
        preparsed, caps = get_caps(h0)
        if caps:
            print("  caps: usage=0x%04X/0x%04X inputLen=%d outputLen=%d featureLen=%d"
                  % (caps.UsagePage, caps.Usage, caps.InputReportByteLength,
                     caps.OutputReportByteLength, caps.FeatureReportByteLength))
            print("  counts: input values=%d feature values=%d link collections=%d"
                  % (caps.NumberInputValueCaps, caps.NumberFeatureValueCaps,
                     caps.NumberLinkCollectionNodes))
            input_caps = dump_value_caps(preparsed, caps, HidP_Input, "INPUT", caps.NumberInputValueCaps)
            feature_caps = dump_value_caps(preparsed, caps, HidP_Feature, "FEATURE", caps.NumberFeatureValueCaps)

            # Feature report values
            lengths = {}
            for c in feature_caps:
                rid = c.ReportID
                lengths.setdefault(rid, caps.FeatureReportByteLength)
                buf = ctypes.create_string_buffer(max(caps.FeatureReportByteLength, 2))
                buf[0] = bytes([rid])
                ok = hid_dll.HidD_GetFeature(h0, buf, len(buf))
                err = ctypes.get_last_error()
                print(f"  get_feature(rid={rid}) ok={bool(ok)} err={err} data={bytes(buf.raw).hex()}")
                break  # first one is enough for now
            print("  input usages:", [
                (hex(c.UsagePage), hex(c.u.NotRange.Usage if not c.IsRange else c.u.Range.UsageMin),
                 c.ReportID, c.BitSize, c.ReportCount) for c in input_caps
            ])
        try_descriptor(h0, IOCTL_HID_GET_REPORT_DESCRIPTOR_A, "A")
        try_descriptor(h0, IOCTL_HID_GET_REPORT_DESCRIPTOR_B, "B")
        if preparsed:
            hid_dll.HidD_FreePreparsedData(preparsed)
        kernel32.CloseHandle(h0)

    # 2) read access
    for access, label in [(GENERIC_READ, "read"), (GENERIC_READ | GENERIC_WRITE, "rw")]:
        h = open_path(path, access, label)
        if not h:
            continue
        preparsed, caps = get_caps(h)
        length = caps.InputReportByteLength if caps else 64
        print(f"  reading {length} bytes for 2s (touch the pad now)...")
        out = []
        th = threading.Thread(target=read_thread, args=(h, length, 2.0, out), daemon=True)
        th.start()
        th.join(3.0)
        alive = th.is_alive()
        print(f"  read thread alive={alive} results={len(out)}")
        for kind, err, n, data in out[:10]:
            if kind == "ERR":
                print(f"    ReadFile FAILED err={err}")
            else:
                print(f"    {n:2d} bytes: {data.hex()}")
        if preparsed:
            hid_dll.HidD_FreePreparsedData(preparsed)
        kernel32.CloseHandle(h)
        break

    return 0


if __name__ == "__main__":
    sys.exit(main())
