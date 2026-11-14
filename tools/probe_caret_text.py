"""Temporary manual probe: verify UIA caret geometry against real text.

Types a known string into the focused field, dumps the TextPattern ranges
with physical (DPI-aware) coordinates, screenshots the field, then clears
the text again (Ctrl+A, Delete). Never presses Enter; nothing is sent.
"""
import ctypes as C
import time

from PIL import ImageGrab

import app_paths
import auto_mode as A
import ime_window as I

TEXT = 'abc中'
VK_CONTROL = 0x11
VK_A = 0x41
VK_DELETE = 0x2E


def key(vk, up=False):
    batch = (I.INPUT * 1)()
    batch[0].type = I.INPUT_KEYBOARD
    batch[0].u.ki = I.KEYBDINPUT(vk, 0, I.KEYEVENTF_KEYUP if up else 0, 0, 0)
    I.U.SendInput(1, batch, C.sizeof(I.INPUT))


def chord(vk):
    key(VK_CONTROL)
    key(vk)
    key(vk, True)
    key(VK_CONTROL, True)


def dump(label):
    element = A.focused_element()
    if element is None:
        print(label, 'no focused element')
        return
    try:
        print(label, 'pid', A._element_process_id(element),
              'kind', A.UIA_LABELS.get(A.element_control_type(element)))
        print(label, 'caret_bounds', A.caret_bounds())
        print(label, '_element_caret', A._element_caret(element))
        pattern = C.c_void_p()
        hr = A._com_call(element, 16, C.c_long, (C.c_int, C.POINTER(C.c_void_p)),
                         A.UIA_TEXT_PATTERN, C.byref(pattern))
        if hr != 0 or not pattern.value:
            print(label, 'no text pattern hr=%d' % hr)
            return
        try:
            text_range = A._selection_range(pattern)
            if text_range is None:
                print(label, 'no selection range')
                return
            try:
                print(label, 'rects', A._range_rects(text_range))
                print(label, 'text', repr(A._range_text(text_range)))
                moved = C.c_int()
                A._com_call(text_range, 13, C.c_long,
                            (C.c_int, C.c_int, C.POINTER(C.c_int)),
                            A.TEXT_UNIT_CHARACTER, -1, C.byref(moved))
                print(label, 'move(-1)=%d rects %s text %r'
                      % (moved.value, A._range_rects(text_range),
                         A._range_text(text_range)))
            finally:
                A._com_call(text_range, 2, C.c_ulong, ())
        finally:
            A._com_call(pattern, 2, C.c_ulong, ())
    finally:
        A.release_element(element)


def main():
    I.enable_dpi_awareness()
    dump('empty')
    hwnd = A.GetForegroundWindow()
    rect = A.W.RECT()
    A.GetWindowRect(hwnd, C.byref(rect))
    print('foreground', hwnd, A.process_name(hwnd),
          (rect.left, rect.top, rect.right, rect.bottom))
    I.send_unicode(TEXT)
    time.sleep(0.6)
    dump('typed')
    bounds = A.caret_bounds()
    print('expected caret near', bounds)
    if bounds:
        x, y, h = bounds
        ImageGrab.grab(bbox=(int(x) - 260, int(y) - 40,
                             int(x) + 260, int(y) + 40)).save(
            app_paths.data_dir() / 'caret_text.png')
    chord(VK_A)
    key(VK_DELETE)
    key(VK_DELETE, True)
    time.sleep(0.3)
    dump('cleared')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
