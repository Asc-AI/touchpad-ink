"""Resident touchpad handwriting host with a notification-area icon.

One process owns the tray icon and exactly one mode at a time:

* 自动识别 - on each Alt press, pick the view from the foreground window
* 局部打字 - the caret-anchored IME overlay (ime_window.ImeWindow)
* 全域画图 - the full-screen annotation layer (draw_window.DrawWindow)
* PPT 墨迹 - drive the presentation's own pen with left drags (ppt_ink.PptInk)
* Note 墨迹 - the same left-drag ink, only while OneNote is foreground

Auto detection runs once per hold (no background scanning): OneNote -> note,
a running PowerPoint/WPS slide show -> ppt, a visible text caret -> typing,
anything else -> nothing. Views created by auto start with their own Alt
trigger disabled; the host hands the current press over by setting
``auto_pending``.

Switching modes first joins the old capture thread, so only one raw-input
worker and one set of hotkeys exist. Every view uses hold-to-write.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import queue
import sys
import time
import tkinter as tk
import traceback

from auto_mode import AutoSelector, auto_log, foreground_kind
import autostart
import pad_info
from draw_window import DrawWindow
from gesture import DoublePress, TapDetector
from ime_window import ImeWindow, enable_dpi_awareness
from live_input import U
from ppt_ink import NoteInk, PptInk
from tray_icon import TrayIcon

MODES = ('auto', 'typing', 'draw', 'ppt', 'note')
VIEW_KINDS = ('typing', 'draw', 'ppt', 'note')
# A view in one of these states is done with the previous press, so auto may
# pick a (possibly different) view for the next one.
FREE_STATES = ('idle', 'choosing')

FindWindowW = C.WINFUNCTYPE(W.HWND, W.LPCWSTR, W.LPCWSTR)(
    ('FindWindowW', U))


def existing_tray():
    """Another tray process already owns the hidden window (any build)."""
    try:
        return bool(FindWindowW(None, 'TouchpadTray'))
    except BaseException:
        return False



class Host:
    def __init__(self, root, safe_preview=False, tray=None, views=None):
        self.root = root
        self.safe_preview = safe_preview
        self.closing = False
        self.commands = queue.Queue()
        self.views = views or {
            'typing': lambda win, trigger_enabled=True: ImeWindow(
                win, safe_preview=safe_preview, on_quit=self.quit,
                trigger_enabled=trigger_enabled),
            'draw': lambda win, trigger_enabled=True: DrawWindow(
                win, safe_preview=safe_preview, on_quit=self.quit,
                trigger_enabled=trigger_enabled),
            'ppt': lambda win, trigger_enabled=True: PptInk(
                win, safe_preview=safe_preview, on_quit=self.quit,
                trigger_enabled=trigger_enabled),
            'note': lambda win, trigger_enabled=True: NoteInk(
                win, safe_preview=safe_preview, on_quit=self.quit,
                trigger_enabled=trigger_enabled),
        }
        self.selector = AutoSelector()
        self.taps = TapDetector()
        self.exit_press = DoublePress()  # 连写 exits on two Alt downs
        self.view = None
        self.view_kind = None
        self.mode = None
        self.tray = tray or None
        self.log = None  # set by main(); keeps tests from writing auto.log
        self.alt_was_down = False
        self.down_kind = None  # foreground classification taken at press-down
        self.down_fg = 0
        root.withdraw()
        root.title('触控板手写')
        root.report_callback_exception = self.report_exception
        if self.tray is None and not safe_preview:
            self.tray = TrayIcon(self.commands,
                                 startup_check=autostart.enabled)
        if self.tray is not None:
            self.tray.start()
        self.switch('auto')
        root.after(16, self.tick)

    # ---------- modes ----------
    def switch(self, mode):
        if self.closing or mode not in MODES:
            return
        if mode == self.mode and (mode == 'auto' or self.view is not None):
            return
        self.mode = mode
        self.selector.reset()
        self.taps.reset()
        self.exit_press.reset()
        if self.log is not None:
            self.log('mode -> %s' % mode)
        if mode == 'auto':
            # Keep one parked typing view so Ctrl+F8 is registered from
            # startup; it never triggers on its own.
            if self.view is None or self.view_kind != 'typing':
                self.open_view('typing', trigger_enabled=False)
            else:
                self.view.trigger_enabled = False
        else:
            self.open_view(mode, trigger_enabled=True)
        if self.tray is not None:
            self.tray.set_mode(mode)

    def open_view(self, kind, trigger_enabled):
        """Close the old view and build ``kind``; None on failure."""
        self.close_view()
        if kind not in self.views:
            return None
        win = tk.Toplevel(self.root)
        win.withdraw()
        try:
            view = self.views[kind](win, trigger_enabled=trigger_enabled)
        except BaseException:
            traceback.print_exc()
            try:
                win.destroy()
            except BaseException:
                pass
            return None
        self.view = view
        self.view_kind = kind
        return view

    def close_view(self):
        view, self.view = self.view, None
        self.view_kind = None
        if view is not None:
            try:
                view.close()
            except BaseException:
                traceback.print_exc()

    def auto_press(self, now=None):
        """Detect one eligible Alt hold and hand it to the matching view."""
        now = time.monotonic() if now is None else now
        down = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        blocked = any(U.GetAsyncKeyState(key) & 0x8000
                      for key in AutoSelector.BLOCK_KEYS)
        gesture = self.taps.update(down, now)
        exit_press = self.exit_press.update(down, now)
        log = self.log
        down_edge = down and not self.alt_was_down
        up_edge = self.alt_was_down and not down
        if down:
            self.alt_was_down = True
        elif self.alt_was_down:
            self.alt_was_down = False
        if log is not None:
            if down_edge:
                log('alt down (mode=%s blocked=%s)' % (self.mode, blocked))
            elif up_edge:
                log('alt up')
        if down_edge:
            self.down_kind = self.down_fg = None
            if not blocked:
                self.preview_press()
        view = self.view
        if view is not None and getattr(view, 'continuous', False):
            # 连写 owns the pad; only a double Alt (handled here because auto
            # views ignore it) leaves the mode. Two press-downs count even
            # when each press is held a little longer while moving.
            if (exit_press == 'double' or gesture == 'double') and not blocked:
                exit_continuous = getattr(view, 'exit_continuous', None)
                if exit_continuous is not None:
                    if log is not None:
                        log('double alt: leave continuous')
                    exit_continuous()
                    self.taps.reset()
                    self.exit_press.reset()
            return False
        if (gesture == 'double' and not blocked
                and getattr(view, 'state', 'idle') in FREE_STATES):
            entered = self.auto_continuous()
            if entered:
                self.taps.reset()
                self.exit_press.reset()
            return entered
        if not self.selector.poll(now, down, blocked):
            return False
        if log is not None:
            log('alt gate fired')
        view = self.view
        if (view is not None
                and getattr(view, 'state', 'idle') not in FREE_STATES):
            if log is not None:
                log('live view busy: %s' % getattr(view, 'state', '?'))
            return False  # the live view already owns this press
        fg = U.GetForegroundWindow()
        if (self.down_fg and fg == self.down_fg
                and self.down_kind is not None):
            kind = self.down_kind  # classified at press-down for the preview
        else:
            kind = foreground_kind()
        if kind is None:
            if log is not None:
                log('no view for the current foreground')
            return False
        if view is None or self.view_kind != kind:
            view = self.open_view(kind, trigger_enabled=False)
        if view is None:
            if log is not None:
                log('could not create the %s view' % kind)
            return False
        view.auto_pending = True
        if log is not None:
            log('handed the press to %s' % kind)
        return True

    def preview_press(self):
        """Pop the PPT region frame on Alt press-down, before the hold gate.

        Only PPT has a local region worth showing; the classification is
        cached so the gate does not pay for it twice.
        """
        view = self.view
        if getattr(view, 'continuous', False):
            return
        if getattr(view, 'state', 'idle') not in FREE_STATES:
            return
        try:
            fg = U.GetForegroundWindow()
            kind = foreground_kind(quiet=True)
        except BaseException:
            traceback.print_exc()
            return
        self.down_fg, self.down_kind = fg, kind
        if kind != 'ppt':
            return
        if view is None or self.view_kind != 'ppt':
            view = self.open_view('ppt', trigger_enabled=False)
        preview = getattr(view, 'preview_region', None)
        if view is None or preview is None:
            return
        try:
            if preview() and self.log is not None:
                self.log('preview ppt region')
        except BaseException:
            traceback.print_exc()

    def auto_continuous(self):
        """Host-side double tap: enter 连写 in the view for this foreground."""
        log = self.log
        kind = foreground_kind()
        if kind in (None, 'draw'):
            if log is not None:
                log('double alt: no continuous view for %s' % kind)
            return False
        view = self.view
        if view is None or self.view_kind != kind:
            view = self.open_view(kind, trigger_enabled=False)
        enter = getattr(view, 'enter_continuous', None)
        if view is None or enter is None:
            return False
        if not enter():
            if log is not None:
                log('double alt: could not enter continuous %s' % kind)
            return False
        if log is not None:
            log('double alt: continuous %s' % kind)
        return True

    def clear(self):
        view = self.view
        if (self.view_kind == 'draw' and view is not None
                and hasattr(view, 'clear_annotation')):
            view.clear_annotation()

    # ---------- loop ----------
    def tick(self):
        if self.closing:
            return
        try:
            for _ in range(20):
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                self.handle(command)
            if self.mode == 'auto' and not self.safe_preview:
                self.auto_press()
        except BaseException:
            self.report_exception(*sys.exc_info())
        if not self.closing:
            self.root.after(16, self.tick)

    def handle(self, command):
        kind = command[0] if command else ''
        if kind == 'mode' and len(command) > 1:
            self.switch(command[1])
        elif kind == 'startup':
            self.toggle_startup()
        elif kind == 'clear':
            self.clear()
        elif kind == 'quit':
            self.quit()

    def toggle_startup(self):
        """Turn "start at sign-in" on or off; log the outcome for diagnosis."""
        state = autostart.toggle()
        if state is None:
            message = 'startup: could not update the Run key'
        else:
            message = 'startup -> %s' % ('on' if state else 'off')
        if self.log is not None:
            self.log(message)
        else:
            print(message, file=sys.stderr)
        return state

    def report_exception(self, exc_type, exc, tb):
        view = self.view
        if view is not None and hasattr(view, 'on_error'):
            try:
                view.on_error(exc_type, exc, tb)
                return
            except BaseException:
                pass
        traceback.print_exception(exc_type, exc, tb)

    def quit(self):
        if self.closing:
            return
        self.closing = True
        self.close_view()
        tray, self.tray = self.tray, None
        if tray is not None:
            try:
                tray.stop()
            except BaseException:
                traceback.print_exc()
        try:
            self.root.destroy()
        except BaseException:
            traceback.print_exc()


def self_check():
    root = tk.Tk()
    host = Host(root, safe_preview=True, tray=False)
    result = {'ok': False}

    def verify():
        ok = []
        try:
            ok.append(host.mode == 'auto')
            ok.append(type(host.view).__name__ == 'ImeWindow')
            ok.append(not host.view.trigger_enabled)
            for mode, name in (('typing', 'ImeWindow'), ('draw', 'DrawWindow'),
                               ('ppt', 'PptInk'), ('note', 'NoteInk')):
                host.switch(mode)
                ok.append(host.mode == mode)
                ok.append(type(host.view).__name__ == name)
            host.switch('auto')
            ok.append(host.mode == 'auto')
            ok.append(type(host.view).__name__ == 'ImeWindow')
            ok.append(not host.view.trigger_enabled)
        except BaseException:
            traceback.print_exc()
            ok.append(False)
        result['ok'] = all(ok)
        print('host self-check: modes=%s ok=%s' % (MODES, result['ok']))
        host.quit()

    root.after(500, verify)
    try:
        root.mainloop()
    finally:
        if not host.closing:
            host.quit()
    return 0 if result['ok'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='switch all modes without capture; no hooks')
    args = parser.parse_args()
    enable_dpi_awareness()
    if args.self_check:
        return self_check()
    pad = pad_info.configure()
    if pad is not None:
        auto_log('pad: ' + pad.describe())
    else:
        auto_log('pad: ' + pad_info.failure())
    if existing_tray():
        print('Another touchpad tray is already running: press Ctrl+F8 in the '
              'old one, then start this build again.', flush=True)
        return 1
    if autostart.refresh():
        auto_log('startup: repointed to this build')
    root = tk.Tk()
    host = Host(root)
    host.log = auto_log
    auto_log('tray started: mode=%s' % host.mode)
    try:
        root.mainloop()
    finally:
        if not host.closing:
            host.quit()
    return 0


if __name__ == '__main__':
    sys.exit(main())
