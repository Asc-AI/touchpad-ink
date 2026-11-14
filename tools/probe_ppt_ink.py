"""Manual probe (never part of the test suite): the new Alt interaction.

Creates a plain Win32 window that logs left-button drags, then exercises the
per-press region, continuous 连写, and the zoom frame. It presses a real Alt
through SendInput and feeds raw frames straight into the view, so nothing here
runs during automated tests.

Checks:
1. Hold Alt: the region centres at the pointer; the pad maps inside it.
2. The pointer is restored to the press position when the hold ends.
3. A second hold elsewhere re-centres the region (no fixed-region leftovers).
4. Two-finger pinch enlarges the region; the light-blue frame disappears when
   one finger lifts.
5. The wheel event zooms both ways.
6. Double Alt enters 连写; holding Alt frees the pointer and releasing it
   rebuilds the region at the new position; double Alt exits.
"""
import ctypes as C
from ctypes import wintypes as W
import struct
import threading
import time
import tkinter as tk

import ppt_ink as P
import ptp_probe as PT
import raw_probe as R
from live_input import U

kernel32 = PT.kernel32
_ALIVE = []  # keep the WNDPROC callback and class data alive

U.ShowWindow.argtypes = [W.HWND, C.c_int]
U.ShowWindow.restype = W.BOOL
U.UpdateWindow.argtypes = [W.HWND]
U.UpdateWindow.restype = W.BOOL
U.SetForegroundWindow.argtypes = [W.HWND]
U.SetForegroundWindow.restype = W.BOOL
U.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
U.GetWindowThreadProcessId.restype = W.DWORD
U.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
U.AttachThreadInput.restype = W.BOOL
U.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
U.PostMessageW.restype = W.BOOL
U.DestroyWindow.argtypes = [W.HWND]
U.DestroyWindow.restype = W.BOOL
U.GetMessageW.argtypes = [C.POINTER(R.MSG), W.HWND, W.UINT, W.UINT]
U.GetMessageW.restype = C.c_int
kernel32.GetCurrentThreadId.restype = W.DWORD

WM_CLOSE = 0x0010
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
SW_SHOW = 5
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001


def frame(t, points):
    return {'t': t, 'scan': 0, 'contacts': [
        {'slot': 1, 'id': cid, 'x': x, 'y': y, 'tip': True,
         'confidence': True, 'flags': 0}
        for cid, x, y in points]}


def key(vk, up=False):
    event = P.INPUT()
    event.type = 1  # INPUT_KEYBOARD
    event.u.ki.wVk = vk
    event.u.ki.dwFlags = 0x0002 if up else 0
    return U.SendInput(1, C.byref(event), C.sizeof(P.INPUT))


def wait_for(predicate, seconds, pump):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_target():
    events = []

    def wndproc(hwnd, msg, wparam, lparam):
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE):
            x = struct.unpack_from('<h', struct.pack('<I', lparam))[0]
            y = struct.unpack_from('<h', struct.pack('<I', lparam), 2)[0]
            events.append((msg, x, y, bool(wparam & MK_LBUTTON)))
        if msg == WM_CLOSE:
            U.DestroyWindow(hwnd)
            return 0
        return U.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = R.WNDPROC(wndproc)
    _ALIVE.append(proc)
    hinst = kernel32.GetModuleHandleW(None)
    name = 'PptInkProbeTarget'
    wc = R.WNDCLASSEXW()
    _ALIVE.append(wc)
    wc.cbSize = C.sizeof(R.WNDCLASSEXW)
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.hbrBackground = C.c_void_p(6)  # COLOR_WINDOW + 1
    wc.lpszClassName = name
    if not U.RegisterClassExW(C.byref(wc)):
        raise C.WinError(C.get_last_error())
    hwnd = U.CreateWindowExW(0, name, 'PPT ink probe target',
                             0x90000000, 150, 150, 520, 360,  # popup, no menu
                             None, None, hinst, None)
    if not hwnd:
        raise C.WinError(C.get_last_error())
    U.ShowWindow(hwnd, SW_SHOW)
    U.UpdateWindow(hwnd)

    def loop():
        msg = R.MSG()
        while U.GetMessageW(C.byref(msg), None, 0, 0) > 0:
            U.TranslateMessage(C.byref(msg))
            U.DispatchMessageW(C.byref(msg))

    threading.Thread(target=loop, name='probe target loop', daemon=True).start()
    return hwnd, events


def foreground(hwnd):
    if U.GetForegroundWindow() == hwnd:
        return True
    fg = U.GetForegroundWindow()
    fg_thread = U.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    U.AttachThreadInput(me, fg_thread, True)
    U.SetForegroundWindow(hwnd)
    U.AttachThreadInput(me, fg_thread, False)
    return U.GetForegroundWindow() == hwnd


def pointer():
    point = W.POINT()
    U.GetCursorPos(C.byref(point))
    return point.x, point.y


def set_pointer(app, x, y):
    app.virtual = P.virtual_screen()
    P.send_mouse(P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE
                 | P.MOUSEEVENTF_VIRTUALDESK,
                 *P.absolute(x, y, *app.virtual))


def close_to(actual, expected, tolerance=2):
    return (abs(actual[0] - expected[0]) <= tolerance
            and abs(actual[1] - expected[1]) <= tolerance)


def close_rect(actual, expected, tolerance=1):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def strokes_of(events):
    """(down client point, flushed end) for each left-button stroke."""
    strokes = []
    current = None
    for event in events:
        if event[0] == WM_LBUTTONDOWN:
            current = [event[1:3], event[1:3]]
            strokes.append(current)
        elif event[0] == WM_MOUSEMOVE and current is not None:
            current[1] = event[1:3]
        elif event[0] == WM_LBUTTONUP:
            current = None
    return strokes


def sleep(seconds, pump):
    wait_for(lambda: False, seconds, pump)


def start_hold(app, pump):
    key(0x12)
    if wait_for(lambda: app.state == 'capturing', 3.0, pump):
        return True
    print('FAIL: holding Alt did not arm capture (state=%s target=%s fg=%s)'
          % (app.state, app.target, U.GetForegroundWindow()))
    key(0x12, up=True)
    return False


def end_hold(app, pump, restore=None):
    key(0x12, up=True)
    ok = wait_for(lambda: app.state == 'idle', 2.0, pump)
    if not ok:
        print('FAIL: releasing Alt did not stop capture (state=%s)'
              % app.state)
    if restore is not None:
        ok = wait_for(lambda: close_to(pointer(), restore), 1.0, pump) and ok
    return ok


def run_hold(app, pump, strokes, restore=None):
    """One complete hold: Alt down, feed the strokes, Alt up."""
    if not start_hold(app, pump):
        return False
    for index, points in enumerate(strokes):
        for step, (cid, x, y) in enumerate(points):
            app.events.put(('frame', frame(index + step * 0.03,
                                           [(cid, x, y)])))
            sleep(0.06, pump)
        app.events.put(('frame', frame(0.5, [])))
        sleep(0.08, pump)
    return end_hold(app, pump, restore)


def double_alt(app, pump):
    for _ in range(2):
        key(0x12)
        sleep(0.06, pump)
        key(0x12, up=True)
        sleep(0.08, pump)


def main():
    P.enable_dpi_awareness()
    hwnd, events = make_target()
    time.sleep(0.2)
    if not foreground(hwnd):
        print('FAIL: target never became foreground (hwnd=%s fg=%s)'
              % (hwnd, U.GetForegroundWindow()))
        return 1
    print('target hwnd=%s client=%s' % (hwnd, P.client_rect(hwnd)))

    root = tk.Tk()
    root.withdraw()
    host = tk.Toplevel(root)
    host.withdraw()
    app = P.PptInk(host)
    pump = lambda: app.root.update()
    if not wait_for(lambda: app.ready, 4.0, pump):
        print('FAIL: capture worker not ready')
        app.close()
        return 1
    if not foreground(hwnd):
        print('FAIL: target lost the foreground (hwnd=%s fg=%s)'
              % (hwnd, U.GetForegroundWindow()))
        app.close()
        return 1

    ok = True
    pad_strokes = [
        [(3, 40, 40), (3, 400, 60), (3, 460, 300)],
        # Starts near where the first stroke lifted: a small hand move that
        # the pen follows (a far-left plant would be treated as a wrist
        # reset and leave the pen at the first stroke's end).
        [(5, 520, 320), (5, 700, 300)],
    ]

    # 1-3: per-press anchoring + pointer restore.
    first_press = (600, 450)
    set_pointer(app, *first_press)
    before = len(events)
    if not run_hold(app, pump, pad_strokes, restore=first_press):
        app.close()
        return 1
    rect = app.rect
    hold1 = events[before:]
    strokes1 = strokes_of(hold1)
    if len(strokes1) < 2:
        print('FAIL: hold 1 produced %d strokes, expected 2'
              % len(strokes1))
        app.close()
        return 1
    region1 = app.region
    expected1 = P.region_rect(rect, first_press, app.region_scale,
                              P.REGION_ANCHOR)
    print('hold 1: rect=%s region=%s expected=%s'
          % (rect, region1, expected1))
    if not close_to(app.anchor, first_press) or not close_rect(region1,
                                                               expected1):
        print('FAIL: hold 1 moved the region although the ink stayed inside')
        ok = False

    second_press = (900, 600)
    set_pointer(app, *second_press)
    before = len(events)
    if not run_hold(app, pump, pad_strokes, restore=second_press):
        app.close()
        return 1
    hold2 = events[before:]
    strokes2 = strokes_of(hold2)
    if len(strokes2) < 2:
        print('FAIL: hold 2 produced %d strokes, expected 2'
              % len(strokes2))
        app.close()
        return 1
    region2 = app.region
    expected2 = P.region_rect(rect, second_press, app.region_scale,
                              P.REGION_ANCHOR)
    print('hold 2: region=%s expected=%s' % (region2, expected2))
    if (not close_to(app.anchor, second_press)
            or not close_rect(region2, expected2) or region2 == region1):
        print('FAIL: hold 2 did not rebuild the region at the new pointer')
        ok = False
    downs1 = [e for e in hold1 if e[0] == WM_LBUTTONDOWN]
    downs2 = [e for e in hold2 if e[0] == WM_LBUTTONDOWN]
    if not downs1 or not downs2 or downs1[0][1:3] == downs2[0][1:3]:
        print('FAIL: the same pad point landed on the same spot in both holds')
        ok = False
    for label, hold, press, region in (
            ('hold 1', hold1, first_press, region1),
            ('hold 2', hold2, second_press, region2)):
        strokes = strokes_of(hold)
        sx = region[2] / float(P.PAD_MAX_X)
        sy = region[3] / float(P.PAD_MAX_Y)
        # The pad is a mouse, not a tablet: each stroke starts at the pointer
        # that built the frame plus the hand's own travel since the first
        # touch (lift, move, plant), scaled by the region. It is neither an
        # absolute map of the new contact nor a chain onto the last end.
        origin = pad_strokes[0][0][1:]
        for index, points in enumerate(pad_strokes):
            pad = points[0][1:]
            expected = (press[0] - rect[0] + (pad[0] - origin[0]) * sx,
                        press[1] - rect[1] + (pad[1] - origin[1]) * sy)
            actual = strokes[index][0] if index < len(strokes) else None
            if actual is None or not close_to(actual, expected, 3):
                print('FAIL: %s stroke %d starts at %s expected %s'
                      % (label, index + 1, actual, expected))
                ok = False
        if len(strokes) > 1:
            gap = (strokes[1][0][0] - strokes[0][1][0],
                   strokes[1][0][1] - strokes[0][1][1])
            print('%s: first end %s, second start %s (gap %.0f,%.0f)'
                  % (label, strokes[0][1], strokes[1][0], *gap))
            if not (abs(gap[0]) + abs(gap[1]) > 10):
                print('FAIL: %s second stroke still chains onto the first'
                      % label)
                ok = False
    if app.frame is None or not any('PPT 墨迹' in line
                                    for line in app.frame.status):
        print('FAIL: the mode/state frame text was never drawn')
        ok = False

    # 4: pinch zoom, frame hides when a finger lifts.
    set_pointer(app, 600, 450)
    scale_before = app.region_scale
    if not start_hold(app, pump):
        app.close()
        return 1
    app.events.put(('frame', frame(0.0, [(1, 200, 200), (2, 400, 200)])))
    sleep(0.05, pump)
    if not app.zooming:
        print('FAIL: two contacts did not start the pinch zoom')
        ok = False
    app.events.put(('frame', frame(0.05, [(1, 200, 200), (2, 600, 200)])))
    sleep(0.05, pump)
    scale_after = app.region_scale
    print('pinch: scale %.2f -> %.2f region=%s'
          % (scale_before, scale_after, app.region))
    if not scale_after < scale_before:
        print('FAIL: fingers apart did not enlarge the region')
        ok = False
    if app.frame is None or app.frame.win.state() == 'withdrawn':
        print('FAIL: the light-blue zoom frame was not visible')
        ok = False
    app.events.put(('frame', frame(0.10, [(1, 200, 200)])))
    sleep(0.05, pump)
    if app.zooming or app.frame.win.state() != 'withdrawn':
        print('FAIL: the frame stayed after a finger lifted')
        ok = False

    # 5: wheel zoom both ways.
    before_wheel = app.region_scale
    app.events.put(('wheel', 120))
    sleep(0.05, pump)
    if not app.region_scale < before_wheel:
        print('FAIL: wheel up did not enlarge the region')
        ok = False
    app.events.put(('wheel', -120))
    sleep(0.05, pump)
    if abs(app.region_scale - before_wheel) > 0.02:
        print('FAIL: wheel down did not restore the region (%.2f)'
              % app.region_scale)
        ok = False
    if not end_hold(app, pump):
        app.close()
        return 1

    # 6: continuous 连写: double Alt, move with Alt, double Alt exits.
    if not foreground(hwnd):
        foreground(hwnd)
    double_alt(app, pump)
    if not wait_for(lambda: app.continuous and app.state == 'capturing',
                    2.0, pump):
        print('FAIL: double Alt did not enter continuous (state=%s cont=%s)'
              % (app.state, app.continuous))
        app.close()
        return 1
    print('continuous entered: region=%s scale=%.2f'
          % (app.region, app.region_scale))
    app.events.put(('frame', frame(0.0, [(3, 100, 100), (3, 300, 200)])))
    sleep(0.10, pump)

    set_pointer(app, 600, 450)
    key(0x12)
    sleep(0.10, pump)
    if not app.moving or app.worker.suppress:
        print('FAIL: Alt down in continuous did not free the pointer '
              '(moving=%s suppress=%s)' % (app.moving, app.worker.suppress))
        ok = False
    set_pointer(app, 1050, 700)
    sleep(0.05, pump)
    key(0x12, up=True)
    if not wait_for(lambda: not app.moving, 1.0, pump):
        print('FAIL: Alt up in continuous did not end the move')
        ok = False
    expected3 = P.region_rect(rect, (1050, 700), app.region_scale,
                              P.REGION_ANCHOR)
    print('continuous move: region=%s expected=%s'
          % (app.region, expected3))
    if app.region != expected3 or not app.worker.suppress:
        print('FAIL: releasing Alt did not rebuild the region at the pointer')
        ok = False

    double_alt(app, pump)
    if not wait_for(lambda: not app.continuous, 2.0, pump):
        print('FAIL: double Alt did not leave continuous')
        ok = False
    if not wait_for(lambda: app.state == 'idle', 1.0, pump):
        print('FAIL: continuous exit left the capture armed')
        ok = False
    print('ppt ink probe: %s' % ('PASS' if ok else 'FAIL'))
    app.close()
    try:
        U.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        root.destroy()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
