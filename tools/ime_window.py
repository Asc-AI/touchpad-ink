"""Compact Microsoft IME-like touchpad handwriting overlay.

Click a text field, hold Alt: a small transparent pad appears at the caret.
Keep holding Alt while writing on the touchpad. A short pause recognizes the
finished strokes right away, so candidates appear without releasing Alt; a
new stroke clears them again. Release Alt to choose: click a candidate or
press its number. Esc cancels. Ctrl+F8 quits (Ctrl+Shift+F8 if Ctrl+F8 is
taken).

Capture starts on Alt down, before the hold gate and before the slow caret
lookup, so the first stroke is never lost; the pad itself is shown once the
press proves to be a hold (or as soon as ink starts). A tap therefore arms
nothing visible and is dropped quietly.

The pad background is a Tk color key, so only ink and the candidate strip are
painted. The strip has Win11 rounded corners: four transparent corner masks
(``hud.round_card_corners``) cover the square frame corners and redraw the
arcs, and every candidate highlight is a rounded rectangle on one canvas. A
resting pause in 连写 recognizes too, without ending the session. The window
never activates (WS_EX_NOACTIVATE), so the target field keeps focus and the
caret does not jump.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import traceback

import auto_mode
import hud
import pad_info
import windows_ink
from gesture import START_GATE, DoublePress, TapDetector
from live_input import LiveInput, StrokeTracker, U, ime_composing, input_log
from live_window import Session

BASE_INK = '#1e3350'
HALO = '#ffffff'
KEY = '#ff00ff'
BAR_BG = '#f6f6f6'
BAR_EDGE = '#cfcfcf'
BAR_TEXT = '#1f1f1f'
BAR_MUTED = '#7b7b7b'
BAR_SEL_BG = '#dce8fb'
BAR_SEL_TEXT = '#0b4ea2'
BAR_HOVER = '#eceff4'
LIVE_REFRESH = 0.12  # min gap between live recognitions while ink changes (~8/s)

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


def api(name, restype, *args):
    fn = getattr(U, name)
    fn.restype, fn.argtypes = restype, list(args)
    return fn


class GUITHREADINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                ('hwndActive', W.HWND), ('hwndFocus', W.HWND),
                ('hwndCapture', W.HWND), ('hwndMenuOwner', W.HWND),
                ('hwndMoveSize', W.HWND), ('hwndCaret', W.HWND),
                ('rcCaret', W.RECT)]


class MONITORINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT),
                ('rcWork', W.RECT), ('dwFlags', W.DWORD)]


ULONG_PTR = C.c_size_t


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD),
                ('time', W.DWORD), ('dwExtraInfo', ULONG_PTR)]


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD),
                ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [('uMsg', W.DWORD), ('wParamL', W.WORD), ('wParamH', W.WORD)]


class _INPUTUNION(C.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]


class INPUT(C.Structure):
    _fields_ = [('type', W.DWORD), ('u', _INPUTUNION)]


GetGUIThreadInfo = C.WINFUNCTYPE(
    W.BOOL, W.DWORD, C.POINTER(GUITHREADINFO))(('GetGUIThreadInfo', U))
GetWindowThreadProcessId = C.WINFUNCTYPE(
    W.DWORD, W.HWND, C.POINTER(W.DWORD))(('GetWindowThreadProcessId', U))
api('ClientToScreen', W.BOOL, W.HWND, C.POINTER(W.POINT))
api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))
api('MonitorFromPoint', W.HANDLE, W.POINT, W.DWORD)
GetMonitorInfoW = C.WINFUNCTYPE(
    W.BOOL, W.HANDLE, C.POINTER(MONITORINFO))(('GetMonitorInfoW', U))
api('SendInput', W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)
api('PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
api('GetDpiForSystem', W.UINT)
api('SetProcessDpiAwarenessContext', W.BOOL, C.c_void_p)
api('GetWindowLongPtrW', C.c_ssize_t, W.HWND, C.c_int)
api('SetWindowLongPtrW', C.c_ssize_t, W.HWND, C.c_int, C.c_ssize_t)
api('GetSystemMetrics', C.c_int, C.c_int)


def enable_dpi_awareness():
    """Physical pixel coordinates keep the caret and Tk geometry aligned."""
    try:
        if U.SetProcessDpiAwarenessContext(C.c_void_p(-4)):  # per-monitor v2
            return
    except Exception:
        pass
    try:
        C.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def apply_overlay_style(root):
    """Borderless, topmost, color-key transparent and never activated."""
    root.overrideredirect(True)
    root.configure(bg=KEY)
    root.wm_attributes('-transparentcolor', KEY)
    root.wm_attributes('-topmost', True)
    root.update_idletasks()
    hwnd = U.GetAncestor(root.winfo_id(), GA_ROOT)
    style = U.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    U.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                        style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    return hwnd


def ui_scale():
    try:
        return max(1.0, U.GetDpiForSystem() / 96)
    except (AttributeError, OSError):
        return 1.0


def work_area(x, y):
    point = W.POINT(int(x), int(y))
    monitor = U.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = C.sizeof(info)
    if monitor and GetMonitorInfoW(monitor, C.byref(info)):
        r = info.rcWork
        return (r.left, r.top, r.right, r.bottom)
    return (0, 0, U.GetSystemMetrics(0), U.GetSystemMetrics(1))


def caret_point(target, fallback_height):
    """Return (x, y, caret_height) at the target's caret, else the cursor.

    Chromium/Electron editors (browsers, Qoder, many chat boxes) expose no
    Win32 caret, so a cursor fallback would leave the pad behind as typing
    advances the real caret. UI Automation's text pattern carries the caret
    rectangle there; the cursor stays as the last resort.
    """
    gti = GUITHREADINFO()
    gti.cbSize = C.sizeof(gti)
    if (GetGUIThreadInfo(0, C.byref(gti)) and gti.hwndCaret
            and U.IsWindow(gti.hwndCaret) and U.GetAncestor(gti.hwndCaret, GA_ROOT) == target):
        r = gti.rcCaret
        height = r.bottom - r.top
        if r.right > r.left and height > 0:
            point = W.POINT(r.left, r.bottom)
            if U.ClientToScreen(gti.hwndCaret, C.byref(point)):
                return point.x, point.y, height
    try:
        bounds = auto_mode.caret_bounds(target)
    except BaseException:
        bounds = None
    if bounds:
        x, y, height = bounds
        return int(x), int(y), max(1, min(int(height), 400))
    point = W.POINT()
    U.GetCursorPos(C.byref(point))
    return point.x, point.y, fallback_height


def place_window(caret_x, caret_y, caret_h, width, height, work, gap=10, margin=4):
    """Place the pad below the caret; flip above or clamp at screen edges."""
    left, top, right, bottom = work
    max_x = max(left + margin, right - width - margin)
    x = min(max(caret_x - width // 5, left + margin), max_x)
    y = caret_y + gap
    if y + height > bottom - margin:
        y = caret_y - max(caret_h, 1) - height - gap
    max_y = max(top + margin, bottom - height - margin)
    return int(x), int(min(max(y, top + margin), max_y))


def candidate_entries(payload, limit=7):
    """Candidate strings; later results keep their top text as a suffix."""
    results = payload.get('results') or []
    if not results:
        return []
    tail = ''.join(result.get('top') or '' for result in results[1:])
    out = []
    for candidate in results[0].get('candidates') or []:
        value = candidate + tail
        if value and value not in out:
            out.append(value)
    return out[:limit]


def unicode_inputs(text):
    """KEYEVENTF_UNICODE scan codes, one key-down/up pair per UTF-16 unit."""
    data = text.encode('utf-16-le')
    events = []
    for index in range(0, len(data), 2):
        unit = int.from_bytes(data[index:index + 2], 'little')
        events.append((unit, KEYEVENTF_UNICODE))
        events.append((unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def send_unicode(text):
    events = unicode_inputs(text)
    if not events:
        return True
    batch = (INPUT * len(events))()
    for index, (scan, flags) in enumerate(events):
        batch[index].type = INPUT_KEYBOARD
        batch[index].u.ki = KEYBDINPUT(0, scan, flags, 0, 0)
    return U.SendInput(len(events), batch, C.sizeof(INPUT)) == len(events)


def describe_window(hwnd, probe_uia=False):
    """One-line focus/IME diagnostics for candidate insertion; never raises.

    The browser failures ("pad shows, chosen text never appears") are not
    reproducible from the injection path alone: SendInput Unicode lands in a
    Chromium textarea. What differs at the user's desk is which window, field
    and IME state own the keys, so log exactly that around every insertion.
    """
    try:
        if not hwnd or not U.IsWindow(hwnd):
            return 'hwnd=%s gone' % hwnd
        pid = W.DWORD()
        thread = GetWindowThreadProcessId(hwnd, C.byref(pid))
        parts = ['hwnd=%s pid=%s %s/%s' % (
            hwnd, pid.value, auto_mode.process_name(hwnd),
            auto_mode.window_class(hwnd))]
        if thread:
            info = GUITHREADINFO()
            info.cbSize = C.sizeof(info)
            if GetGUIThreadInfo(thread, C.byref(info)):
                parts.append('focus=%s/%s' % (
                    info.hwndFocus,
                    auto_mode.window_class(info.hwndFocus)))
        parts.append('composing=%s' % bool(ime_composing(hwnd)))
        if probe_uia:
            parts.append('uia=%s' % auto_mode.UIA_LABELS.get(
                auto_mode.focus_control_type(), '?'))
        return ' '.join(parts)
    except BaseException:
        return 'hwnd=%s <diagnostics failed>' % hwnd


# Alt release in a normal browser window moves the keyboard focus to the
# toolbar (measured: UIA focus becomes Button), so chosen text went nowhere
# while the window still looked active. The field's element is captured on
# Alt down; it is focused again and verified before typing.
FOCUS_FOCUSED = 'focused'    # an editable element in the target already has it
FOCUS_RESTORED = 'restored'  # the saved field took the focus back
FOCUS_LOST = 'lost'          # focus is elsewhere and would not return
FOCUS_UNKNOWN = 'unknown'    # nothing saved (QQ: Group); leave the app alone
FOCUS_SETTLE = 0.05          # let the browser apply the focus before typing


def focus_ready(target, saved):
    """Make sure injected text can reach the target's field.

    The saved element is only used when focus is currently somewhere else;
    an editable focus in the target process is the best signal (the user may
    have moved to another field). ``FOCUS_UNKNOWN`` keeps the old behaviour
    for apps that expose no editable element yet accept the keys.
    """
    if not target:
        return FOCUS_LOST
    current = auto_mode.focused_editable(auto_mode.process_id(target))
    if current is not None:
        auto_mode.release_element(current)
        return FOCUS_FOCUSED
    if saved is None:
        return FOCUS_UNKNOWN
    if not auto_mode.set_focus(saved):
        return FOCUS_LOST
    time.sleep(FOCUS_SETTLE)
    if auto_mode.focus_control_type() in auto_mode.EDITABLE_TYPES:
        return FOCUS_RESTORED
    return FOCUS_LOST


def should_recognize(reason, expect):
    """Alt release ends the round; Esc, focus loss and safety stops hide.

    ``keep`` is the real-time variant: candidates were already recognized
    while Alt was held, so the stop keeps them instead of re-running.
    """
    if not reason or expect == 'cancel':
        return False
    if 'Alt' in reason:
        return True
    if any(word in reason for word in
           ('Esc', '失去焦点', '超时', '90 秒', '10 分钟', '窗口关闭', '积压', '异常')):
        return False
    return expect in ('start', 'recognize', 'keep')


class ImeWindow:
    kind = 'typing'

    def __init__(self, root, safe_preview=False, on_quit=None,
                 trigger_enabled=True):
        self.root = root
        self.safe_preview = safe_preview
        self.on_quit = on_quit
        self.events = queue.Queue()
        self.trigger_enabled = trigger_enabled
        self.auto_pending = False
        self.ui = ui_scale()
        self.width = int(304 * self.ui)
        self.pad_h = int(190 * self.ui)
        self.bar_h = int(44 * self.ui)
        self.height = self.pad_h + self.bar_h
        inset = int(12 * self.ui)
        self.pad_w, self.pad_h_px = pad_info.max_x(), pad_info.max_y()
        self.ink_scale = min((self.width - 2 * inset) / self.pad_w,
                             (self.pad_h - 2 * inset) / self.pad_h_px)
        self.ox = (self.width - self.ink_scale * self.pad_w) / 2
        self.oy = (self.pad_h - self.ink_scale * self.pad_h_px) / 2
        self.ink_w = max(2.0, 3.0 * self.ui)
        self.halo_w = self.ink_w + 2.0 * self.ui

        self.ready = self.visible = self.closing = False
        self.alt_held = self.alt_blocked = False
        self.alt_since = 0.0
        self.state = 'idle'
        self.expect = None
        self.target = None
        # AddRef'd UIA element of the target's text field, saved while Alt is
        # still down (Alt release moves browser focus to the toolbar).
        self.focus_element = None
        self.pos = (0, 0)
        self.session = None
        self.last_path = ''
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active = {}
        self.last_strokes = []
        self.recognition_token = 0
        self.candidate_texts = []
        self.esc_down = False
        self.self_check_failed = False
        self.hide_token = 0
        # Prearm: capture starts on Alt down so no first stroke is lost; the
        # pad is shown after the hold gate (or as soon as ink begins).
        self.prearmed = False
        self.show_due = False
        # Live recognition: every new point schedules a recognition, throttled
        # to LIVE_REFRESH. No pause detection; results refresh in place.
        self.live_pending = False
        self.live_ready = False
        self.live_version = -1
        self.last_live_wall = 0.0
        self.stroke_version = 0
        self.ink_version = 0
        self.cand_hits = []
        self.cand_bg_items = []
        # 连写: capture stays armed without Alt; a pause recognizes, one Alt
        # tap clears the round, two Alt taps leave the mode.
        self.continuous = False
        self.taps = TapDetector()
        self.exit_press = DoublePress()  # 连写 exits on two Alt downs

        root.withdraw()
        self.hwnd = apply_overlay_style(root)
        root.geometry(f'{self.width}x{self.height}+-4000+-4000')
        root.report_callback_exception = self.on_error

        self.canvas = tk.Canvas(root, width=self.width, height=self.pad_h,
                                bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill='x', side='top')
        # The bar keeps its own background; four transparent corner masks sit
        # above every child and redraw the Win11 arcs, so neither the frame nor
        # the opaque candidate canvas can show a right angle at the corners.
        # The bar itself has no Tk highlight border: that border is drawn
        # outside the children's area, so a corner mask could never cover it
        # and a 1px square corner stayed visible (the "black corner"). The
        # straight edges get their own 1px strips instead, under the masks.
        self.bar_radius = max(4, int(8 * self.ui))
        self.bar = tk.Frame(root, width=self.width, height=self.bar_h,
                            bg=BAR_BG, highlightthickness=0, bd=0)
        self.bar.pack(fill='x', side='top')
        self.bar.pack_propagate(False)
        self.close_btn = tk.Label(self.bar, text='×', bg=BAR_BG, fg='#8a8a8a',
                                  font=('Microsoft YaHei UI', 12), padx=int(10 * self.ui))
        self.close_btn.pack(side='right', fill='y')
        self.close_btn.bind('<ButtonRelease-1>', lambda e: self.cancel())
        self.close_btn.bind('<Enter>', lambda e: self.close_btn.config(fg='#c0392b'))
        self.close_btn.bind('<Leave>', lambda e: self.close_btn.config(fg='#8a8a8a'))
        self.info = tk.Label(self.bar, text='', bg=BAR_BG, fg=BAR_MUTED,
                             font=('Microsoft YaHei UI', 9), anchor='w')
        self.info.pack(side='left', padx=(int(12 * self.ui), int(4 * self.ui)))
        # One canvas draws every candidate: rounded Win11 highlight, digit and
        # glyph. A Frame per candidate left the selected cell a square block.
        self.cand_font = tkfont.Font(family='Microsoft YaHei UI', size=12)
        self.cand_digit_font = tkfont.Font(family='Microsoft YaHei UI', size=7)
        self.cand_frame = tk.Canvas(self.bar, bg=BAR_BG, highlightthickness=0,
                                    bd=0, width=0, height=self.bar_h)
        self.cand_frame.pack(side='left', fill='y')
        self.cand_frame.bind('<ButtonRelease-1>', self.on_candidate_click)
        self.cand_frame.bind('<Motion>', self.on_candidate_motion)
        self.cand_frame.bind('<Leave>', lambda e: self.paint_candidates(-1))
        # The 1px straight edges must sit above the candidate canvas and the
        # close button (both paint BAR_BG over the whole bar height), but
        # below the corner masks that cut the arcs.
        edge = max(1, int(self.ui))
        self.bar_edges = []
        for kwargs in (
                dict(x=self.bar_radius, y=0, relwidth=1,
                     width=-2 * self.bar_radius, height=edge),
                dict(x=self.bar_radius, rely=1, y=-edge, relwidth=1,
                     width=-2 * self.bar_radius, height=edge),
                dict(x=0, y=self.bar_radius, width=edge, relheight=1,
                     height=-2 * self.bar_radius),
                dict(relx=1, x=-edge, y=self.bar_radius, width=edge,
                     relheight=1, height=-2 * self.bar_radius)):
            strip = tk.Frame(self.bar, bg=BAR_EDGE, bd=0,
                             highlightthickness=0)
            strip.place(**kwargs)
            self.bar_edges.append(strip)
        self.bar_masks = hud.round_card_corners(
            self.bar, self.bar_radius, BAR_BG, BAR_EDGE, KEY)

        # Match main() for window created before DPI setup only.
        root.update_idletasks()
        self.worker = LiveInput(self.events, self.hwnd,
                                allow_capture=not safe_preview,
                                fallback_hotkey=not safe_preview,
                                start_hotkey=False,
                                alt_releases=False)
        self.worker.start()
        # An Alt already held at startup is not a trigger; require a fresh press.
        self.alt_held = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        self.alt_blocked = self.alt_held
        root.after(16, self.tick)

    # ---------- window ----------
    def show(self, x, y):
        self.hide_token += 1
        self.pos = (int(x), int(y))
        self.root.geometry(f'{self.width}x{self.height}+{int(x)}+{int(y)}')
        self.root.deiconify()
        self.root.wm_attributes('-transparentcolor', KEY)
        self.root.wm_attributes('-topmost', True)
        self.root.lift()
        self.visible = True
        self.root.after(10, self.refocus_target)

    def refocus_target(self):
        if (self.visible and self.target and U.IsWindow(self.target)
                and U.GetForegroundWindow() != self.target):
            U.SetForegroundWindow(self.target)

    def hide(self):
        self.hide_token += 1
        self.continuous = False
        self.prearmed = False
        self.show_due = False
        self.live_pending = self.live_ready = False
        self.live_version = -1
        self.taps.reset()
        self.exit_press.reset()
        if self.worker:
            self.worker.continuous = False
            self.worker.free_hold = False
            self.worker.request('select_off')
            if self.worker.guard.active:
                self.worker.request('release')
        self.root.withdraw()
        self.visible = False
        self.state = 'idle'
        self.expect = None
        self.recognition_token += 1
        self.clear_ink()
        self.clear_candidates()
        self.info.config(text='')
        self.drop_focus_element()
        self.target = None
        if self.worker:
            self.worker.target = None

    def schedule_hide(self, delay=1800):
        """Auto-hide unless a newer show/hide happened in the meantime."""
        token = self.hide_token

        def run():
            if token == self.hide_token:
                self.hide()

        self.root.after(delay, run)

    def flash(self, message):
        """Reuse the bar for a short clipboard/fallback notice."""
        x, y = self.pos
        self.show(x, y)
        self.info.config(text=message)
        self.schedule_hide()

    # ---------- Alt hold / taps ----------
    def poll_alt(self, now=None):
        """Alt down starts a fresh capture; Alt up ends it and recognizes."""
        if not self.ready or self.safe_preview:
            return
        now = time.monotonic() if now is None else now
        down = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        gesture = self.taps.update(down, now)
        exit_press = self.exit_press.update(down, now)
        if self.continuous:
            self.alt_held = down
            # In auto mode the host owns the double tap (it entered the mode);
            # manual mode has no host loop, so the view exits by itself.
            # Two Alt downs count even when a press is held a little longer.
            if (exit_press == 'double'
                    or gesture == 'double') and self.trigger_enabled:
                self.exit_continuous()
            elif gesture == 'tap':
                self.new_round()
            return
        if gesture == 'double' and self.trigger_enabled:
            self.alt_held = False
            self.enter_continuous()
            return
        if down and not self.alt_held:
            self.alt_held = True
            self.alt_since = now
            self.alt_blocked = any(
                U.GetAsyncKeyState(key) & 0x8000
                for key in (0x11, 0x10, 0x5B, 0x5C, 0x09, 0x20, 0x73))
            if self.state not in ('idle', 'choosing'):
                self.alt_blocked = True
            if self.trigger_enabled:
                if not self.alt_blocked and self.state == 'idle':
                    # Hooks go in now; the pad waits for the hold gate, so
                    # writing immediately after Alt never loses a stroke.
                    self.prearm()
            return
        if not down:
            self.auto_pending = False
            if self.alt_held:
                self.alt_held = False
                if self.prearmed and not self.visible:
                    self.abort_prearm()  # tap before the gate: no bar
                elif self.state == 'capturing':
                    # Keep the live answer only if it already covers every
                    # point; continuous refresh often leaves the last 0.12 s
                    # unread, and a final pass is only ~8 ms.
                    if (self.live_ready and self.candidate_texts
                            and self.live_version == self.ink_version):
                        self.expect = 'keep'
                    else:
                        self.expect = 'recognize'
                    self.worker.request('release')
                    # Chromium may move focus to its toolbar on Alt key-up,
                    # sometimes after the first result is drawn. Retry once
                    # the bar is up (a no-op when the caret never moved).
                    self.root.after(220, self.regain_caret)
            return
        if self.auto_pending:
            # The host auto-selector chose this view for the current press.
            self.auto_pending = False
            if self.state in ('idle', 'choosing'):
                self.alt_blocked = True
                self.start()
            return
        if not self.trigger_enabled:
            return
        if self.alt_blocked:
            return
        # Alt+Tab / Alt+Space / Alt+F4 during the hold kill this press.
        if any(U.GetAsyncKeyState(key) & 0x8000
               for key in (0x09, 0x20, 0x73)):
            self.alt_blocked = True
            if self.prearmed:
                self.abort_prearm()
            return
        if now - self.alt_since < START_GATE:  # filters taps / double taps
            return
        if self.prearmed and self.state in ('starting', 'capturing'):
            self.alt_blocked = True
            self.start()  # keep the pre-armed capture, just place the pad
            return
        if self.state not in ('idle', 'choosing'):
            return
        self.alt_blocked = True
        self.start()

    # ---------- continuous (连写) ----------
    def foreground_target(self):
        """The input window to write into, without any slow UIA/caret work."""
        foreground = U.GetForegroundWindow()
        if foreground == self.hwnd and self.target and U.IsWindow(self.target):
            foreground = self.target
        if not foreground or foreground == self.hwnd or U.IsIconic(foreground):
            return None
        return foreground

    def remember_focus(self):
        """Save the target's text field while it still owns the caret.

        Alt release in a normal browser window moves UI Automation focus to
        a toolbar button, so the element must be captured on Alt down (or as
        the round starts, still while Alt is held).
        """
        if not self.target:
            return
        element = auto_mode.focused_editable(auto_mode.process_id(self.target))
        if element is None:
            return
        old, self.focus_element = self.focus_element, element
        if old is not None:
            auto_mode.release_element(old)

    def drop_focus_element(self):
        element, self.focus_element = self.focus_element, None
        if element is not None:
            auto_mode.release_element(element)

    def regain_caret(self):
        """Put the caret back as soon as the candidates appear.

        The element saved on Alt down is focused again, so the blinking caret
        returns while the user picks instead of only at insertion time. In
        连写 Alt was not the trigger and focus stays; that path is skipped.
        """
        if self.continuous or not self.target or self.focus_element is None:
            return
        state = focus_ready(self.target, self.focus_element)
        if state == FOCUS_RESTORED:
            input_log('focus: caret restored after Alt [%s]'
                      % describe_window(self.target, probe_uia=True))
        elif state == FOCUS_LOST:
            input_log('focus: caret restore failed after Alt [%s]'
                      % describe_window(self.target, probe_uia=True))

    def locate_overlay(self, foreground=None):
        """Foreground input window and the overlay position under its caret."""
        foreground = foreground or self.foreground_target()
        if foreground is None:
            return None
        caret_x, caret_y, caret_h = caret_point(
            foreground, int(18 * self.ui))
        x, y = place_window(caret_x, caret_y, caret_h, self.width, self.height,
                            work_area(caret_x, caret_y), gap=int(10 * self.ui),
                            margin=int(4 * self.ui))
        return foreground, x, y

    def prearm(self):
        """Begin capturing on Alt down so the first stroke is never lost.

        Only the foreground handle is needed here; the caret lookup and the
        window placement stay on the normal start path. A press that ends
        before the hold gate is aborted quietly.
        """
        if self.prearmed or self.state != 'idle':
            return False
        target = self.foreground_target()
        if target is None:
            return False
        self.target = target
        self.worker.target = target
        self.remember_focus()
        self.recognition_token += 1
        self.clear_ink()
        self.clear_candidates()
        self.last_strokes = []
        self.live_ready = self.live_pending = False
        self.live_version = -1
        self.state = 'starting'
        self.expect = 'start'
        self.continuous = False
        self.prearmed = True
        self.begin_worker(start=True)
        return True

    def abort_prearm(self):
        """A tap ended before the hold gate: drop the hidden capture quietly."""
        self.prearmed = False
        self.show_due = False
        self.expect = 'cancel'
        self.state = 'idle'
        self.recognition_token += 1
        self.live_pending = self.live_ready = False
        self.worker.request('release')
        self.drop_focus_element()
        self.target = None
        self.worker.target = None

    def enter_continuous(self):
        if not self.ready or self.safe_preview or self.continuous:
            return False
        found = self.locate_overlay()
        if found is None:
            return False
        # A second Alt down may already have pre-armed the capture; keep its
        # hooks and session instead of asking the worker to start twice.
        live = (self.prearmed and self.state in ('starting', 'capturing')
                and self.target == found[0])
        self.prearmed = False
        self.continuous = True
        self.taps.reset()
        self.exit_press.reset()
        self.target = found[0]
        self.worker.target = self.target
        self.remember_focus()
        self.show(found[1], found[2])
        U.SetForegroundWindow(self.target)
        self.recognition_token += 1
        self.clear_ink()
        self.clear_candidates()
        self.last_strokes = []
        self.live_ready = self.live_pending = False
        self.live_version = -1
        if live:
            self.worker.continuous = True
            self.worker.free_hold = True
            self.worker.suppress = True
            if self.state == 'capturing':
                self.expect = 'recognize'
                self.worker.request('select_off')
            else:
                # The started event is still queued; let it land as usual.
                self.expect = 'start'
                self.begin_worker(start=False)
        else:
            self.state = 'starting'
            self.expect = 'start'
            self.begin_worker(start=True)
        self.show_idle_hint()
        return True

    def exit_continuous(self, message='已退出连写'):
        if not self.continuous:
            return
        self.continuous = False
        self.alt_held = False  # the release that follows an exit is not a stop
        self.taps.reset()
        self.exit_press.reset()
        self.worker.continuous = False
        self.worker.free_hold = False
        if self.info.cget('text') == '连写中':
            # Leaving 连写: the idle status no longer applies.
            self.info.config(text='')
        if self.state in ('capturing', 'starting'):
            self.expect = 'cancel'
            self.worker.request('release')
        elif self.state in ('choosing', 'recognizing'):
            self.worker.request('select_off')
            if self.visible:
                self.schedule_hide(2600)

    def new_round(self):
        """One Alt tap in 连写: drop this round and write again."""
        self.recognition_token += 1
        self.live_pending = self.live_ready = False
        self.live_version = -1
        self.clear_ink()
        self.clear_candidates()
        self.worker.request('select_off')
        if self.state in ('capturing', 'starting'):
            self.show_idle_hint()
        else:
            self.arm_continuous()

    def arm_continuous(self):
        if not self.continuous:
            return
        found = self.locate_overlay()
        if found is None:
            self.exit_continuous('连写目标已关闭')
            self.hide()
            return
        old_target = self.target
        self.target = found[0]
        self.worker.target = self.target
        self.remember_focus()
        self.show(found[1], found[2])
        self.clear_ink()
        self.clear_candidates()
        self.last_strokes = []
        self.live_ready = self.live_pending = False
        self.live_version = -1
        self.state = 'starting'
        self.expect = 'start'
        if self.worker.guard.active and old_target == self.target:
            # 连写 keeps its capture armed between rounds. Asking the worker
            # to 'start' again would call begin(), which sees an active guard
            # and releases it ("已停止并释放"), silently ending the mode right
            # after a candidate is picked. Keep the live capture instead.
            self.state = 'capturing'
            self.expect = 'recognize'
            self.worker.continuous = True
            self.worker.free_hold = True
            self.worker.suppress = True
            self.worker.request('select_off')
        else:
            self.begin_worker(start=True)
        self.show_idle_hint()

    def show_idle_hint(self):
        """Show the 连写 status only while the strip has nothing else in it.

        Routine hints were removed so candidates keep the whole width; this
        restores one terse status for the truly idle bar (right after entering
        连写, clearing a round or picking a candidate). The first new stroke
        clears it, so recognition results always get the full strip.
        """
        if (not self.visible or not self.continuous or self.candidate_texts
                or self.recording_strokes or self.tracker.active):
            return
        self.info.config(text='连写中')

    def begin_worker(self, start=False):
        self.worker.continuous = self.continuous
        self.worker.free_hold = self.continuous
        self.worker.suppress = True
        self.worker.request('select_off')
        if start:
            self.worker.request('start')

    def show_status(self, state, seconds=1.8):
        """Keep the bar clean: state hints are no longer printed.

        Every routine hint ("识别中…", "连写中 · 停笔识别", "准备写字") was
        taking room from the candidates and pushing the last ones under the
        close button, so the typing bar now only ever shows candidates (or a
        real error notice). The parameter is kept for callers.
        """
        return

    # ---------- recognition flow ----------
    def start(self):
        """Show the pad and make sure capture is armed.

        Capture is requested before the caret lookup so a stroke drawn right
        after Alt goes down is recorded even if UI Automation takes a while.
        A pre-armed capture keeps its ink, session and hooks; when the target
        changed during the gate it is dropped instead of writing elsewhere.
        """
        if not self.ready or self.safe_preview:
            return
        foreground = self.foreground_target()
        if foreground is None:
            if self.prearmed:
                self.abort_prearm()
            return
        keep = (self.prearmed and self.state in ('starting', 'capturing')
                and self.target == foreground)
        if self.prearmed and not keep:
            self.abort_prearm()
        self.target = foreground
        self.worker.target = foreground
        self.remember_focus()
        if not keep:
            self.recognition_token += 1  # ignore a late result from the last capture
            self.clear_ink()
            self.clear_candidates()
            self.last_strokes = []
            self.live_ready = self.live_pending = False
            self.live_version = -1
            self.info.config(text='')
            self.state = 'starting'
            self.expect = 'start'
            self.continuous = False
            self.begin_worker(start=True)
        self.prearmed = False
        found = self.locate_overlay(foreground)
        if found is None:
            if self.state in ('starting', 'capturing'):
                self.expect = 'cancel'
                self.worker.request('release')
            return
        self.show(found[1], found[2])
        U.SetForegroundWindow(foreground)
        self.show_status('短时批注')

    def cancel(self):
        if self.state == 'capturing':
            self.expect = 'cancel'
            self.worker.request('release')
        else:
            self.hide()

    def clear_ink(self):
        self.canvas.delete('halo')
        self.canvas.delete('ink')
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active.clear()
        self.stroke_version += 1

    def clear_candidates(self):
        self.candidate_texts = []
        self.cand_hits = []
        self.cand_bg_items = []
        self._cand_hover = -1
        self.cand_frame.delete('all')
        self.cand_frame.config(width=0)

    def map(self, x, y):
        return self.ox + x * self.ink_scale, self.oy + y * self.ink_scale

    def draw(self, frame):
        segments = self.tracker.update(frame)
        self.track_strokes(frame, segments)
        for cid, previous, point in segments:
            px, py = self.map(*point)
            if previous is None:
                self.canvas.create_oval(px - self.ink_w / 2, py - self.ink_w / 2,
                                        px + self.ink_w / 2, py + self.ink_w / 2,
                                        fill=BASE_INK, outline='', tags='ink')
            else:
                x0, y0 = self.map(*previous)
                self.canvas.create_line(x0, y0, px, py, fill=HALO,
                                        width=self.halo_w, capstyle='round',
                                        tags='halo')
                self.canvas.create_line(x0, y0, px, py, fill=BASE_INK,
                                        width=self.ink_w, capstyle='round',
                                        tags='ink')
        if len(self.canvas.find_withtag('ink')) > 9000:
            self.trim_ink()

    def trim_ink(self):
        """Bound display items only; halo and ink are trimmed in matched sets.

        Trimming 'ink' alone let 'halo' grow without limit on long writing
        sessions (private memory kept climbing).
        """
        for tag in ('ink', 'halo'):
            items = self.canvas.find_withtag(tag)
            if len(items) > 4000:
                for item in items[:-4000]:
                    self.canvas.delete(item)

    def draw_strokes(self, strokes):
        for stroke in strokes:
            previous = None
            for x, y in stroke:
                point = self.map(x, y)
                if previous is not None:
                    self.canvas.create_line(previous[0], previous[1], point[0], point[1],
                                            fill=HALO, width=self.halo_w,
                                            capstyle='round', tags='halo')
                    self.canvas.create_line(previous[0], previous[1], point[0], point[1],
                                            fill=BASE_INK, width=self.ink_w,
                                            capstyle='round', tags='ink')
                previous = point

    def track_strokes(self, frame, segments):
        for cid, previous, point in segments:
            # Every new point makes the last recognition stale; the tick
            # throttles fresh requests while the finger is still moving.
            self.ink_version += 1
            if previous is None or cid not in self.recording_active:
                self.stroke_version += 1
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

    def finished_strokes(self):
        """Completed strokes as (x, y) lists; the live and final input."""
        return [[(point[1], point[2]) for point in stroke['points']]
                for stroke in self.recording_strokes if stroke['points']]

    def recognize(self, strokes):
        self.recognition_token += 1
        self.state = 'recognizing'
        self.expect = None
        self.clear_candidates()
        self.live_ready = False
        threading.Thread(target=self._recognize_worker,
                         args=(strokes, self.recognition_token),
                         daemon=True).start()

    def live_recognize(self):
        """Recognize the ink written so far, while the capture keeps running.

        No pause detection: every new point marks the ink stale and the tick
        starts a fresh request at most every LIVE_REFRESH seconds. One request
        is in flight at a time; its result only fills the bar.
        """
        strokes = self.finished_strokes()
        if not strokes or self.live_pending:
            return False
        self.live_pending = True
        self.live_ready = False
        self.live_version = self.ink_version
        self.last_live_wall = time.monotonic()
        self.recognition_token += 1
        token = self.recognition_token
        threading.Thread(target=self._recognize_worker,
                         args=(strokes, token), daemon=True).start()
        return True

    def _recognize_worker(self, strokes, token):
        try:
            payload = dict(windows_ink.recognize(strokes), token=token)
        except Exception as exc:
            payload = {'token': token, 'error': str(exc)}
        self.events.put(('recognized', payload))

    def show_recognition(self, data, live=False):
        candidates = candidate_entries(data)
        if live and candidates and candidates == self.candidate_texts:
            # Same answer for newer ink: 8 redraws per second would only
            # flicker. Keep the cells but re-arm digits so their 30 s window
            # keeps sliding and an IME-composing pause does not disarm them.
            self.live_ready = True
            self.worker.request(('select_on', len(candidates)))
            return
        self.clear_candidates()
        self.info.config(text='')
        if not live:
            self.state = 'choosing'
            self.live_ready = False
        if data.get('error'):
            if not live:
                self.info.config(text='识别不可用 · 原始数据已保存')
                self.worker.request('select_off')
            return
        if not candidates:
            if not live:
                self.worker.request('select_off')
            return
        self.candidate_texts = candidates
        self.draw_candidates(candidates)
        # Digits work as soon as candidates appear, whether Alt is still held
        # (live pause recognition) or already released. A new stroke sends
        # select_off again, so ordinary typing is not intercepted.
        self.worker.request(('select_on', len(candidates)))
        if live:
            # Capture stays armed: the user can keep writing, and a new
            # stroke clears these candidates.
            self.live_ready = True
        else:
            self.live_ready = False
            # Alt is up now; give the browser field its caret back before the
            # user picks, so the bar does not sit next to a dead caret.
            self.regain_caret()

    def draw_candidates(self, candidates):
        """Rounded Win11 cells: one canvas, one hit box per candidate."""
        height = max(self.bar_h - 2, int(40 * self.ui))
        radius = self.bar_radius
        pad = int(2 * self.ui)
        top = int(3 * self.ui)
        bottom = height - int(3 * self.ui)
        x = int(2 * self.ui)
        for index, value in enumerate(candidates):
            width = max(int(30 * self.ui),
                        self.cand_font.measure(value) + int(18 * self.ui))
            box = (x, top, x + width, bottom)
            items = hud.round_rect(self.cand_frame, box, radius,
                                   fill=BAR_SEL_BG if index == 0 else BAR_BG)
            self.cand_bg_items.append(items[0])
            center = (box[0] + box[2]) / 2
            self.cand_frame.create_text(
                center, top + int(1 * self.ui), text=str(index + 1),
                fill=BAR_SEL_TEXT if index == 0 else BAR_MUTED,
                font=self.cand_digit_font, anchor='n')
            self.cand_frame.create_text(
                center, top + int(12 * self.ui), text=value,
                fill=BAR_SEL_TEXT if index == 0 else BAR_TEXT,
                font=self.cand_font, anchor='n')
            self.cand_hits.append((box[0], box[2], value))
            x = box[2] + pad
        self.cand_frame.config(width=x)
        # Keep the four transparent corner masks above every child; a widget
        # created or re-packed later must never paint a square corner over
        # them.
        for mask in self.bar_masks:
            tk.Misc.lift(mask)  # Canvas.lift is tag_raise; raise the widget

    def paint_candidates(self, hover):
        for index, item in enumerate(self.cand_bg_items):
            color = BAR_SEL_BG if index == 0 else (
                BAR_HOVER if index == hover else BAR_BG)
            self.cand_frame.itemconfigure(item, fill=color)

    def candidate_index(self, x, y=None):
        if y is not None and not (0 <= y <= self.bar_h):
            return None
        for index, (left, right, _value) in enumerate(self.cand_hits):
            if left <= x <= right:
                return index
        return None

    def on_candidate_click(self, event):
        index = self.candidate_index(event.x, event.y)
        # Insert on release so withdrawing the pad cannot leak a stray click.
        if index is not None and index < len(self.candidate_texts):
            self.choose(self.candidate_texts[index], source='click')

    def on_candidate_motion(self, event):
        index = self.candidate_index(event.x, event.y)
        if index is None:
            index = -1
        if getattr(self, '_cand_hover', None) != index:
            self._cand_hover = index
            self.paint_candidates(index)

    def choose(self, text, source='click'):
        target = self.target
        continuous = self.continuous
        input_log('choose %r source=%s continuous=%s target[%s]'
                  % (text, source, continuous, describe_window(target)))
        if continuous:
            self.clear_ink()
            self.clear_candidates()
            self.recognition_token += 1
            self.live_pending = self.live_ready = False
            self.live_version = -1
        if not target or not U.IsWindow(target):
            input_log('choose: target gone, copying')
            if continuous:
                self.exit_continuous('目标窗口已关闭')
            self.hide()
            self.copy(text)
            return
        if U.GetForegroundWindow() != target:
            input_log('choose: refocus from [%s]'
                      % describe_window(U.GetForegroundWindow()))
            U.SetForegroundWindow(target)
            time.sleep(0.06)
        if U.GetForegroundWindow() != target:
            input_log('choose: refocus failed, copying')
            if continuous:
                self.exit_continuous('目标输入框已失焦')
            self.hide()
            self.copy(text)
            return
        # Releasing Alt can leave the target in menu mode, where Unicode
        # input is discarded as a system character. Cancel that state first.
        U.PostMessageW(target, 0x001F, 0, 0)  # WM_CANCELMODE
        # Alt release also moves browser focus to the toolbar; the field's
        # element was saved on Alt down and is focused again (and verified)
        # before typing. Without a saved element the app is left alone.
        state = focus_ready(target, self.focus_element)
        input_log('choose: focus %s' % state)
        before = describe_window(U.GetForegroundWindow(), probe_uia=True)
        sent = (send_unicode(text)
                if state in (FOCUS_FOCUSED, FOCUS_RESTORED, FOCUS_UNKNOWN)
                else False)
        input_log('choose: sent=%s before[%s] after[%s]'
                  % (sent, before,
                     describe_window(U.GetForegroundWindow(), probe_uia=True)))
        if continuous:
            self.arm_continuous()
        else:
            self.hide()
        if not sent:
            # Re-arm first, then flash: the old order let arm_continuous
            # overwrite the 已复制 notice, so a silent failure looked like
            # nothing happened at all.
            self.copy(text)

    def copy(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            self.flash('已复制：%s    按 Ctrl+V 粘贴' % text)
        except Exception:
            traceback.print_exc()

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
            if self.state != 'starting' or (not self.visible and not self.prearmed):
                self.worker.request('release')
                return
            self.session = Session(data['path'], prefix='ime')
            self.last_path = str(self.session.base.with_suffix('.jsonl'))
            self.clear_ink()
            self.state = 'capturing'
            self.expect = 'recognize'
            self.worker.continuous = self.continuous
            self.worker.free_hold = self.continuous
            if not self.continuous and not (U.GetAsyncKeyState(0x12) & 0x8000):
                # A very short Alt tap can end before the hooks finished.
                self.expect = 'cancel'
                self.worker.request('release')
        elif kind == 'frame':
            if self.session and not self.session.closed:
                self.session.write(data)
                if self.state == 'capturing':
                    version = self.stroke_version
                    self.draw(data)
                    if self.stroke_version != version:
                        # Ink started: the bar belongs to the strokes from now
                        # on, so drop the idle 连写 status immediately.
                        self.info.config(text='')
                        if self.prearmed and not self.visible:
                            # Ink has started: show the pad without waiting
                            # for the rest of the hold gate.
                            self.show_due = True
        elif kind == 'stopped':
            expect = self.expect
            stroke_points = self.finished_strokes()
            self.last_strokes = stroke_points
            self.recording_strokes = []
            self.recording_active.clear()
            self.tracker.active.clear()
            if self.session:
                self.session.close(data)
                self.session = None
            recognize = should_recognize(data, expect)
            # A live answer may be kept only when it covers all the ink;
            # otherwise the final pass reads the strokes in full.
            keep = bool(recognize and self.candidate_texts and self.live_ready
                        and self.live_version == self.ink_version
                        and expect in ('keep', 'recognize'))
            if self.continuous and (expect != 'recognize' or not recognize):
                # Safety stop or explicit exit: leave 连写, keep the bar if any.
                self.continuous = False
                self.worker.continuous = False
                self.worker.free_hold = False
                self.taps.reset()
                self.exit_press.reset()
            self.expect = None
            self.prearmed = False
            self.show_due = False
            self.live_pending = False
            if not self.visible:
                self.state = 'idle'
                self.target = None
                self.worker.target = None
                return
            if keep:
                self.state = 'choosing'
                self.live_ready = False
                self.info.config(text='')
                self.worker.request(('select_on', len(self.candidate_texts)))
                self.regain_caret()
            elif not recognize:
                self.hide()
            elif stroke_points:
                self.recognize(stroke_points)
            else:
                # Nothing was written: an empty bar with only the close button
                # would just sit there, so put it away.
                self.hide()
        elif kind == 'recognized':
            if data.get('token') != self.recognition_token:
                # A newer stroke, live request or round owns the bar now.
                return
            if not self.visible:
                self.live_pending = False
                return
            live = self.live_pending and self.state == 'capturing'
            self.live_pending = False
            if live:
                self.show_recognition(data, live=True)
            else:
                self.show_recognition(data)
        elif kind == 'select':
            # Live candidates (Alt still held / 连写) stay in 'capturing', so
            # the old 'choosing'-only guard made digits dead exactly when the
            # user wanted them. Clicks already worked; match them here.
            if (isinstance(data, int) and self.candidate_texts
                    and 1 <= data <= len(self.candidate_texts)
                    and (self.state == 'choosing'
                         or (self.state == 'capturing' and self.live_ready))):
                self.choose(self.candidate_texts[data - 1], source='digit')
        elif kind == 'select_blocked':
            # The pinyin IME owns the digits right now. Say so instead of
            # leaving a bar that looks ready but ignores every selection.
            input_log('view: digits unavailable: %s' % data)
            if self.visible:
                self.info.config(text='输入法组词中：数字暂不可选')
        elif kind == 'exit_continuous':
            # A physical right click belongs to the app; it also leaves 连写.
            self.exit_continuous(data or '右键退出连写')
        elif kind == 'typing':
            # The user went back to typing (pinyin/ordinary keys); the bar is
            # stale and must not keep intercepting digits.
            if self.state == 'choosing':
                self.hide()
        elif kind == 'select_failed':
            # Digits stayed unavailable; the candidates are still clickable.
            pass
        elif kind in ('notice', 'error'):
            self.expect = None
            if self.prearmed:
                # The hidden pre-arm could not capture; do not show a dead pad.
                self.prearmed = False
                self.show_due = False
                self.state = 'idle'
                self.target = None
                self.worker.target = None
            self.info.config(text=str(data))
            if kind == 'error':
                self.ready = False
            if self.visible:
                self.schedule_hide(2200)
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
            if self.show_due:
                self.show_due = False
                if (self.prearmed and not self.visible
                        and self.state in ('starting', 'capturing')):
                    self.start()
            if (self.visible
                    and self.state == 'capturing' and self.expect == 'recognize'
                    and not self.prearmed and not self.live_pending
                    and self.recording_strokes
                    and self.ink_version != self.live_version
                    and time.monotonic() - self.last_live_wall >= LIVE_REFRESH):
                # No pause detection: as soon as the ink changed, recognize
                # again, throttled to LIVE_REFRESH so the bar refreshes in
                # place while the finger is still moving. Results stay a
                # preview until release; the next points replace them.
                self.live_recognize()
            if self.visible and not self.safe_preview:
                down = bool(U.GetAsyncKeyState(0x1B) & 0x8000)
                if down and not self.esc_down:
                    if self.state == 'capturing':
                        self.cancel()
                    else:
                        self.hide()
                self.esc_down = down
                if (self.state in ('choosing', 'recognizing') and self.target
                        and U.IsWindow(self.target)
                        and U.GetForegroundWindow() not in (self.target, self.hwnd)):
                    self.hide()
        except BaseException:
            self.on_error(*sys.exc_info())
        self.root.after(16, self.tick)

    def on_error(self, exc_type, exc, tb):
        self.worker.request('release')
        traceback.print_exception(exc_type, exc, tb)
        if self.session:
            try:
                self.session.close('界面异常；本段数据可能不完整：' + str(exc))
            except BaseException:
                traceback.print_exc()
            self.session = None
        if not self.closing:
            self.hide()

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.worker:
            self.worker.stopping.set()
            self.worker.join(timeout=3)
        self.drop_focus_element()
        try:
            if self.session:
                self.session.close('浮层关闭')
        except BaseException:
            traceback.print_exc()
        finally:
            self.root.destroy()

    # ---------- safe verification ----------
    def demo(self, strokes):
        self.pos = (220, 160)
        self.show(*self.pos)
        self.root.wm_attributes('-transparentcolor', KEY)
        self.state = 'recognizing'
        self.clear_candidates()
        self.clear_ink()
        self.draw_strokes(strokes)
        self.recognize(strokes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='non-activating overlay smoke test; no capture hooks')
    args = parser.parse_args()
    enable_dpi_awareness()
    pad_info.configure()
    root = tk.Tk()
    app = ImeWindow(root, safe_preview=args.self_check)
    if args.self_check:
        from test_ime_window import visual_check
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
