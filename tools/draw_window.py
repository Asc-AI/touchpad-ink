"""Full-screen touchpad annotation layer ("全域画图").

Choose 全域画图 from the tray, then hold Alt and write. Contacts are mapped
over the whole monitor under the mouse, so a slide in PowerPoint can be
annotated even though no text field is focused. Releasing Alt keeps the ink;
the next Alt press clears it and starts a new annotation.

The window is color-key transparent, click-through (WS_EX_TRANSPARENT) and
never activates, so the presentation underneath keeps focus and stays usable
while ink is visible. Esc stops the current capture, clears and hides. The
usual Ctrl+F8 quit is handled by the host.
"""
import ctypes as C
from ctypes import wintypes as W
import queue
import sys
import time
import tkinter as tk
import traceback

from hud import StatusHud
from ime_window import (BASE_INK, GWL_EXSTYLE, HALO, KEY, MONITORINFO,
                        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, api,
                        apply_overlay_style, enable_dpi_awareness, ui_scale)
from live_input import LiveInput, StrokeTracker, U
from live_window import Session
import pad_info

WS_EX_TRANSPARENT = 0x00000020
SWP_NOACTIVATE = 0x0010

api('MonitorFromPoint', W.HANDLE, W.POINT, W.DWORD)
GetMonitorInfoW = C.WINFUNCTYPE(
    W.BOOL, W.HANDLE, C.POINTER(MONITORINFO))(('GetMonitorInfoW', U))
api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))
api('GetWindowLongPtrW', C.c_ssize_t, W.HWND, C.c_int)
api('SetWindowLongPtrW', C.c_ssize_t, W.HWND, C.c_int, C.c_ssize_t)
api('SetWindowPos', W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int,
    C.c_int, C.c_int, W.UINT)
api('PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
api('GetSystemMetrics', C.c_int, C.c_int)

HWND_TOPMOST = C.c_void_p(-1)


def monitor_rect(x, y):
    """(left, top, width, height) of the monitor containing the point."""
    point = W.POINT(int(x), int(y))
    monitor = U.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = C.sizeof(info)
    if monitor and GetMonitorInfoW(monitor, C.byref(info)):
        r = info.rcMonitor
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return (0, 0, U.GetSystemMetrics(0), U.GetSystemMetrics(1))


def draw_map(width, height, x, y):
    """Map touchpad coordinates onto the annotation rectangle."""
    return x * width / pad_info.max_x(), y * height / pad_info.max_y()


def click_through(hwnd):
    style = U.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    U.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                        style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                        | WS_EX_TOOLWINDOW)
    return hwnd


class DrawWindow:
    kind = 'draw'

    def __init__(self, root, safe_preview=False, on_quit=None,
                 trigger_enabled=True):
        self.root = root
        self.safe_preview = safe_preview
        self.on_quit = on_quit
        self.events = queue.Queue()
        self.trigger_enabled = trigger_enabled
        self.auto_pending = False
        self.ui = ui_scale()
        self.ink_w = max(4.0, 4.5 * self.ui)
        self.halo_w = self.ink_w + 2.0 * self.ui

        self.ready = self.visible = self.closing = False
        self.alt_held = self.alt_blocked = False
        self.alt_since = 0.0
        self.esc_down = False
        self.state = 'idle'
        self.expect = None
        self.target = None
        self.rect = (0, 0, 0, 0)
        self.session = None
        self.last_path = ''
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active = {}
        self.stroke_items = {}
        self.self_check_failed = False
        self.hud = None

        root.withdraw()
        self.hwnd = click_through(apply_overlay_style(root))
        self.canvas = tk.Canvas(root, width=640, height=400, bg=KEY,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)
        root.geometry('640x400+-4000+-4000')
        root.report_callback_exception = self.on_error

        self.worker = LiveInput(self.events, self.hwnd,
                                allow_capture=not safe_preview,
                                fallback_hotkey=not safe_preview,
                                start_hotkey=False,
                                alt_releases=False)
        self.worker.start()
        # An Alt already held at startup must not open the layer by itself.
        self.alt_held = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        self.alt_blocked = self.alt_held
        root.after(16, self.tick)

    # ---------- window ----------
    def show(self, rect):
        left, top, width, height = rect
        self.rect = tuple(int(v) for v in rect)
        self.clear_ink()
        self.root.deiconify()
        self.root.wm_attributes('-transparentcolor', KEY)
        self.root.wm_attributes('-topmost', True)
        self.root.update_idletasks()
        self.canvas.configure(width=width, height=height)
        U.SetWindowPos(self.hwnd, HWND_TOPMOST, left, top, width, height,
                       SWP_NOACTIVATE)
        self.root.lift()
        self.visible = True

    def hide(self):
        self.root.withdraw()
        self.visible = False

    def map(self, x, y):
        _, _, width, height = self.rect
        return draw_map(width, height, x, y)

    def clear_ink(self):
        self.canvas.delete('halo')
        self.canvas.delete('ink')
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active.clear()
        self.stroke_items.clear()

    def clear_annotation(self):
        """Tray action / Esc while idle: drop the ink and hide the layer."""
        if self.state == 'capturing':
            self.expect = 'cancel'
            self.worker.request('release')
        self.clear_ink()
        self.hide()
        self.state = 'idle'
        self.expect = None
        self.target = None
        self.worker.target = None

    # ---------- Alt hold ----------
    def poll_alt(self, now=None):
        """Alt down clears and opens; Alt up stops and keeps the ink."""
        if not self.ready or self.safe_preview:
            return
        now = time.monotonic() if now is None else now
        down = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        if down and not self.alt_held:
            self.alt_held = True
            self.alt_since = now
            self.alt_blocked = any(
                U.GetAsyncKeyState(key) & 0x8000
                for key in (0x11, 0x10, 0x5B, 0x5C, 0x09, 0x20, 0x73))
            if self.state != 'idle':
                self.alt_blocked = True
            self.show_status('准备写字')
            return
        if not down:
            self.auto_pending = False
            if self.alt_held:
                self.alt_held = False
                if self.state == 'capturing':
                    self.expect = 'cancel'
                    self.worker.request('release')
            return
        if self.auto_pending:
            # The host auto-selector chose this view for the current press.
            self.auto_pending = False
            if self.state == 'idle':
                self.alt_blocked = True
                self.start()
            return
        if not self.trigger_enabled:
            return
        if self.alt_blocked or now - self.alt_since < 0.15:
            return
        if self.state != 'idle':
            return
        if any(U.GetAsyncKeyState(key) & 0x8000
               for key in (0x09, 0x20, 0x73)):
            self.alt_blocked = True
            return
        self.alt_blocked = True
        self.start()

    def show_status(self, state, seconds=1.8):
        title = '全域画图'
        lines = ['状态：%s' % state,
                 '按住 Alt 写 · 松开保留',
                 'Esc 清除 · Ctrl+F8 退出']
        try:
            if self.hud is None:
                self.hud = StatusHud(self.root)
            self.hud.show(title, lines, seconds)
        except BaseException:
            traceback.print_exc()

    # ---------- capture flow ----------
    def start(self):
        if not self.ready or self.safe_preview:
            return
        foreground = U.GetForegroundWindow()
        if not foreground or foreground == self.hwnd or U.IsIconic(foreground):
            return
        point = W.POINT()
        U.GetCursorPos(C.byref(point))
        rect = monitor_rect(point.x, point.y)
        self.target = foreground
        self.worker.target = foreground
        self.show(rect)
        U.SetForegroundWindow(foreground)
        self.state = 'starting'
        self.expect = 'start'
        self.worker.request('start')

    def cancel(self):
        if self.state == 'capturing':
            self.expect = 'cancel'
            self.worker.request('release')
        else:
            self.clear_annotation()

    def draw(self, frame):
        segments = self.tracker.update(frame)
        self.track_strokes(frame, segments)
        for cid, previous, point in segments:
            px, py = self.map(*point)
            state = self.stroke_items.get(cid)
            if previous is None or state is None:
                # One item per stroke keeps the thick white halo from
                # cutting dash-like gaps into the neighbouring segments.
                self.canvas.create_oval(px - self.halo_w / 2,
                                        py - self.halo_w / 2,
                                        px + self.halo_w / 2,
                                        py + self.halo_w / 2,
                                        fill=HALO, outline='', tags='halo')
                self.canvas.create_oval(px - self.ink_w / 2,
                                        py - self.ink_w / 2,
                                        px + self.ink_w / 2,
                                        py + self.ink_w / 2,
                                        fill=BASE_INK, outline='', tags='ink')
                self.stroke_items[cid] = {'points': [(px, py)]}
                continue
            state['points'].append((px, py))
            coords = [value for pair in state['points'] for value in pair]
            if 'halo' not in state:
                state['halo'] = self.canvas.create_line(
                    *coords, fill=HALO, width=self.halo_w, capstyle='round',
                    joinstyle='round', tags='halo')
                state['ink'] = self.canvas.create_line(
                    *coords, fill=BASE_INK, width=self.ink_w,
                    capstyle='round', joinstyle='round', tags='ink')
            else:
                self.canvas.coords(state['halo'], *coords)
                self.canvas.coords(state['ink'], *coords)
                self.canvas.tag_raise(state['ink'])
        for cid in list(self.stroke_items):
            if cid not in self.tracker.active:
                del self.stroke_items[cid]

    def draw_strokes(self, strokes):
        """Replay: all halos first, then ink, so joins never turn dotted."""
        lines = []
        dots = []
        for stroke in strokes:
            points = [self.map(x, y) for x, y in stroke]
            if not points:
                continue
            dots.append(points[0])
            if len(points) > 1:
                coords = [value for pair in points for value in pair]
                self.canvas.create_line(*coords, fill=HALO, width=self.halo_w,
                                        capstyle='round', joinstyle='round',
                                        tags='halo')
                lines.append(coords)
        for px, py in dots:
            self.canvas.create_oval(px - self.halo_w / 2, py - self.halo_w / 2,
                                    px + self.halo_w / 2, py + self.halo_w / 2,
                                    fill=HALO, outline='', tags='halo')
        for coords in lines:
            self.canvas.create_line(*coords, fill=BASE_INK, width=self.ink_w,
                                    capstyle='round', joinstyle='round',
                                    tags='ink')
        for px, py in dots:
            self.canvas.create_oval(px - self.ink_w / 2, py - self.ink_w / 2,
                                    px + self.ink_w / 2, py + self.ink_w / 2,
                                    fill=BASE_INK, outline='', tags='ink')

    def track_strokes(self, frame, segments):
        for cid, previous, point in segments:
            if previous is None or cid not in self.recording_active:
                stroke = {'contact': cid,
                          'points': [(frame['t'], point[0], point[1])]}
                self.recording_strokes.append(stroke)
                self.recording_active[cid] = stroke
            else:
                self.recording_active[cid]['points'].append(
                    (frame['t'], point[0], point[1]))
        for cid in list(self.recording_active):
            if cid not in self.tracker.active:
                del self.recording_active[cid]

    # ---------- worker events ----------
    def event(self, kind, data):
        if kind == 'ready':
            self.ready = True
        elif kind == 'quit':
            if self.on_quit:
                self.on_quit()
            else:
                self.close()
        elif kind == 'started':
            if not self.visible or self.state != 'starting':
                self.worker.request('release')
                return
            self.session = Session(data['path'], prefix='draw')
            self.last_path = str(self.session.base.with_suffix('.jsonl'))
            self.clear_ink()
            self.state = 'capturing'
            self.expect = 'cancel'
            if not (U.GetAsyncKeyState(0x12) & 0x8000):
                # A very short Alt tap can end before the hooks finished.
                self.worker.request('release')
        elif kind == 'frame':
            if self.session and not self.session.closed:
                self.session.write(data)
                if self.state == 'capturing':
                    self.draw(data)
        elif kind == 'stopped':
            self.recording_strokes = []
            self.recording_active.clear()
            self.tracker.active.clear()
            if self.session:
                self.session.close(data)
                self.session = None
            self.state = 'idle'
            self.expect = None
            target = self.target
            self.target = None
            self.worker.target = None
            if target and U.IsWindow(target):
                # Alt can leave the underlying app in menu/keytip mode.
                U.PostMessageW(target, 0x001F, 0, 0)  # WM_CANCELMODE
            if 'Esc' in str(data):
                self.clear_ink()
                self.hide()
            elif not self.canvas.find_withtag('ink'):
                self.hide()  # nothing was written
        elif kind in ('notice', 'error'):
            print('draw mode: %s' % data, flush=True)
            if kind == 'error':
                self.ready = False
            if self.state == 'starting':
                self.state = 'idle'
                if not self.canvas.find_withtag('ink'):
                    self.hide()
            elif kind == 'error':
                self.clear_annotation()

    # ---------- loop ----------
    def tick(self):
        if self.closing:
            return
        self.worker.guard.heartbeat = time.monotonic()
        try:
            for _ in range(400):
                try:
                    kind, data = self.events.get_nowait()
                except queue.Empty:
                    break
                self.event(kind, data)
            self.poll_alt()
            if self.visible and not self.safe_preview:
                down = bool(U.GetAsyncKeyState(0x1B) & 0x8000)
                if down and not self.esc_down:
                    self.cancel()
                self.esc_down = down
        except BaseException:
            self.on_error(*sys.exc_info())
        self.root.after(16, self.tick)

    def on_error(self, exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        if not self.closing:
            self.clear_annotation()

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.worker:
            self.worker.stopping.set()
            self.worker.join(timeout=3)
        try:
            if self.hud is not None:
                self.hud.close()
        except BaseException:
            pass
        try:
            if self.session:
                self.session.close('画图层关闭')
        except BaseException:
            traceback.print_exc()
        finally:
            self.root.destroy()

    # ---------- safe verification ----------
    def demo(self, strokes, rect=None):
        rect = rect or (0, 0, U.GetSystemMetrics(0), U.GetSystemMetrics(1))
        self.show(rect)
        self.state = 'idle'
        self.draw_strokes(strokes)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='non-activating full-screen replay; no capture hooks')
    args = parser.parse_args()
    enable_dpi_awareness()
    pad_info.configure()
    root = tk.Tk()
    app = DrawWindow(root, safe_preview=args.self_check)
    if args.self_check:
        from test_draw_window import visual_check
        root.after(900, lambda: visual_check(app))
    try:
        root.mainloop()
        if app.self_check_failed:
            raise RuntimeError('Safe visual verification failed')
    finally:
        if not app.closing:
            app.close()


if __name__ == '__main__':
    main()
