"""Manual probe: read-only UI Automation focus inspection (no capture).

Prints the UI Automation focus of the current foreground window: control
type, class name and whether it is keyboard focusable. Used to decide how
auto mode should recognise text editors that expose no Win32 caret
(Chromium/Electron/Qoder, Word, WPS ...).

Manual only; not part of the test suite and never injects input.
"""
import ctypes as C
from ctypes import wintypes as W
import sys

ole32 = C.OleDLL('ole32')
U = C.WinDLL('user32', use_last_error=True)

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = 0x80010106

UIA_ControlTypePropertyId = 30003
UIA_IsKeyboardFocusablePropertyId = 30009
UIA_IsEnabledPropertyId = 30010
UIA_ClassNamePropertyId = 30012
UIA_LocalizedControlTypePropertyId = 30004
UIA_IsTextPatternAvailablePropertyId = 30040
UIA_IsValuePatternAvailablePropertyId = 30043
UIA_ValueIsReadOnlyPropertyId = 30046

CONTROL_TYPES = {
    50000: 'Button', 50001: 'Calendar', 50002: 'CheckBox', 50003: 'ComboBox',
    50004: 'Edit', 50005: 'Hyperlink', 50006: 'Image', 50007: 'ListItem',
    50008: 'List', 50009: 'Menu', 50010: 'MenuBar', 50011: 'MenuItem',
    50012: 'ProgressBar', 50013: 'RadioButton', 50014: 'ScrollBar',
    50015: 'Slider', 50016: 'Spinner', 50017: 'StatusBar', 50018: 'Tab',
    50019: 'TabItem', 50020: 'Text', 50021: 'ToolBar', 50022: 'ToolTip',
    50023: 'Tree', 50024: 'TreeItem', 50025: 'Custom', 50026: 'Group',
    50027: 'Thumb', 50028: 'DataGrid', 50029: 'DataItem', 50030: 'Document',
    50031: 'SplitButton', 50032: 'Window', 50033: 'Pane', 50034: 'Header',
    50035: 'HeaderItem', 50036: 'Table', 50037: 'TitleBar',
    50038: 'Separator', 50039: 'SemanticZoom', 50040: 'AppBar',
}


class GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD),
                ('Data4', C.c_ubyte * 8)]


class VARIANT_UNION(C.Union):
    _fields_ = [('llVal', C.c_longlong), ('lVal', C.c_long),
                ('boolVal', W.VARIANT_BOOL), ('dblVal', C.c_double),
                ('punkVal', C.c_void_p), ('bstrVal', C.c_void_p),
                ('scode', C.c_longlong), ('ullVal', C.c_ulonglong)]


class VARIANT(C.Structure):
    _fields_ = [('vt', W.USHORT), ('r1', W.USHORT), ('r2', W.USHORT),
                ('r3', W.USHORT), ('u', VARIANT_UNION)]


def clsid(text):
    g = GUID()
    if ole32.CLSIDFromString(C.c_wchar_p(text), C.byref(g)) != 0:
        raise OSError('bad GUID %s' % text)
    return g


def call(ptr, index, restype, argtypes, *args):
    vtbl = C.cast(ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
    fn = C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def release(ptr):
    call(ptr, 2, C.c_ulong, ())


def main():
    hres = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hres not in (0, 1) and (hres & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
        print('CoInitializeEx failed 0x%08x' % (hres & 0xFFFFFFFF))
        return 1
    iid = clsid('{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}')
    automation = C.c_void_p()
    hres = ole32.CoCreateInstance(
        C.byref(clsid('{FF48DBA4-60EF-4201-AA87-54103EEF594E}')), None,
        CLSCTX_INPROC_SERVER, C.byref(iid), C.byref(automation))
    if hres != 0 or not automation:
        print('CoCreateInstance failed 0x%08x' % (hres & 0xFFFFFFFF))
        return 1
    try:
        element = C.c_void_p()
        hres = call(automation, 8, C.c_long, (C.POINTER(C.c_void_p),),
                    C.byref(element))
        if hres != 0 or not element:
            print('GetFocusedElement failed 0x%08x' % (hres & 0xFFFFFFFF))
            return 1
        try:
            for pid, label in (
                    (UIA_ControlTypePropertyId, 'control type'),
                    (UIA_ClassNamePropertyId, 'class'),
                    (UIA_LocalizedControlTypePropertyId, 'localized'),
                    (UIA_IsKeyboardFocusablePropertyId, 'focusable'),
                    (UIA_IsEnabledPropertyId, 'enabled'),
                    (UIA_IsTextPatternAvailablePropertyId, 'textpattern'),
                    (UIA_IsValuePatternAvailablePropertyId, 'valpattern'),
                    (UIA_ValueIsReadOnlyPropertyId, 'value ro')):
                var = VARIANT()
                hres = call(element, 10, C.c_long,
                            (C.c_int, C.POINTER(VARIANT)), pid,
                            C.byref(var))
                if hres != 0:
                    print('%-11s <hr=0x%08x>' % (label, hres & 0xFFFFFFFF))
                elif var.vt == 3:
                    raw = var.u.lVal
                    extra = (' (%s)' % CONTROL_TYPES.get(raw, '?'))
                    print('%-11s %s%s' % (label, raw, extra))
                elif var.vt == 11:
                    print('%-11s %s' % (label, bool(var.u.boolVal)))
                elif var.vt == 8:
                    text = C.cast(var.u.bstrVal, C.c_wchar_p).value
                    print('%-11s %s' % (label, text or '-'))
                    C.WinDLL('oleaut32').SysFreeString(C.c_void_p(var.u.bstrVal))
                else:
                    print('%-11s vt=%s' % (label, var.vt))
        finally:
            release(element)
    finally:
        release(automation)
    fg = U.GetForegroundWindow()
    buf = C.create_unicode_buffer(256)
    U.GetClassNameW(fg, buf, 256)
    print('foreground hwnd=%s class=%s' % (fg, buf.value))
    return 0


if __name__ == '__main__':
    sys.exit(main())
