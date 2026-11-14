"""Portable Windows Precision Touchpad discovery and report decoding.

No vendor or product IDs are hard-coded.  A PTP is any HID collection with
usage page 0x0D (Digitizer) and usage 0x05 (Touch Pad).  Windows' own HID
parser exposes the fields as value/button capabilities; the descriptor's bit
offsets never have to be guessed.  ``HidP_GetData`` decodes one raw report into
``DataIndex -> raw value`` entries, and this module maps those entries onto
contacts (Contact ID, Tip Switch, Confidence, X, Y, Contact Count).

Descriptors differ between vendors in two ways, both handled here:

* one link collection per contact (Synaptics and many I2C pads), or
* parallel arrays for all contacts in the top-level collection.

Hybrid devices that send one contact per report (no Contact Count) are also
accepted.  Coordinates are normalised to start at zero, because some pads use
a non-zero logical minimum.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, field as dc_field

import hid
import ptp_probe as P

HIDP_INPUT = 0
HIDP_STATUS_SUCCESS = 0x00110000

PAGE_DIGITIZER = 0x0D
PAGE_DESKTOP = 0x01
PAGE_BUTTON = 0x09

USAGE_TOUCHPAD = 0x05
USAGE_CONTACT_ID = 0x51
USAGE_TIP_SWITCH = 0x42
USAGE_CONFIDENCE = 0x47
USAGE_CONTACT_COUNT = 0x54
USAGE_SCAN_TIME = 0x56
USAGE_X = 0x30
USAGE_Y = 0x31
USAGE_BUTTON_1 = 0x01

KEY_ID = (PAGE_DIGITIZER, USAGE_CONTACT_ID)
KEY_TIP = (PAGE_DIGITIZER, USAGE_TIP_SWITCH)
KEY_CONFIDENCE = (PAGE_DIGITIZER, USAGE_CONFIDENCE)
KEY_X = (PAGE_DESKTOP, USAGE_X)
KEY_Y = (PAGE_DESKTOP, USAGE_Y)
KEY_COUNT = (PAGE_DIGITIZER, USAGE_CONTACT_COUNT)
KEY_SCAN = (PAGE_DIGITIZER, USAGE_SCAN_TIME)
KEY_BUTTON_1 = (PAGE_BUTTON, USAGE_BUTTON_1)
CONTACT_KEYS = (KEY_ID, KEY_TIP, KEY_CONFIDENCE, KEY_X, KEY_Y)


class LayoutError(RuntimeError):
    """The collection exists but its input reports cannot be decoded."""


class _ButtonCapsUnion(C.Union):
    _fields_ = [('Range', P._Range), ('NotRange', P._NotRange)]


class HIDP_BUTTON_CAPS(C.Structure):
    _anonymous_ = ('u',)
    _fields_ = [
        ('UsagePage', W.USHORT),
        ('ReportID', C.c_ubyte),
        ('IsAlias', C.c_ubyte),
        ('BitField', W.USHORT),
        ('LinkCollection', W.USHORT),
        ('LinkUsage', W.USHORT),
        ('LinkUsagePage', W.USHORT),
        ('IsRange', C.c_ubyte),
        ('IsStringRange', C.c_ubyte),
        ('IsDesignatorRange', C.c_ubyte),
        ('IsAbsolute', C.c_ubyte),
        ('ReportCount', W.USHORT),
        ('Reserved2', W.USHORT),
        ('Reserved', W.ULONG * 9),
        ('u', _ButtonCapsUnion),
    ]


class HIDP_DATA(C.Structure):
    _fields_ = [
        ('DataIndex', W.USHORT),
        ('Reserved', W.USHORT),
        ('RawValue', W.ULONG),
    ]


P.hid_dll.HidP_GetButtonCaps.argtypes = [
    C.c_int, C.POINTER(HIDP_BUTTON_CAPS), C.POINTER(W.USHORT), C.c_void_p]
P.hid_dll.HidP_GetButtonCaps.restype = C.c_long
P.hid_dll.HidP_GetData.argtypes = [
    C.c_int, C.POINTER(HIDP_DATA), C.POINTER(W.ULONG), C.c_void_p,
    C.c_void_p, W.ULONG]
P.hid_dll.HidP_GetData.restype = C.c_long


@dataclass(frozen=True)
class Capability:
    """One HID value/button capability, already normalised."""
    page: int
    usage_min: int
    usage_max: int
    report_id: int
    count: int
    data_min: int
    data_max: int
    logical_min: int
    logical_max: int
    link: int
    is_range: bool

    def occurrences(self):
        """Yield ``(usage, data_index, logical_min, logical_max)``."""
        for i in range(max(1, self.count)):
            usage = self.usage_min + i if self.is_range else self.usage_min
            data_index = self.data_min + i
            if self.data_max and data_index > self.data_max:
                break
            yield usage, data_index, self.logical_min, self.logical_max


@dataclass(frozen=True)
class Field:
    page: int
    usage: int
    report_id: int
    data_index: int
    logical_min: int
    logical_max: int
    link: int

    @property
    def key(self):
        return (self.page, self.usage)


@dataclass
class ReportLayout:
    report_id: int
    contacts: list
    count_index: int | None
    scan_index: int | None
    button_index: int | None
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    @property
    def max_contacts(self):
        return len(self.contacts)

    @property
    def width(self):
        return self.x_max - self.x_min

    @property
    def height(self):
        return self.y_max - self.y_min

    def parse(self, values, raw):
        """Turn ``DataIndex -> raw value`` into the frame dict views expect."""
        if self.count_index is None:
            # Hybrid mode: one contact per report, Contact Count is absent.
            count = 1
        else:
            count = int(values.get(self.count_index, 0))
        count = max(0, min(count, len(self.contacts)))
        contacts = []
        for slot in range(count):
            indices = self.contacts[slot]
            contact_id = int(values.get(indices.get(KEY_ID), 0))
            tip = (bool(values.get(indices[KEY_TIP], 0))
                   if KEY_TIP in indices else True)
            confidence = (bool(values.get(indices[KEY_CONFIDENCE], 0))
                          if KEY_CONFIDENCE in indices else True)
            x = int(values.get(indices.get(KEY_X), self.x_min)) - self.x_min
            y = int(values.get(indices.get(KEY_Y), self.y_min)) - self.y_min
            flags = ((contact_id & 7) << 2) | (int(tip) << 1) | int(confidence)
            contacts.append(dict(slot=slot, id=contact_id, x=x, y=y,
                                 tip=tip, confidence=confidence, flags=flags))
        scan = int(values.get(self.scan_index, 0)) if self.scan_index is not None else 0
        buttons = (int(values.get(self.button_index, 0))
                   if self.button_index is not None else 0)
        return dict(schema_version=2, scan=scan, count=count,
                    buttons=buttons, contacts=contacts, hex=raw.hex())


@dataclass
class PadLayout:
    reports: dict
    input_length: int
    uses_report_ids: bool = dc_field(default=False)

    @property
    def primary(self):
        return max(self.reports.values(), key=lambda report: report.max_contacts)

    @property
    def report_ids(self):
        return tuple(sorted(self.reports))

    @property
    def max_contacts(self):
        return self.primary.max_contacts

    @property
    def x_min(self):
        return self.primary.x_min

    @property
    def x_max(self):
        return self.primary.x_max

    @property
    def y_min(self):
        return self.primary.y_min

    @property
    def y_max(self):
        return self.primary.y_max

    @property
    def width(self):
        return self.x_max - self.x_min

    @property
    def height(self):
        return self.y_max - self.y_min


@dataclass
class PadDevice:
    path: str
    layout: PadLayout
    vendor_id: int = 0
    product_id: int = 0
    product: str = ''
    manufacturer: str = ''

    @property
    def max_x(self):
        return self.layout.width

    @property
    def max_y(self):
        return self.layout.height

    @property
    def max_contacts(self):
        return self.layout.max_contacts

    def describe(self):
        return ('%s %04X:%04X %s report=%d contacts=%d range=%dx%d'
                % (self.product or 'touchpad', self.vendor_id, self.product_id,
                   self.path.rsplit('#', 1)[-1], self.layout.input_length,
                   self.max_contacts, self.max_x, self.max_y))


def _cap_from_value(c):
    if c.IsRange:
        return Capability(
            page=c.UsagePage, usage_min=c.u.Range.UsageMin,
            usage_max=c.u.Range.UsageMax, report_id=c.ReportID,
            count=c.ReportCount, data_min=c.u.Range.DataIndexMin,
            data_max=c.u.Range.DataIndexMax, logical_min=c.LogicalMin,
            logical_max=c.LogicalMax, link=c.LinkCollection, is_range=True)
    return Capability(
        page=c.UsagePage, usage_min=c.u.NotRange.Usage,
        usage_max=c.u.NotRange.Usage, report_id=c.ReportID,
        count=c.ReportCount, data_min=c.u.NotRange.DataIndex,
        data_max=c.u.NotRange.DataIndex, logical_min=c.LogicalMin,
        logical_max=c.LogicalMax, link=c.LinkCollection, is_range=False)


def _cap_from_button(c):
    if c.IsRange:
        return Capability(
            page=c.UsagePage, usage_min=c.u.Range.UsageMin,
            usage_max=c.u.Range.UsageMax, report_id=c.ReportID,
            count=c.ReportCount, data_min=c.u.Range.DataIndexMin,
            data_max=c.u.Range.DataIndexMax, logical_min=0, logical_max=1,
            link=c.LinkCollection, is_range=True)
    return Capability(
        page=c.UsagePage, usage_min=c.u.NotRange.Usage,
        usage_max=c.u.NotRange.Usage, report_id=c.ReportID,
        count=c.ReportCount, data_min=c.u.NotRange.DataIndex,
        data_max=c.u.NotRange.DataIndex, logical_min=0, logical_max=1,
        link=c.LinkCollection, is_range=False)


def _fields_from(cap):
    if not cap.page:
        return []
    out = []
    for usage, data_index, low, high in cap.occurrences():
        out.append(Field(cap.page, usage, cap.report_id, data_index,
                         int(low), int(high), cap.link))
    return out


def _contact_maps(fields):
    """Group contact fields into per-contact ``key -> DataIndex`` dicts."""
    by_key = {}
    for item in fields:
        if item.key in CONTACT_KEYS:
            by_key.setdefault(item.key, []).append(item)
    if KEY_X not in by_key or KEY_Y not in by_key:
        return []
    length = max(len(items) for items in by_key.values())
    maps = []
    for index in range(length):
        indices = {}
        for key, items in by_key.items():
            chosen = items[index] if index < len(items) else items[0]
            indices[key] = chosen.data_index
        maps.append(indices)
    return maps


def _build_report(report_id, fields):
    top = [item for item in fields if item.link == 0]
    linked = [item for item in fields if item.link != 0]
    contacts = []

    # One link collection per contact is the usual PTP layout.  A link only
    # counts as a contact when it carries both X and Y.
    links = {}
    for item in linked:
        links.setdefault(item.link, []).append(item)
    contact_links = []
    for link, items in links.items():
        keys = {item.key for item in items}
        if KEY_X in keys and KEY_Y in keys:
            contact_links.append((min(item.data_index for item in items), link,
                                  items))
    contact_links.sort()
    for _, _, items in contact_links:
        contacts.extend(_contact_maps(items))

    # Some vendors keep every contact in the top-level collection as parallel
    # arrays (X count = 5, Y count = 5, ...).  Only use those fields when no
    # per-contact link collection exists.
    if not contacts:
        contacts = _contact_maps(top)

    if not contacts:
        return None

    first_x = next((item for item in fields if item.key == KEY_X), None)
    first_y = next((item for item in fields if item.key == KEY_Y), None)
    if first_x is None or first_y is None:
        return None
    if first_x.logical_max <= first_x.logical_min:
        return None
    if first_y.logical_max <= first_y.logical_min:
        return None

    def top_index(key):
        return next((item.data_index for item in top if item.key == key), None)

    return ReportLayout(
        report_id=report_id, contacts=contacts,
        count_index=top_index(KEY_COUNT), scan_index=top_index(KEY_SCAN),
        button_index=top_index(KEY_BUTTON_1),
        x_min=first_x.logical_min, x_max=first_x.logical_max,
        y_min=first_y.logical_min, y_max=first_y.logical_max)


def build_layout(input_length, value_caps, button_caps):
    """Build a :class:`PadLayout` from normalised ``Capability`` objects."""
    if input_length <= 0:
        return None
    by_report = {}
    for cap in list(value_caps) + list(button_caps):
        by_report.setdefault(cap.report_id, []).extend(_fields_from(cap))
    reports = {}
    for report_id, fields in by_report.items():
        report = _build_report(report_id, fields)
        if report is not None:
            reports[report_id] = report
    if not reports:
        return None
    return PadLayout(reports=reports, input_length=input_length,
                     uses_report_ids=any(rid != 0 for rid in reports))


def _open_query(path):
    handle = P.kernel32.CreateFileW(
        path, 0, P.FILE_SHARE_READ | P.FILE_SHARE_WRITE, None,
        P.OPEN_EXISTING, 0, None)
    if not handle or handle == P.INVALID_HANDLE_VALUE:
        return None
    return handle


def _read_caps(handle):
    preparsed = C.c_void_p()
    if not P.hid_dll.HidD_GetPreparsedData(handle, C.byref(preparsed)):
        return None, None
    caps = P.HIDP_CAPS()
    status = P.hid_dll.HidP_GetCaps(preparsed, C.byref(caps))
    if status != HIDP_STATUS_SUCCESS:
        P.hid_dll.HidD_FreePreparsedData(preparsed)
        return None, None
    return preparsed, caps


def _read_list(preparsed, caps, button=False):
    count = (caps.NumberInputButtonCaps if button
             else caps.NumberInputValueCaps)
    if not count:
        return []
    size = C.sizeof(HIDP_BUTTON_CAPS) if button else C.sizeof(P.HIDP_VALUE_CAPS)
    array = (C.c_ubyte * (size * count))()
    length = W.USHORT(count)
    fn = P.hid_dll.HidP_GetButtonCaps if button else P.hid_dll.HidP_GetValueCaps
    pointer = (C.cast(array, C.POINTER(HIDP_BUTTON_CAPS)) if button
               else C.cast(array, C.POINTER(P.HIDP_VALUE_CAPS)))
    status = fn(HIDP_INPUT, pointer, C.byref(length), preparsed)
    if status != HIDP_STATUS_SUCCESS:
        return []
    out = []
    for index in range(length.value):
        offset = index * size
        if button:
            cap = HIDP_BUTTON_CAPS.from_buffer_copy(
                bytes(array[offset:offset + size]))
            out.append(_cap_from_button(cap))
        else:
            cap = P.HIDP_VALUE_CAPS.from_buffer_copy(
                bytes(array[offset:offset + size]))
            out.append(_cap_from_value(cap))
    return out


def load_device(entry):
    """Open one HID collection and build its layout, without reading input."""
    path = entry.get('path')
    if isinstance(path, bytes):
        path = path.decode('ascii', 'replace')
    if not path:
        raise LayoutError('设备没有路径')
    handle = _open_query(path)
    if not handle:
        raise LayoutError('无法打开设备')
    try:
        preparsed, caps = _read_caps(handle)
        if not preparsed:
            raise LayoutError('无法读取 HID caps')
        try:
            if caps.UsagePage != PAGE_DIGITIZER or caps.Usage != USAGE_TOUCHPAD:
                raise LayoutError('不是 0x0D/0x05 触控板集合')
            values = _read_list(preparsed, caps)
            buttons = _read_list(preparsed, caps, button=True)
            layout = build_layout(caps.InputReportByteLength, values, buttons)
            if layout is None:
                raise LayoutError('报告里没有可用的触点字段')
            return PadDevice(
                path=path, layout=layout,
                vendor_id=int(entry.get('vendor_id') or 0),
                product_id=int(entry.get('product_id') or 0),
                product=entry.get('product_string') or '',
                manufacturer=entry.get('manufacturer_string') or '')
        finally:
            P.hid_dll.HidD_FreePreparsedData(preparsed)
    finally:
        P.kernel32.CloseHandle(handle)


def _matches_override(device, override):
    if not override:
        return True
    wanted = override.casefold()
    tag = '%04x:%04x' % (device.vendor_id, device.product_id)
    return wanted == tag or wanted in device.path.casefold()


def discover(override=''):
    """Return ``(device, candidates, errors)``; never raises.

    ``override`` accepts a path fragment or ``vid:pid`` (hex), useful when a
    laptop exposes more than one precision touchpad collection.
    """
    try:
        entries = [entry for entry in hid.enumerate()
                   if entry.get('usage_page') == PAGE_DIGITIZER
                   and entry.get('usage') == USAGE_TOUCHPAD]
    except BaseException as exc:
        return None, [], ['HID 枚举失败：' + str(exc)]
    seen = set()
    candidates, errors = [], []
    for entry in entries:
        path = entry.get('path')
        if path in seen:
            continue
        seen.add(path)
        try:
            device = load_device(entry)
        except LayoutError as exc:
            errors.append('%s: %s' % (entry.get('product_string') or 'touchpad',
                                      exc))
            continue
        except BaseException as exc:
            errors.append('%s: %s' % (entry.get('product_string') or 'touchpad',
                                      exc))
            continue
        candidates.append(device)
    if override:
        candidates = [d for d in candidates if _matches_override(d, override)]
    return (candidates[0] if candidates else None), candidates, errors


class HidReader:
    """Owns a query handle and preparsed data; decodes raw input reports."""

    def __init__(self, device):
        self.device = device
        self.handle = _open_query(device.path)
        if not self.handle:
            raise LayoutError('无法打开触控板：' + device.path)
        self.preparsed, caps = _read_caps(self.handle)
        if not self.preparsed:
            P.kernel32.CloseHandle(self.handle)
            self.handle = None
            raise LayoutError('无法读取触控板 HID caps')
        self.caps = caps
        self.limit = max(64, int(caps.NumberInputDataIndices) + 8)

    def read(self, report):
        """Decode one raw report; ``None`` for unknown or malformed reports."""
        if not report or len(report) != self.device.layout.input_length:
            return None
        report_id = report[0] if self.device.layout.uses_report_ids else 0
        layout = self.device.layout.reports.get(report_id)
        if layout is None:
            return None
        buffer = C.create_string_buffer(bytes(report), len(report))
        data = (HIDP_DATA * self.limit)()
        length = W.ULONG(self.limit)
        status = P.hid_dll.HidP_GetData(
            HIDP_INPUT, data, C.byref(length), self.preparsed, buffer,
            len(report))
        if status != HIDP_STATUS_SUCCESS:
            return None
        values = {data[index].DataIndex: data[index].RawValue
                  for index in range(length.value)}
        return layout.parse(values, bytes(report))

    def close(self):
        if self.preparsed:
            P.hid_dll.HidD_FreePreparsedData(self.preparsed)
            self.preparsed = None
        if self.handle:
            P.kernel32.CloseHandle(self.handle)
            self.handle = None
