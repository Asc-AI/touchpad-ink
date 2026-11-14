"""Manual probe: print the caret rectangle Win32 and UI Automation report.

Read-only. Run with a text field focused (Qoder chat, a browser textarea) to
see which layer can anchor the typing overlay:

    python tools/probe_uia_caret.py [process-prefix]

Without focus the probe also inspects the element under the pointer and the
main window of ``process-prefix`` (default ``qoder``), so it still works
while the agent itself runs in the background.
"""
import ctypes as C
from ctypes import wintypes as W
import sys
import time

import auto_mode as A

TREE_SCOPE_DESCENDANTS = 4


def find_edits(automation, parent):
    """Edit descendants of ``parent`` (read-only UI Automation walk)."""
    condition = C.c_void_p()
    array = C.c_void_p()
    value = A._VARIANT()
    value.vt = A.VT_I4
    value.u.lVal = A.UIA_EDIT
    try:
        if A._com_call(automation, 23, C.c_long,
                       (C.c_int, A._VARIANT, C.POINTER(C.c_void_p)),
                       A.UIA_CONTROL_TYPE_PROPERTY, value,
                       C.byref(condition)) != 0 or not condition.value:
            return []
        if A._com_call(parent, 6, C.c_long,
                       (C.c_int, C.c_void_p, C.POINTER(C.c_void_p)),
                       TREE_SCOPE_DESCENDANTS, condition,
                       C.byref(array)) != 0 or not array.value:
            return []
        length = C.c_int()
        if A._com_call(array, 3, C.c_long, (C.POINTER(C.c_int),),
                       C.byref(length)) != 0:
            return []
        found = []
        for index in range(length.value):
            element = C.c_void_p()
            if A._com_call(array, 4, C.c_long,
                           (C.c_int, C.POINTER(C.c_void_p)), index,
                           C.byref(element)) == 0 and element.value:
                found.append(element)
        return found
    except BaseException:
        return []
    finally:
        for pointer in (array, condition):
            if pointer.value:
                A._com_call(pointer, 2, C.c_ulong, ())


def win32_caret(target):
    gti = A.GUITHREADINFO()
    gti.cbSize = C.sizeof(gti)
    if not A.GetGUIThreadInfo(0, C.byref(gti)) or not gti.hwndCaret:
        return None
    point = W.POINT(gti.rcCaret.left, gti.rcCaret.bottom)
    if not A.U.ClientToScreen(gti.hwndCaret, C.byref(point)):
        return None
    return (point.x, point.y, gti.rcCaret.bottom - gti.rcCaret.top,
            A.GetAncestor(gti.hwndCaret, A.GA_ROOT) == target)


def inspect(element):
    if not element.value:
        return None
    pid = A._element_process_id(element)
    kind = A.UIA_LABELS.get(A.element_control_type(element), '-')
    return {'pid': pid, 'kind': kind, 'caret': A._element_caret(element)}


EnumProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


def find_window(prefix):
    found = []

    def visit(hwnd, _param):
        if hwnd and A.IsWindow(hwnd) and not A.IsIconic(hwnd):
            name = A.process_name(hwnd)
            if name.startswith(prefix):
                found.append(hwnd)
        return True

    A.U.EnumWindows(EnumProc(visit), 0)
    return found[0] if found else 0


def main():
    prefix = (sys.argv[1] if len(sys.argv) > 1 else 'qoder').casefold()
    hwnd = A.GetForegroundWindow()
    print('foreground: hwnd=%s process=%s class=%s'
          % (hwnd, A.process_name(hwnd) or '-', A.window_class(hwnd) or '-'))
    print('win32 caret:', win32_caret(hwnd))
    print('uia caret:  ', A.caret_bounds(hwnd))
    point = W.POINT()
    A.GetCursorPos(C.byref(point))
    print('cursor:     ', (point.x, point.y))

    automation = A._automation()
    element = C.c_void_p()
    try:
        A._com_call(automation, 7, C.c_long,
                    (W.POINT, C.POINTER(C.c_void_p)), point,
                    C.byref(element))
        print('point element:', inspect(element))
    except BaseException as exc:
        print('point element failed:', exc)
    finally:
        A.release_element(element)

    target = find_window(prefix)
    print('%s window: %s' % (prefix, target))
    if target:
        print('win32 caret: ', win32_caret(target))
        print('uia caret:   ', A.caret_bounds(target))
        element = C.c_void_p()
        try:
            A._com_call(automation, 6, C.c_long,
                        (W.HWND, C.POINTER(C.c_void_p)), target,
                        C.byref(element))
            print('window element:', inspect(element))
            edits = find_edits(automation, element)
            print('edit descendants: %d' % len(edits))
            for index, edit in enumerate(edits[:6]):
                print('  edit %d: %s' % (index, inspect(edit)))
            if edits:
                A._com_call(edits[0], 3, C.c_long, ())  # SetFocus
                time.sleep(0.3)
                hwnd = A.GetForegroundWindow()
                print('after focus: foreground=%s %s'
                      % (hwnd, A.process_name(hwnd) or '-'))
                print('after focus caret:', A.caret_bounds(hwnd))
                print('focused element:', inspect(A.focused_element()))
        except BaseException as exc:
            print('window element failed:', exc)
        finally:
            A.release_element(element)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
