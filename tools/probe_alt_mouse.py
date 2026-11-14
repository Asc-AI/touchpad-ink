"""Dev probe: can left-drag injection work while Alt is held?

Alt is the requested write trigger for PPT ink, but an earlier check suggested
Windows drops synthetic mouse input while Alt is down. This probe isolates
that claim on a plain Win32 window:

  1. baseline: hold Alt, inject a left drag
  2. hook: a low-level keyboard hook swallows Alt down/up, then drag
  3. clear: hold Alt, inject Alt up, drag (physical key still "held" in spirit)

It never touches a real document. Run manually; not part of the test suite.
"""
import ctypes as C
from ctypes import wintypes as W
import struct
import threading
import time

import ptp_probe as PT
import raw_probe as R
from ime_window import INPUT
from live_input import U, HOOKPROC

kernel32 = PT.kernel32
_ALIVE = []

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_CLOSE = 0x0010
MK_LBUTTON = 0x0001
SW_SHOW = 5
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
VK_ALT = 0x12

for name, restype, *args in (
        ('SetCursorPos', W.BOOL, C.c_int, C.c_int),
        ('GetCursorPos', W.BOOL, C.POINTER(W.POINT)),
        ('ShowWindow', W.BOOL, W.HWND, C.c_int),
        ('UpdateWindow', W.BOOL, W.HWND),
        ('SetForegroundWindow', W.BOOL, W.HWND),
        ('GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD)),
        ('AttachThreadInput', W.BOOL, W.DWORD, W.DWORD, W.BOOL),
        ('PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM),
        ('DestroyWindow', W.BOOL, W.HWND),
        ('GetMessageW', C.c_int, C.POINTER(R.MSG), W.HWND, W.UINT, W.UINT),
        ('PeekMessageW', W.BOOL, C.POINTER(R.MSG), W.HWND, W.UINT, W.UINT,
         W.UINT),
        ('TranslateMessage', W.BOOL, C.POINTER(R.MSG)),
        ('DispatchMessageW', C.c_ssize_t, C.POINTER(R.MSG)),
        ('GetAsyncKeyState', C.c_short, C.c_int)):
    fn = getattr(U, name)
    fn.restype, fn.argtypes = restype, list(args)

kernel32.GetCurrentThreadId.restype = W.DWORD


def send_key(vk, up=False):
    event = INPUT()
    event.type = 1
    event.u.ki.wVk = vk
    event.u.ki.dwFlags = 0x0002 if up else 0
    return U.SendInput(1, C.byref(event), C.sizeof(INPUT))


def send_mouse(flags, x=0, y=0):
    event = INPUT()
    event.type = 0
    event.u.mi.dx = x
    event.u.mi.dy = y
    event.u.mi.dwFlags = flags
    return U.SendInput(1, C.byref(event), C.sizeof(INPUT))


def move_abs(px, py):
    send_mouse(0x0001 | 0x8000 | 0x4000, px * 65535 // 2559,
               py * 65535 // 1599)


def make_target():
    events = []
    state = {'swallow_alt': False, 'hook_events': 0}

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_KEYUP:
            pass
        if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP,
                   WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP):
                x = struct.unpack_from('<h', struct.pack('<I', lparam))[0]
                y = struct.unpack_from('<h', struct.pack('<I', lparam), 2)[0]
                events.append((msg, x, y))
            else:
                events.append((msg, wparam, 0))
        if msg == WM_CLOSE:
            U.DestroyWindow(hwnd)
            return 0
        return U.DefWindowProcW(hwnd, msg, wparam, lparam)

    def key_hook(code, message, data):
        state['hook_calls'] = state.get('hook_calls', 0) + 1
        if code >= 0 and message in (WM_KEYDOWN, WM_SYSKEYDOWN,
                                     WM_KEYUP, WM_SYSKEYUP):
            vk = C.cast(data, C.POINTER(W.DWORD)).contents.value
            state['last_vk'] = vk
            state['last_msg'] = message
            if vk == VK_ALT and state['swallow_alt']:
                state['hook_events'] += 1
                return 1
        return U.CallNextHookEx(None, code, message, data)

    proc = R.WNDPROC(wndproc)
    kproc = HOOKPROC(key_hook)
    _ALIVE.extend((proc, kproc, state))
    hinst = kernel32.GetModuleHandleW(None)
    name = 'AltMouseProbe'
    wc = R.WNDCLASSEXW()
    _ALIVE.append(wc)
    wc.cbSize = C.sizeof(R.WNDCLASSEXW)
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.hbrBackground = C.c_void_p(6)
    wc.lpszClassName = name
    if not U.RegisterClassExW(C.byref(wc)):
        raise C.WinError(C.get_last_error())
    hwnd = U.CreateWindowExW(0, name, 'Alt mouse probe', 0x90000000,
                             100, 100, 600, 420, None, None, hinst, None)
    if not hwnd:
        raise C.WinError(C.get_last_error())
    U.ShowWindow(hwnd, SW_SHOW)
    U.UpdateWindow(hwnd)

    def loop():
        msg = R.MSG()
        while U.GetMessageW(C.byref(msg), None, 0, 0) > 0:
            U.TranslateMessage(C.byref(msg))
            U.DispatchMessageW(C.byref(msg))

    threading.Thread(target=loop, name='alt probe target', daemon=True).start()
    return hwnd, events, state, kproc


def pump(seconds):
    """Pump this thread's messages so a low-level hook stays responsive."""
    msg = R.MSG()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while U.PeekMessageW(C.byref(msg), None, 0, 0, 1):
            U.TranslateMessage(C.byref(msg))
            U.DispatchMessageW(C.byref(msg))
        time.sleep(0.005)


def foreground(hwnd):
    fg = U.GetForegroundWindow()
    if fg == hwnd:
        return True
    fg_thread = U.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    U.AttachThreadInput(me, fg_thread, True)
    U.SetForegroundWindow(hwnd)
    U.AttachThreadInput(me, fg_thread, False)
    return U.GetForegroundWindow() == hwnd


def drag(events):
    before = len(events)
    move_abs(400, 300)
    pump(0.05)
    send_mouse(0x0002)          # left down
    for i in range(4):
        move_abs(420 + i * 20, 300 + i * 10)
        pump(0.03)
    send_mouse(0x0004)          # left up
    pump(0.2)
    window = events[before:]
    downs = [e for e in window if e[0] == WM_LBUTTONDOWN]
    ups = [e for e in window if e[0] == WM_LBUTTONUP]
    moves = [e for e in window if e[0] == WM_MOUSEMOVE]
    return len(downs), len(ups), len(moves)


def cursor():
    point = W.POINT()
    U.GetCursorPos(C.byref(point))
    return point.x, point.y


def main():
    hwnd, events, state, kproc = make_target()
    time.sleep(0.2)
    if not foreground(hwnd):
        print('FAIL: target not foreground')
        return 1
    U.SetCursorPos(400, 300)
    print('target=%s' % hwnd)

    # 1. baseline: Alt held down, no hook swallowing
    send_key(VK_ALT)
    pump(0.3)
    print('alt state after down:', hex(U.GetAsyncKeyState(VK_ALT) & 0xffff),
          'cursor:', cursor())
    down, up, moving = drag(events)
    print('baseline alt-held drag: down=%d up=%d moves=%d cursor=%s'
          % (down, up, moving, cursor()))
    send_key(VK_ALT, up=True)
    pump(0.3)

    # 2. swallow Alt in a low-level keyboard hook
    hook = U.SetWindowsHookExW(13, kproc, kernel32.GetModuleHandleW(None), 0)
    state['swallow_alt'] = True
    pump(0.1)
    send_key(VK_ALT)
    pump(0.3)
    print('hook=%s swallowed=%d calls=%s last=%s alt state: %s'
          % (bool(hook), state['hook_events'], state.get('hook_calls'),
             (state.get('last_msg'), state.get('last_vk')),
             hex(U.GetAsyncKeyState(VK_ALT) & 0xffff)))
    down, up, moving = drag(events)
    print('swallowed-alt drag: down=%d up=%d moves=%d cursor=%s'
          % (down, up, moving, cursor()))
    send_key(VK_ALT, up=True)
    pump(0.3)
    state['swallow_alt'] = False
    U.UnhookWindowsHookEx(hook)

    # 3. Alt physically down, then synthetic Alt up clears the logical state
    send_key(VK_ALT)
    pump(0.2)
    send_key(VK_ALT, up=True)
    pump(0.2)
    print('cleared alt state:', hex(U.GetAsyncKeyState(VK_ALT) & 0xffff))
    down, up, moving = drag(events)
    print('cleared-alt drag: down=%d up=%d moves=%d cursor=%s'
          % (down, up, moving, cursor()))

    U.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
