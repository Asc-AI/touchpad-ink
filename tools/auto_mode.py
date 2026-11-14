"""Decide which handwriting view the foreground app wants.

Auto mode checks this once per Alt press (never continuously): a OneNote
foreground gets the note view, a running slide show gets PPT ink, a visible
text caret gets the caret overlay, anything else is left alone. Pure
``classify`` and ``AutoSelector`` keep the decision testable without touching
a real window, Alt key or the mouse.

Typing detection has three layers because modern editors hide the caret:

* Win32 ``GetGUIThreadInfo`` caret (classic edit controls),
* Win32 focus class containing ``edit`` / ``scintilla``,
* UI Automation focus: control type ``Edit`` (browser inputs, textareas,
  Chromium/Electron composers) or ``Document`` (Word/WPS/Qoder editors).
  ``Document`` is ignored for known browsers so merely holding Alt on a web
  page does not open the overlay.

The same UI Automation element exposes the caret rectangle through its text
pattern, which is how the typing overlay finds the caret in Chromium/Electron
editors that have no Win32 caret at all.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import os
import time

import app_paths
import raw_probe as R

U = R.user32
kernel32 = C.WinDLL('kernel32', use_last_error=True)
ole32 = C.OleDLL('ole32')

GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

ONENOTE_PROCESS = 'onenote'
SLIDE_SHOW_PROCESSES = ('powerpnt.exe', 'wpp.exe', 'pptview.exe')
SHOW_CLASSES = ('screenclass',)
EDIT_TOKENS = ('edit', 'scintilla')
# Holding Alt on ordinary web content should not open the handwriting overlay;
# editable fields there still classify as UIA ``Edit``.
BROWSER_PROCESSES = ('chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe',
                     'opera.exe', 'vivaldi.exe', 'chromium.exe', 'iexplore.exe',
                     '360se.exe', '360chrome.exe', 'qqbrowser.exe',
                     'sogouexplorer.exe')

# QQ's Electron composer exposes no Win32 caret and reports its focused
# element as Group/Pane, so UI Automation cannot see the message box. The
# process plus the Chromium window class is the only reliable signal; the
# overlay is harmless anywhere else in the QQ window.
CHAT_TYPING_PROCESSES = ('qq.exe', 'qqnt.exe')
CHAT_TYPING_CLASSES = ('chrome_widgetwin_1',)

# WeChat 4.x (weixin.exe) draws its whole UI in one Qt window. The message
# box has no Win32 caret and exposes no UIA Edit, yet Unicode SendInput
# reaches it, so process plus the Qt window class is the only cue. The
# class carries the Qt version (Qt5.15.14 -> Qt51514QWindowIcon).
WECHAT_PROCESS = 'weixin.exe'
QT_WINDOW_SUFFIX = 'qwindowicon'

UIA_EDIT = 50004
UIA_DOCUMENT = 50030
UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_CURRENT_PROCESS_ID = 30002
UIA_TEXT_PATTERN = 10014
TEXT_UNIT_CHARACTER = 0
EDITABLE_TYPES = (UIA_EDIT, UIA_DOCUMENT)
UIA_LABELS = {
    50000: 'Button', 50001: 'Calendar', 50002: 'CheckBox', 50003: 'ComboBox',
    UIA_EDIT: 'Edit', 50005: 'Hyperlink', 50006: 'Image', 50007: 'ListItem',
    50008: 'List', 50009: 'Menu', 50010: 'MenuBar', 50011: 'MenuItem',
    50012: 'ProgressBar', 50013: 'RadioButton', 50014: 'ScrollBar',
    50015: 'Slider', 50016: 'Spinner', 50017: 'StatusBar', 50018: 'Tab',
    50019: 'TabItem', 50020: 'Text', 50021: 'ToolBar', 50022: 'ToolTip',
    50023: 'Tree', 50024: 'TreeItem', 50025: 'Custom', 50026: 'Group',
    50027: 'Thumb', 50028: 'DataGrid', 50029: 'DataItem',
    UIA_DOCUMENT: 'Document', 50031: 'SplitButton', 50032: 'Window',
    50033: 'Pane', 50034: 'Header', 50035: 'HeaderItem', 50036: 'Table',
    50037: 'TitleBar', 50038: 'Separator',
}

KIND_LABELS = {'note': 'Note 墨迹', 'ppt': 'PPT 墨迹', 'typing': '局部打字'}

LOG_PATH = os.path.join(str(app_paths.data_dir()), 'auto.log')


def auto_log(text):
    """Append one diagnostic line to data/auto.log (never raises)."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as handle:
            handle.write('%s %s\n'
                         % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except BaseException:
        pass


def api(name, restype, *args):
    """Bind a private function object instead of mutating shared argtypes.

    Every view module loads the same ``raw_probe.user32``; when they set
    ``argtypes`` for their own struct classes the last import wins and the
    others raise ``ArgumentError``. A private ``WINFUNCTYPE`` callable keeps
    this module independent of that import order.
    """
    return C.WINFUNCTYPE(restype, *args)((name, U))


class GUITHREADINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                ('hwndActive', W.HWND), ('hwndFocus', W.HWND),
                ('hwndCapture', W.HWND), ('hwndMenuOwner', W.HWND),
                ('hwndMoveSize', W.HWND), ('hwndCaret', W.HWND),
                ('rcCaret', W.RECT)]


class MONITORINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT),
                ('rcWork', W.RECT), ('dwFlags', W.DWORD)]


class _GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD),
                ('Data4', C.c_ubyte * 8)]


class _VARIANT_VALUE(C.Union):
    _fields_ = [('llVal', C.c_longlong), ('lVal', C.c_long),
                ('boolVal', W.VARIANT_BOOL), ('dblVal', C.c_double),
                ('punkVal', C.c_void_p), ('bstrVal', C.c_void_p),
                ('scode', C.c_longlong), ('ullVal', C.c_ulonglong)]


class _VARIANT(C.Structure):
    _fields_ = [('vt', W.USHORT), ('r1', W.USHORT), ('r2', W.USHORT),
                ('r3', W.USHORT), ('u', _VARIANT_VALUE)]


GetWindowThreadProcessId = api('GetWindowThreadProcessId', W.DWORD, W.HWND,
                               C.POINTER(W.DWORD))
GetClassNameW = api('GetClassNameW', C.c_int, W.HWND, W.LPWSTR, C.c_int)
GetWindowRect = api('GetWindowRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
MonitorFromWindow = api('MonitorFromWindow', W.HANDLE, W.HWND, W.DWORD)
GetMonitorInfoW = api('GetMonitorInfoW', W.BOOL, W.HANDLE,
                      C.POINTER(MONITORINFO))
GetGUIThreadInfo = api('GetGUIThreadInfo', W.BOOL, W.DWORD,
                       C.POINTER(GUITHREADINFO))
GetAncestor = api('GetAncestor', W.HWND, W.HWND, W.UINT)
IsIconic = api('IsIconic', W.BOOL, W.HWND)
GetForegroundWindow = api('GetForegroundWindow', W.HWND)
IsWindow = api('IsWindow', W.BOOL, W.HWND)
GetCursorPos = api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))

kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
kernel32.OpenProcess.restype = W.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = W.BOOL
kernel32.CloseHandle.argtypes = [W.HANDLE]
kernel32.CloseHandle.restype = W.BOOL

ole32.CLSIDFromString.argtypes = [W.LPCWSTR, C.POINTER(_GUID)]
ole32.CLSIDFromString.restype = C.c_long
ole32.CoInitializeEx.argtypes = [C.c_void_p, W.DWORD]
ole32.CoInitializeEx.restype = C.c_long
ole32.CoCreateInstance.argtypes = [C.POINTER(_GUID), C.c_void_p, W.DWORD,
                                   C.POINTER(_GUID),
                                   C.POINTER(C.c_void_p)]
ole32.CoCreateInstance.restype = C.c_long

oleaut32 = C.OleDLL('oleaut32')
oleaut32.SafeArrayGetLBound.argtypes = [C.c_void_p, W.UINT,
                                        C.POINTER(C.c_long)]
oleaut32.SafeArrayGetLBound.restype = C.c_long
oleaut32.SafeArrayGetUBound.argtypes = [C.c_void_p, W.UINT,
                                        C.POINTER(C.c_long)]
oleaut32.SafeArrayGetUBound.restype = C.c_long
oleaut32.SafeArrayAccessData.argtypes = [C.c_void_p,
                                         C.POINTER(C.c_void_p)]
oleaut32.SafeArrayAccessData.restype = C.c_long
oleaut32.SafeArrayUnaccessData.argtypes = [C.c_void_p]
oleaut32.SafeArrayUnaccessData.restype = C.c_long
oleaut32.SafeArrayDestroy.argtypes = [C.c_void_p]
oleaut32.SafeArrayDestroy.restype = C.c_long
oleaut32.SysFreeString.argtypes = [C.c_void_p]
oleaut32.SysFreeString.restype = None  # void; OleDLL would read EAX as HRESULT

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = 0x80010106
CLSID_CUIAUTOMATION = '{FF48DBA4-60EF-4201-AA87-54103EEF594E}'
IID_IUIAUTOMATION = '{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}'
# IUIAutomation vtable: ElementFromPoint is index 7, GetFocusedElement is 8.
# IUIAutomationElement vtable: GetCurrentPropertyValue is index 10.
VT_I4 = 3

_uia = None
_uia_failed = False
EnumChildProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
EnumChildWindows = api('EnumChildWindows', W.BOOL, W.HWND, EnumChildProc,
                       W.LPARAM)


def basename(path):
    """Case-folded exe name from a full Windows path (or just the name)."""
    if not path:
        return ''
    return os.path.basename(str(path).replace('\\', '/')).casefold()


def _guid(text):
    value = _GUID()
    if ole32.CLSIDFromString(text, C.byref(value)) != 0:
        raise OSError('bad GUID %s' % text)
    return value


def _com_call(ptr, index, restype, argtypes, *args):
    vtable = C.cast(ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
    fn = C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtable[index])
    return fn(ptr, *args)


def _automation():
    """Lazily create the out-of-process IUIAutomation object (read-only)."""
    global _uia, _uia_failed
    if _uia is not None:
        return _uia
    if _uia_failed:
        return None
    try:
        hres = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if (hres & 0xFFFFFFFF) not in (0, 1, RPC_E_CHANGED_MODE):
            raise OSError('CoInitializeEx 0x%08x' % (hres & 0xFFFFFFFF))
        ptr = C.c_void_p()
        hres = ole32.CoCreateInstance(
            C.byref(_guid(CLSID_CUIAUTOMATION)), None, CLSCTX_INPROC_SERVER,
            C.byref(_guid(IID_IUIAUTOMATION)), C.byref(ptr))
        if hres != 0 or not ptr.value:
            raise OSError('CoCreateInstance 0x%08x' % (hres & 0xFFFFFFFF))
        _uia = ptr
    except BaseException:
        _uia_failed = True
        _uia = None
    return _uia


def focused_element():
    """Global UI Automation focused element, or None."""
    automation = _automation()
    if automation is None:
        return None
    element = C.c_void_p()
    try:
        if _com_call(automation, 8, C.c_long, (C.POINTER(C.c_void_p),),
                     C.byref(element)) != 0 or not element.value:
            return None
        return element
    except BaseException:
        return None


def focus_control_type():
    """UI Automation control type of the focused element, 0 when unknown."""
    element = focused_element()
    if element is None:
        return 0
    try:
        return element_control_type(element)
    except BaseException:
        return 0
    finally:
        release_element(element)


def point_control_type():
    """Control type of the element under the mouse, 0 when unknown.

    The focused element is a toolbar button in many editors after a click,
    while the text area the user intends to write into sits under the mouse.
    """
    automation = _automation()
    if automation is None:
        return 0
    point = W.POINT()
    if not GetCursorPos(C.byref(point)):
        return 0
    element = C.c_void_p()
    try:
        if _com_call(automation, 7, C.c_long,
                     (W.POINT, C.POINTER(C.c_void_p)), point,
                     C.byref(element)) != 0 or not element.value:
            return 0
        return element_control_type(element)
    except BaseException:
        return 0
    finally:
        release_element(element)


def element_control_type(element):
    var = _VARIANT()
    if _com_call(element, 10, C.c_long, (C.c_int, C.POINTER(_VARIANT)),
                 UIA_CONTROL_TYPE_PROPERTY, C.byref(var)) != 0:
        return 0
    return int(var.u.lVal) if var.vt == VT_I4 else 0


def focused_editable(pid=0):
    """The focused UIA element when it is a text field in process ``pid``.

    Returned AddRef'd; the caller must ``release_element`` it. Captured on
    Alt down, because releasing Alt in a normal browser window moves the
    keyboard focus to a toolbar button and the field is hard to find then.
    """
    element = focused_element()
    if element is None or not element.value:
        return None
    try:
        if pid and _element_process_id(element) != pid:
            release_element(element)
            return None
        if element_control_type(element) not in EDITABLE_TYPES:
            release_element(element)
            return None
        return element
    except BaseException:
        release_element(element)
        return None


def set_focus(element):
    """IUIAutomationElement::SetFocus; False when the element is gone."""
    if element is None or not element.value:
        return False
    try:
        return _com_call(element, 3, C.c_long, ()) == 0
    except BaseException:
        return False


def release_element(element):
    if element.value:
        try:
            _com_call(element, 2, C.c_ulong, ())
        except BaseException:
            pass


def _element_process_id(element):
    var = _VARIANT()
    if _com_call(element, 10, C.c_long, (C.c_int, C.POINTER(_VARIANT)),
                 UIA_CURRENT_PROCESS_ID, C.byref(var)) != 0:
        return 0
    return int(var.u.lVal) if var.vt == VT_I4 else 0


def _safe_rects(array):
    """Copy (left, top, width, height) doubles out of a returned SAFEARRAY."""
    if not array.value:
        return []
    rects = []
    try:
        low, high = C.c_long(), C.c_long()
        if oleaut32.SafeArrayGetLBound(array, 1, C.byref(low)) != 0:
            return []
        if oleaut32.SafeArrayGetUBound(array, 1, C.byref(high)) != 0:
            return []
        count = high.value - low.value + 1
        if count < 4:
            return []
        data = C.c_void_p()
        if oleaut32.SafeArrayAccessData(array, C.byref(data)) != 0:
            return []
        try:
            values = C.cast(data, C.POINTER(C.c_double * count)).contents
            for index in range(0, count - 3, 4):
                rects.append(tuple(float(values[index + i])
                                   for i in range(4)))
        finally:
            oleaut32.SafeArrayUnaccessData(array)
    finally:
        oleaut32.SafeArrayDestroy(array)
    return rects


def _range_rects(text_range):
    array = C.c_void_p()
    if _com_call(text_range, 10, C.c_long, (C.POINTER(C.c_void_p),),
                 C.byref(array)) != 0:
        return []
    return _safe_rects(array)


def _range_text(text_range):
    text = C.c_void_p()
    if _com_call(text_range, 12, C.c_long, (C.c_int, C.POINTER(C.c_void_p)),
                 -1, C.byref(text)) != 0 or not text.value:
        return None
    try:
        return C.wstring_at(text.value)
    finally:
        oleaut32.SysFreeString(text)


def _selection_range(pattern):
    """Last selected text range of a text pattern, or None."""
    ranges = C.c_void_p()
    try:
        if _com_call(pattern, 5, C.c_long, (C.POINTER(C.c_void_p),),
                     C.byref(ranges)) != 0 or not ranges.value:
            return None
        length = C.c_int()
        if _com_call(ranges, 3, C.c_long, (C.POINTER(C.c_int),),
                     C.byref(length)) != 0 or length.value < 1:
            return None
        text_range = C.c_void_p()
        if _com_call(ranges, 4, C.c_long, (C.c_int, C.POINTER(C.c_void_p)),
                     length.value - 1, C.byref(text_range)) != 0:
            return None
        return text_range if text_range.value else None
    finally:
        if ranges.value:
            try:
                _com_call(ranges, 2, C.c_ulong, ())
            except BaseException:
                pass


CARET_MAX_WIDTH = 8.0   # a collapsed caret is a thin vertical bar
CARET_MAX_HEIGHT = 32.0  # first-line estimate when only a line box is known


def caret_from_rects(rects, text=''):
    """Screen ``(right, bottom, height)`` from a text range's rectangles.

    A collapsed selection is normally reported as a thin bar at the caret
    (measured: 1 px wide in Chromium/Electron, at the exact caret x). Some
    editors instead report the whole line box for an empty field, where the
    caret sits at the start of the first line; that height is only a
    placement hint, so the first line is estimated.
    """
    if not rects:
        return None
    left, top, width, height = rects[-1]
    if height <= 0:
        return None
    if text:
        return (left + width, top + height, height)  # caret at selection end
    if 0 <= width <= CARET_MAX_WIDTH:
        return (left + width, top + height, height)
    line = min(height, CARET_MAX_HEIGHT)
    return (left + 1, top + line, line)


def _element_caret(element):
    """Screen ``(right, bottom, height)`` of the element's caret, or None."""
    pattern = C.c_void_p()
    if _com_call(element, 16, C.c_long, (C.c_int, C.POINTER(C.c_void_p)),
                 UIA_TEXT_PATTERN, C.byref(pattern)) != 0 or not pattern.value:
        return None
    text_range = _selection_range(pattern)
    if text_range is None:
        _com_call(pattern, 2, C.c_ulong, ())
        return None
    try:
        rects = _range_rects(text_range)
        text = _range_text(text_range) or ''
        return caret_from_rects(rects, text)
    except BaseException:
        return None
    finally:
        try:
            _com_call(text_range, 2, C.c_ulong, ())
        except BaseException:
            pass
        try:
            _com_call(pattern, 2, C.c_ulong, ())
        except BaseException:
            pass


def _caret_from(element, pid):
    if not element.value:
        return None
    if pid and _element_process_id(element) != pid:
        return None
    bounds = _element_caret(element)
    if bounds is None:
        return None
    x, y, height = bounds
    if not (1 <= height <= 400 and -100000 < x < 100000
            and -100000 < y < 100000):
        return None
    return bounds


def caret_bounds(hwnd=None):
    """Screen ``(right, bottom, height)`` of the caret through UI Automation.

    Chromium/Electron editors (Qoder, browsers) expose no Win32 caret; their
    text selection does carry a caret rectangle. Called once per Alt hold.
    The focused element is preferred; when focus sits elsewhere (taskbar, a
    toolbar button) the element under the pointer is used, because that is
    where the user clicked to place the caret.
    """
    automation = _automation()
    if automation is None:
        return None
    target = hwnd or GetForegroundWindow()
    pid = process_id(resolve_uwp(target)) if target else 0
    element = focused_element()
    if element is not None:
        try:
            bounds = _caret_from(element, pid)
        except BaseException:
            bounds = None
        finally:
            release_element(element)
        if bounds is not None:
            return bounds
    element = C.c_void_p()
    try:
        point = W.POINT()
        if not GetCursorPos(C.byref(point)):
            return None
        if _com_call(automation, 7, C.c_long,
                     (W.POINT, C.POINTER(C.c_void_p)), point,
                     C.byref(element)) != 0:
            return None
        return _caret_from(element, pid)
    except BaseException:
        return None
    finally:
        release_element(element)


def classify(process='', class_name='', fullscreen=False, caret=False,
             edit=False, uia=0):
    """Return 'note', 'ppt', 'typing' or None for one sniffed window.

    OneNote wins over everything (its page may show a caret too). A slide
    show wins over a caret: presentations may keep an edit caret behind the
    full-screen show window. UI Automation ``Edit`` always means a text
    field; ``Document`` means an editor/document (Word, WPS, Qoder) but is
    skipped for browsers, where ordinary pages are documents too.
    """
    name = basename(process)
    if name.startswith(ONENOTE_PROCESS):
        return 'note'
    if name in SLIDE_SHOW_PROCESSES and (
            str(class_name).casefold() in SHOW_CLASSES or fullscreen):
        return 'ppt'
    if (name in CHAT_TYPING_PROCESSES
            and str(class_name).casefold() in CHAT_TYPING_CLASSES):
        return 'typing'
    cls = str(class_name).casefold()
    if (name == WECHAT_PROCESS and cls.startswith('qt')
            and cls.endswith(QT_WINDOW_SUFFIX)):
        return 'typing'
    if caret or edit or uia == UIA_EDIT:
        return 'typing'
    if uia == UIA_DOCUMENT and name not in BROWSER_PROCESSES:
        return 'typing'
    return None


class AutoSelector:
    """One host-side Alt detector shared by all auto views.

    It reports a press exactly once, after the same 0.28 s hold gate the
    manual views use (longer than a double-tap press, so a tap never opens a
    view), and stays quiet while a modifier (Ctrl/Shift/Win), Tab, Space or
    F4 is down so Alt+Tab, Alt+F4 and AltGr never trigger.
    """
    HOLD = 0.28
    BLOCK_KEYS = (0x11, 0x10, 0x5B, 0x5C, 0x09, 0x20, 0x73)

    def __init__(self, hold=HOLD):
        self.hold = hold
        self.held = False
        self.blocked = False
        self.since = 0.0

    def reset(self):
        self.held = self.blocked = False
        self.since = 0.0

    def poll(self, now, down, blocked=False):
        if not down:
            self.held = self.blocked = False
            return False
        if not self.held:
            self.held = True
            self.since = now
            self.blocked = bool(blocked)
            return False
        if blocked:
            self.blocked = True  # a late modifier also kills this press
            return False
        if self.blocked or now - self.since < self.hold:
            return False
        self.blocked = True
        return True


def process_id(hwnd):
    pid = W.DWORD()
    if not hwnd or not GetWindowThreadProcessId(hwnd, C.byref(pid)):
        return 0
    return pid.value


def process_name(hwnd):
    """Executable basename for a window; '' when the process is closed."""
    pid = process_id(hwnd)
    if not pid:
        return ''
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ''
    try:
        size = W.DWORD(1024)
        buffer = C.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                   C.byref(size)):
            return ''
        return basename(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def window_class(hwnd):
    buffer = C.create_unicode_buffer(256)
    if not hwnd or not GetClassNameW(hwnd, buffer, 256):
        return ''
    return buffer.value


def resolve_uwp(hwnd):
    """Map a UWP ``ApplicationFrameWindow`` to its app's CoreWindow.

    OneNote for Windows 10 is hosted in an ApplicationFrameHost window, so
    the frame belongs to ``ApplicationFrameHost.exe`` rather than OneNote.
    """
    if not hwnd or window_class(hwnd).casefold() != 'applicationframewindow':
        return hwnd
    found = []

    def visit(child, _param):
        if window_class(child).casefold() == 'windows.ui.core.corewindow':
            found.append(child)
            return False
        return True

    try:
        EnumChildWindows(hwnd, EnumChildProc(visit), 0)
    except BaseException:
        pass
    return found[0] if found else hwnd


def is_fullscreen(hwnd):
    """The window rect covers its monitor (slide shows are borderless)."""
    rect = W.RECT()
    monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = C.sizeof(info)
    if not GetWindowRect(hwnd, C.byref(rect)) or not monitor:
        return False
    if not GetMonitorInfoW(monitor, C.byref(info)):
        return False
    full = info.rcMonitor
    return (abs(rect.left - full.left) <= 2 and abs(rect.top - full.top) <= 2
            and abs(rect.right - full.right) <= 2
            and abs(rect.bottom - full.bottom) <= 2)


def caret_present(foreground):
    """A real caret in the foreground thread, even during a blink-off phase."""
    gti = GUITHREADINFO()
    gti.cbSize = C.sizeof(gti)
    if not GetGUIThreadInfo(0, C.byref(gti)) or not gti.hwndCaret:
        return False
    if not IsWindow(gti.hwndCaret):
        return False
    root = GetAncestor(gti.hwndCaret, GA_ROOT)
    return not (foreground and root and root != foreground)


def focus_is_edit(foreground):
    """Fallback for fields whose caret is not exposed through GTI."""
    gti = GUITHREADINFO()
    gti.cbSize = C.sizeof(gti)
    focus = foreground
    if GetGUIThreadInfo(0, C.byref(gti)) and gti.hwndFocus:
        focus = gti.hwndFocus
    name = window_class(focus).casefold()
    return any(token in name for token in EDIT_TOKENS)


def process_matches(hwnd, expected):
    """True when the window belongs to ``expected`` (prefix match, casefold)."""
    name = process_name(resolve_uwp(hwnd))
    expected = str(expected or '').casefold()
    return bool(name) and bool(expected) and (
        name == expected or name.startswith(expected))


def foreground_kind(details=None, quiet=False):
    """Classify the foreground window once; None means no view should open.

    UI Automation is only consulted when the cheap checks (process, window
    class, full screen, Win32 caret) found nothing, so slide shows and
    classic edit controls never wait on COM. ``quiet`` skips the diagnostic
    line, used by the press-down preview which would otherwise log every
    Alt press twice.
    """
    hwnd = GetForegroundWindow()
    if not hwnd or IsIconic(hwnd):
        return None
    target = resolve_uwp(hwnd)
    pid = process_id(target)
    if pid and pid == kernel32.GetCurrentProcessId():
        return None  # our own overlays must never retrigger us
    process = process_name(target)
    class_name = window_class(target)
    fullscreen = is_fullscreen(hwnd)
    kind = classify(process, class_name, fullscreen)
    caret = edit = False
    uia = 0
    source = ''
    if kind is None:
        caret = caret_present(target)
        edit = focus_is_edit(target)
        if not (caret or edit):
            uia, source = focus_control_type(), 'focus'
            if uia not in (UIA_EDIT, UIA_DOCUMENT):
                pointed = point_control_type()
                if pointed:
                    uia, source = pointed, 'point'
        kind = classify(process, class_name, fullscreen, caret, edit, uia)
    if details is not None:
        details.update(process=process, class_name=class_name,
                       fullscreen=fullscreen, caret=caret, edit=edit, uia=uia,
                       source=source)
    if quiet:
        return kind
    print('auto: %s / %s fullscreen=%s caret=%s edit=%s uia=%s%s -> %s'
          % (process or '-', class_name or '-', fullscreen, caret, edit,
             UIA_LABELS.get(uia, uia or '-'),
             ('@' + source) if source else '', kind or 'no view'), flush=True)
    auto_log('auto: %s / %s fullscreen=%s caret=%s edit=%s uia=%s%s -> %s'
             % (process or '-', class_name or '-', fullscreen, caret, edit,
                UIA_LABELS.get(uia, uia or '-'),
                ('@' + source) if source else '', kind or 'no view'))
    return kind


def self_check():
    """Read-only: print how the current foreground would be classified."""
    details = {}
    kind = foreground_kind(details)
    hwnd = GetForegroundWindow()
    print('auto self-check: hwnd=%s process=%s class=%s fullscreen=%s '
          'caret=%s edit=%s uia=%s%s kind=%s'
          % (hwnd, details.get('process') or '-',
             details.get('class_name') or '-',
             details.get('fullscreen', '-'), details.get('caret', '-'),
             details.get('edit', '-'),
             UIA_LABELS.get(details.get('uia'), details.get('uia') or '-'),
             ('@' + details['source']) if details.get('source') else '',
             kind))
    print('note: OneNote -> note, full-screen PowerPoint/WPS -> ppt, '
          'text caret / UIA Edit / UIA Document -> typing, otherwise no view')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='classify the foreground window only; no capture')
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    parser.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
