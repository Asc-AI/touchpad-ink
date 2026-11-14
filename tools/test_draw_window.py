import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
import unittest
from unittest.mock import patch

import draw_window as D
import ime_window as I
from test_windows_ink import first_character

BASE = Path(__file__).resolve().parent.parent


class MapTests(unittest.TestCase):
    def test_corners_fill_the_rect(self):
        self.assertEqual(D.draw_map(1920, 1080, 0, 0), (0.0, 0.0))
        self.assertEqual(D.draw_map(1920, 1080, 1332, 828), (1920.0, 1080.0))

    def test_center(self):
        self.assertEqual(D.draw_map(1000, 500, 666, 414), (500.0, 250.0))

    def test_instance_map_uses_monitor_rect(self):
        app = type('App', (), {})()
        app.rect = (100, 50, 2000, 1000)
        self.assertEqual(D.DrawWindow.map(app, 1332, 828), (2000.0, 1000.0))


class FakeWorker:
    """No hooks, no WM_INPUT; records commands only."""
    def __init__(self, events, owner, allow_capture=True, fallback_hotkey=False,
                 start_hotkey=True, alt_releases=True, alt_stops=True):
        self.events = events
        self.owner = owner
        self.target = None
        self.allow_capture = allow_capture
        self.fallback_hotkey = fallback_hotkey
        self.start_hotkey = start_hotkey
        self.alt_releases = alt_releases
        self.alt_stops = alt_stops
        self.guard = type('Guard', (), {'active': False, 'heartbeat': 0.0})()
        self.stopping = threading.Event()
        self.commands = []

    def start(self):
        pass

    def request(self, command):
        self.commands.append(command)

    def join(self, timeout=None):
        pass


class FakeSession:
    def __init__(self, device, prefix='live'):
        self.device = device
        self.prefix = prefix
        self.base = Path('fake_draw_session')
        self.frames = []
        self.closed = False
        self.reason = None

    def write(self, frame):
        self.frames.append(frame)

    def close(self, reason):
        self.closed = True
        self.reason = reason


class FakeHud:
    """Records the mode/state lines instead of opening a real Tk window."""
    def __init__(self, root):
        self.shows = []
        self.closed = False

    def show(self, title, lines=(), seconds=1.8):
        self.shows.append((title, tuple(lines), seconds))

    def hide(self, token=None):
        pass

    def close(self):
        self.closed = True


@unittest.skipUnless(sys.platform == 'win32', 'Win32 full-screen layer')
class DrawFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.session_patch = patch.object(D, 'Session', FakeSession)
        self.hud_patch = patch.object(D, 'StatusHud', FakeHud)
        self.session_patch.start()
        self.hud_patch.start()
        with patch.object(D, 'LiveInput', FakeWorker):
            self.app = D.DrawWindow(self.win, safe_preview=False)
        self.app.alt_held = self.app.alt_blocked = False
        self.app.alt_since = 0.0
        self.app.ready = True
        self.app.visible = True
        self.app.rect = (0, 0, 1332, 828)
        self.app.target = 100

    def tearDown(self):
        self.app.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.session_patch.stop()
        self.hud_patch.stop()

    def _ink(self):
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='draw')
        self.app.event('frame', {'t': 0.0, 'contacts': [
            {'id': 0, 'x': 100, 'y': 100, 'tip': True, 'confidence': True}]})
        self.app.event('frame', {'t': 0.02, 'contacts': [
            {'id': 0, 'x': 200, 'y': 300, 'tip': True, 'confidence': True}]})
        self.assertTrue(self.app.canvas.find_withtag('ink'))

    def test_click_through_and_non_activating(self):
        style = D.U.GetWindowLongPtrW(self.app.hwnd, I.GWL_EXSTYLE)
        self.assertTrue(style & D.WS_EX_TRANSPARENT)
        self.assertTrue(style & I.WS_EX_NOACTIVATE)
        self.assertTrue(style & I.WS_EX_TOOLWINDOW)

    def test_alt_hold_starts_after_gate_only_once(self):
        self.app.state = 'idle'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(D.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.1)
            self.assertEqual(started, [])
            self.app.poll_alt(10.2)
            self.app.poll_alt(10.3)
        self.assertEqual(started, [1])

    def test_disabled_trigger_never_starts_on_its_own(self):
        self.app.state = 'idle'
        self.app.trigger_enabled = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(D.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_auto_pending_starts_without_the_gate(self):
        self.app.state = 'idle'
        self.app.alt_held = True
        self.app.auto_pending = True
        self.app.trigger_enabled = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(D.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
        self.assertEqual(started, [1])
        self.assertFalse(self.app.auto_pending)

    def test_alt_with_ctrl_never_starts(self):
        self.app.state = 'idle'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(
                D.U, 'GetAsyncKeyState',
                side_effect=lambda vk: 0x8000 if vk in (0x12, 0x11) else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_alt_release_stops_capture(self):
        self.app.state = 'capturing'
        self.app.alt_held = True
        with patch.object(D.U, 'GetAsyncKeyState', return_value=0):
            self.app.poll_alt(10.0)
        self.assertEqual(self.app.worker.commands, ['release'])
        self.assertEqual(self.app.expect, 'cancel')

    def test_alt_press_while_starting_is_ignored(self):
        self.app.state = 'starting'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(D.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_start_maps_cursor_monitor_and_requests_capture(self):
        self.app.state = 'idle'
        self.app.visible = False
        self.app.target = None
        self.app.canvas.create_line(1, 1, 2, 2, tags='ink')
        with patch.object(D, 'monitor_rect', return_value=(100, 50, 800, 600)), \
             patch.object(D.U, 'GetForegroundWindow', return_value=200), \
             patch.object(D.U, 'IsIconic', return_value=False), \
             patch.object(D.U, 'GetCursorPos'), \
             patch.object(D.U, 'SetForegroundWindow'), \
             patch.object(D.U, 'SetWindowPos') as position:
            self.app.start()
        self.assertEqual(self.app.rect, (100, 50, 800, 600))
        self.assertEqual(self.app.target, 200)
        self.assertEqual(self.app.worker.target, 200)
        self.assertEqual(self.app.worker.commands, ['start'])
        self.assertEqual(self.app.state, 'starting')
        self.assertTrue(self.app.visible)
        self.assertEqual(self.app.canvas.find_withtag('ink'), ())
        self.assertEqual(position.call_args[0][2:6], (100, 50, 800, 600))

    def test_start_without_foreground_is_ignored(self):
        self.app.state = 'idle'
        self.app.visible = False
        with patch.object(D.U, 'GetForegroundWindow', return_value=0):
            self.app.start()
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.worker.commands, [])

    def test_release_keeps_ink_and_cancels_menu_mode(self):
        self._ink()
        session = self.app.session
        with patch.object(D.U, 'IsWindow', return_value=True), \
             patch.object(D.U, 'PostMessageW') as post:
            self.app.event('stopped', 'Alt：松开并识别')
            post.assert_called_once_with(100, 0x001F, 0, 0)
        self.assertTrue(self.app.visible)
        self.assertEqual(self.app.state, 'idle')
        self.assertTrue(self.app.canvas.find_withtag('ink'))
        self.assertIsNone(self.app.target)
        self.assertTrue(session.closed)

    def test_release_without_ink_hides(self):
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='draw')
        with patch.object(D.U, 'IsWindow', return_value=False):
            self.app.event('stopped', 'Alt：松开并识别')
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.state, 'idle')

    def test_escape_reason_clears_and_hides(self):
        self._ink()
        with patch.object(D.U, 'IsWindow', return_value=True), \
             patch.object(D.U, 'PostMessageW'):
            self.app.event('stopped', 'Esc：取消并释放')
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.canvas.find_withtag('ink'), ())

    def test_clear_annotation_releases_active_capture(self):
        self.app.state = 'capturing'
        self.app.worker.guard.active = True
        self.app.clear_annotation()
        self.assertEqual(self.app.worker.commands, ['release'])
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.state, 'idle')

    def test_new_alt_press_clears_old_annotation(self):
        self._ink()
        # An idle layer with ink is cleared by the next start().
        self.app.state = 'idle'
        self.app.visible = True
        with patch.object(D, 'monitor_rect', return_value=(0, 0, 800, 600)), \
             patch.object(D.U, 'GetForegroundWindow', return_value=200), \
             patch.object(D.U, 'IsIconic', return_value=False), \
             patch.object(D.U, 'GetCursorPos'), \
             patch.object(D.U, 'SetForegroundWindow'), \
             patch.object(D.U, 'SetWindowPos'):
            self.app.start()
        self.assertEqual(self.app.canvas.find_withtag('ink'), ())

    def test_quit_event_uses_host_callback(self):
        called = []
        self.app.on_quit = lambda: called.append(1)
        self.app.event('quit', None)
        self.assertEqual(called, [1])


def visual_check(app):
    """Safe self-check: replay ink full-screen and verify the screenshot."""
    strokes = first_character()
    bbox = {}

    def screen_box():
        points = [app.map(x, y) for stroke in strokes for x, y in stroke]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        left, top, width, height = app.rect
        x0 = max(left, int(left + min(xs)) - 40)
        y0 = max(top, int(top + min(ys)) - 40)
        x1 = min(left + width, int(left + max(xs)) + 40)
        y1 = min(top + height, int(top + max(ys)) + 40)
        return x0, y0, x1, y1

    try:
        style = D.U.GetWindowLongPtrW(app.hwnd, I.GWL_EXSTYLE)
        assert style & D.WS_EX_TRANSPARENT, 'layer must be click-through'
        assert style & I.WS_EX_NOACTIVATE, 'layer must not activate'
        assert style & I.WS_EX_TOOLWINDOW, 'layer must stay out of Alt+Tab'
        app.demo(strokes)
        bbox.update(box=screen_box())
    except BaseException:
        app.self_check_failed = True
        traceback.print_exc()
        app.close()
        return

    def capture():
        try:
            from PIL import ImageGrab
            x0, y0, x1, y1 = bbox['box']
            image = ImageGrab.grab(bbox=(x0, y0, x1, y1))
            out = BASE / 'data' / 'draw_preview.png'
            image.save(out)
            pixels = list(image.getdata())
            ink = sum(1 for p in pixels if abs(p[0] - 30) <= 10
                      and abs(p[1] - 51) <= 10 and abs(p[2] - 80) <= 10)
            assert ink > 200, 'ink pixels: %d' % ink
            print('draw visual check ok: rect=%s box=%s ink=%d saved=%s'
                  % (app.rect, bbox['box'], ink, out))
        except BaseException:
            app.self_check_failed = True
            traceback.print_exc()
        finally:
            app.close()

    app.root.after(450, capture)


if __name__ == '__main__':
    unittest.main()
