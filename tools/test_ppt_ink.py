import ctypes as C
from ctypes import wintypes as W
import queue
import sys
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_mode as A
import live_input as L
import ppt_ink as P


def frame(t, points):
    return {'t': t, 'contacts': [
        {'id': cid, 'x': x, 'y': y, 'tip': True, 'confidence': True}
        for cid, x, y in points]}


class MapTests(unittest.TestCase):
    def test_rect_corners(self):
        rect = (100, 50, 1600, 900)
        self.assertEqual(P.pad_to_screen(rect, 0, 0), (100.0, 50.0))
        self.assertEqual(P.pad_to_screen(rect, P.PAD_MAX_X, P.PAD_MAX_Y),
                         (1700.0, 950.0))

    def test_rect_center(self):
        self.assertEqual(P.pad_to_screen((0, 0, 1332, 828), 666, 414),
                         (666.0, 414.0))

    def test_absolute_full_span(self):
        self.assertEqual(P.absolute(0, 0, 0, 0, 2560, 1600), (0, 0))
        self.assertEqual(P.absolute(2559, 1599, 0, 0, 2560, 1600),
                         (65535, 65535))

    def test_absolute_handles_negative_origin(self):
        # Second monitor left of the primary: virtual origin is negative.
        self.assertEqual(P.absolute(-1920, 0, -1920, 0, 4480, 1600), (0, 0))
        self.assertEqual(P.absolute(2559, 1599, -1920, 0, 4480, 1600),
                         (65535, 65535))

    def test_absolute_clamps_outside_desktop(self):
        self.assertEqual(P.absolute(-100, -100, 0, 0, 1920, 1080), (0, 0))
        self.assertEqual(P.absolute(9999, 9999, 0, 0, 1920, 1080),
                         (65535, 65535))

    def test_client_rect_without_window(self):
        self.assertIsNone(P.client_rect(None))
        self.assertIsNone(P.client_rect(0))


class RegionTests(unittest.TestCase):
    """Mac-like local mapping: pad -> sub-rectangle around the pointer."""

    def test_scale_two_is_half_width_with_pad_aspect(self):
        rect = (100, 50, 1600, 900)
        region = P.region_rect(rect, (900, 500), 2.0)
        self.assertEqual(region[0], 500)
        self.assertLessEqual(abs(region[2] - 800), 1)
        self.assertLessEqual(abs(region[3] - 497), 1)
        self.assertLessEqual(
            abs(region[2] / region[3] - P.PAD_MAX_X / P.PAD_MAX_Y), 0.01)

    def test_pointer_inside_region_becomes_the_pad_centre(self):
        rect = (100, 50, 1600, 900)
        region = P.region_rect(rect, (900, 500), 2.0)
        x, y = P.pad_to_screen(region, P.PAD_MAX_X / 2, P.PAD_MAX_Y / 2)
        self.assertLessEqual(abs(x - 900), 1.5)
        self.assertLessEqual(abs(y - 500), 1.5)

    def test_ppt_anchor_puts_the_pointer_upper_left(self):
        rect = (100, 50, 1600, 900)
        region = P.region_rect(rect, (900, 500),
                               P.REGION_SCALE, P.REGION_ANCHOR)
        left, top, width, height = region
        self.assertTrue(left <= 900 <= left + width)
        self.assertTrue(top <= 500 <= top + height)
        # Rightward / downward writing gets the larger share of the region.
        self.assertLess(900 - left, left + width - 900)
        self.assertLess(500 - top, top + height - 500)
        # Pad origin is the region's top-left, so the spare room is usable.
        self.assertEqual(P.pad_to_screen(region, 0, 0), (left, top))

    def test_default_region_is_a_third_of_the_slide(self):
        rect = (0, 0, 2400, 1350)
        region = P.region_rect(rect, (900, 500))
        self.assertLessEqual(abs(region[2] - 800), 1)
        self.assertLessEqual(
            abs(region[2] / region[3] - P.PAD_MAX_X / P.PAD_MAX_Y), 0.01)

    def test_region_is_clamped_inside_the_client(self):
        rect = (100, 50, 1600, 900)
        region = P.region_rect(rect, (1700, 950), 2.0)
        self.assertLessEqual(region[0] + region[2], 1700)
        self.assertLessEqual(region[1] + region[3], 950)
        self.assertGreaterEqual(region[0], 100)
        self.assertGreaterEqual(region[1], 50)

    def test_pointer_outside_client_centres_the_region(self):
        rect = (100, 50, 1600, 900)
        region = P.region_rect(rect, (9000, 9000), 2.0)
        self.assertLessEqual(abs(region[0] - 500), 1)
        self.assertLessEqual(abs(region[1] + region[3] / 2 - 500), 1.5)

    def test_short_client_limits_by_height_and_keeps_the_aspect(self):
        region = P.region_rect((0, 0, 2000, 400), (1000, 200), 2.0)
        self.assertEqual(region[3], 400)
        self.assertLessEqual(
            abs(region[2] / region[3] - P.PAD_MAX_X / P.PAD_MAX_Y), 0.01)

    def test_scale_one_is_the_whole_client(self):
        rect = (10, 20, 800, 600)
        self.assertEqual(P.region_rect(rect, (100, 100), 1.0), rect)

    def test_env_scale_defaults_and_clamps(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(P.env_region_scale(2.0), 2.0)
        for raw, expected in (('3.5', 3.5), ('bad', 2.0),
                              ('99', P.REGION_MAX_SCALE), ('0.2', 1.0)):
            with patch.dict('os.environ', {'TOUCHPAD_INK_REGION': raw}):
                self.assertEqual(P.env_region_scale(2.0), expected)


class FakeWorker:
    """No hooks, no WM_INPUT; records commands only."""
    def __init__(self, events, owner, allow_capture=True, fallback_hotkey=False,
                 start_hotkey=True, alt_releases=True, alt_stops=True,
                 alt_hold=False):
        self.events = events
        self.owner = owner
        self.target = None
        self.allow_capture = allow_capture
        self.fallback_hotkey = fallback_hotkey
        self.start_hotkey = start_hotkey
        self.alt_releases = alt_releases
        self.alt_stops = alt_stops
        self.alt_hold = alt_hold
        self.continuous = False
        self.suppress = True
        self.free_hold = False
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
        self.base = Path('fake_ppt_session')
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


class FakeFrame:
    def __init__(self, root):
        self.visible = False
        self.rect = None
        self.status = ()
        self.extra = ()
        self.closed = False
        self.shows = 0

    def show(self, rect, status=None, extra=()):
        self.shows += 1
        self.visible = True
        self.rect = rect
        self.status = tuple(status or ())
        self.extra = tuple(extra or ())

    def hide(self):
        self.visible = False

    def close(self):
        self.closed = True


@unittest.skipUnless(sys.platform == 'win32', 'Win32 raw input host')
class PptFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.session_patch = patch.object(P, 'Session', FakeSession)
        self.hud_patch = patch.object(P, 'StatusHud', FakeHud)
        self.frame_patch = patch.object(P, 'RegionFrame', FakeFrame)
        self.session_patch.start()
        self.hud_patch.start()
        self.frame_patch.start()
        with patch.object(P, 'LiveInput', FakeWorker), \
             patch.object(P.U, 'GetAsyncKeyState', return_value=0):
            self.app = P.PptInk(self.win)
        self.app.alt_held = self.app.alt_blocked = False
        self.app.alt_since = 0.0
        self.app.ready = True
        self.app.rect = (0, 0, 1332, 828)
        self.app.region = (0, 0, 0, 0)  # fall back to rect
        self.app.smooth_alpha = 1.0     # legacy tests inject raw points
        self.app.min_step = 0.0
        self.app.virtual = (0, 0, 1332, 828)
        # Coordinates in these tests start at the pad border; the edge gate
        # gets its own tests below.
        self.app.edge_start = (0, 0)

    def tearDown(self):
        self.app.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.session_patch.stop()
        self.hud_patch.stop()
        self.frame_patch.stop()

    def _capture(self):
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')

    def test_start_uses_client_rect_and_requests_capture(self):
        self.app.state = 'idle'
        with patch.object(P, 'client_rect', return_value=(10, 20, 800, 600)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'SetForegroundWindow') as foreground:
            self.app.start()
        self.assertEqual(self.app.rect, (10, 20, 800, 600))
        self.assertEqual(self.app.virtual, (0, 0, 2560, 1600))
        self.assertEqual(self.app.target, 300)
        self.assertEqual(self.app.worker.target, 300)
        self.assertEqual(self.app.worker.commands, ['start'])
        self.assertEqual(self.app.state, 'starting')
        foreground.assert_not_called()  # the presentation keeps focus

    def test_start_anchors_the_region_at_the_pointer(self):
        self.app.state = 'idle'
        cursor = W.POINT(700, 400)

        def get_cursor(pointer):
            C.cast(pointer, C.POINTER(W.POINT)).contents.x = cursor.x
            C.cast(pointer, C.POINTER(W.POINT)).contents.y = cursor.y
            return True

        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self.app.start()
        self.assertEqual(self.app.region,
                         P.region_rect((100, 50, 1600, 900), (700, 400),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        x, y = self.app.map(P.PAD_MAX_X * P.REGION_ANCHOR[0],
                            P.PAD_MAX_Y * P.REGION_ANCHOR[1])
        self.assertLessEqual(abs(x - 700), 1.5)
        self.assertLessEqual(abs(y - 400), 1.5)

    def test_the_hold_gate_keeps_the_pointer_the_preview_showed(self):
        # Until capture starts the pad still moves the mouse. The region must
        # stay where Alt-down previewed it, not follow the hand's drift.
        self.app.state = 'idle'
        pos = [(700, 400)]

        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = pos[0]
            return True

        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self.assertTrue(self.app.preview_region())
            pos[0] = (300, 300)  # the hand pushed the pointer during the gate
            self.app.start()
        self.assertEqual(self.app.anchor, (700, 400))
        self.assertEqual(self.app.pen_anchor, (700, 400))
        self.assertEqual(
            self.app.region,
            P.region_rect((100, 50, 1600, 900), (700, 400),
                          self.app.region_scale, P.REGION_ANCHOR))

    def _start_with_cursor(self, cursor, rect=(100, 50, 1600, 900),
                           foreground=300):
        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = cursor
            return True

        with patch.object(P, 'client_rect', return_value=rect), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=foreground), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self.app.start()

    def test_same_pad_point_maps_to_the_same_spot_after_repeated_holds(self):
        """The region is fixed: repeated holds never let the ink drift."""
        self.app.state = 'idle'
        self._start_with_cursor((700, 400))
        first_region = self.app.region
        first = self.app.map(500, 300)
        for _ in range(2):
            self.app.state = 'idle'
            self._start_with_cursor((700, 400))
            self.assertEqual(self.app.region, first_region)
            self.assertEqual(self.app.map(500, 300), first)

    def test_region_recenters_at_the_pointer_on_each_hold(self):
        self.app.state = 'idle'
        self._start_with_cursor((700, 400))
        first_region = self.app.region
        self.app.state = 'idle'
        self._start_with_cursor((1100, 700))
        self.assertEqual(self.app.region,
                         P.region_rect(self.app.rect, (1100, 700),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        self.assertNotEqual(self.app.region, first_region)
        self.assertEqual(self.app.map(500, 300),
                         P.pad_to_screen(self.app.region, 500, 300))

    def test_release_puts_the_pointer_back_where_the_hold_started(self):
        self.app.state = 'idle'
        self._start_with_cursor((700, 400))
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('stopped', 'Alt：松开')
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual(calls[-1][0], move)
        self.assertEqual(calls[-1][1:],
                         P.absolute(700, 400, 0, 0, 2560, 1600))
        self.assertIsNone(self.app.restore_pointer)

    def test_new_target_or_client_reanchors_the_region(self):
        self.app.state = 'idle'
        self._start_with_cursor((700, 400))
        first_region = self.app.region
        # Same pointer and size, different window: anchor again.
        self.app.state = 'idle'
        self._start_with_cursor((700, 400), foreground=301)
        self.assertEqual(self.app.target, 301)
        self.assertEqual(self.app.region,
                         P.region_rect(self.app.rect, (700, 400),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        self.assertNotEqual(self.app.region_target, 300)
        # Same window and pointer, but the client moved/resized: anchor too.
        self.app.state = 'idle'
        self._start_with_cursor((700, 400), foreground=301,
                                rect=(120, 70, 1500, 850))
        self.assertEqual(self.app.region,
                         P.region_rect((120, 70, 1500, 850), (700, 400),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        self.assertNotEqual(first_region, self.app.region)

    def test_start_falls_back_to_monitor_under_cursor(self):
        self.app.state = 'idle'
        with patch.object(P, 'client_rect', return_value=None), \
             patch.object(P, 'monitor_rect', return_value=(100, 50, 900, 700)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos'):
            self.app.start()
        self.assertEqual(self.app.rect, (100, 50, 900, 700))
        self.assertEqual(self.app.state, 'starting')

    def test_start_without_target_is_ignored(self):
        self.app.state = 'idle'
        with patch.object(P.U, 'GetForegroundWindow', return_value=0):
            self.app.start()
        self.assertEqual(self.app.state, 'idle')
        self.assertEqual(self.app.worker.commands, [])
        with patch.object(P.U, 'GetForegroundWindow', return_value=self.app.hwnd):
            self.app.start()
        self.assertEqual(self.app.worker.commands, [])

    def test_start_ignores_minimized_window(self):
        self.app.state = 'idle'
        with patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=True):
            self.app.start()
        self.assertEqual(self.app.worker.commands, [])

    def test_started_opens_session_and_waits_for_release(self):
        self.app.state = 'starting'
        self.app.expect = 'start'
        with patch.object(P.U, 'GetAsyncKeyState', return_value=0x8000):
            self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.expect, 'cancel')
        self.assertTrue(self.app.session is not None)
        self.assertEqual(self.app.session.prefix, 'ppt')
        self.assertEqual(self.app.worker.commands, [])

    def test_started_cancels_office_alt_menu_mode(self):
        self.app.state = 'starting'
        self.app.target = 100
        with patch.object(P.U, 'GetAsyncKeyState', return_value=0x8000), \
             patch.object(P.U, 'IsWindow', return_value=True), \
             patch.object(P.U, 'PostMessageW') as post:
            self.app.event('started', {'path': Path('fake_note.bin')})
        post.assert_called_once_with(100, 0x001F, 0, 0)

    def test_started_for_stale_request_releases(self):
        self.app.state = 'idle'
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.assertEqual(self.app.worker.commands, ['release'])
        self.assertIsNone(self.app.session)

    def test_started_releases_when_alt_already_up(self):
        self.app.state = 'starting'
        with patch.object(P.U, 'GetAsyncKeyState', return_value=0):
            self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_stroke_is_a_left_button_drag(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 0, 0)]))
            self.app.event('frame', frame(0.02, [(7, 666, 414)]))
            self.app.event('frame', frame(0.04, [(7, 1332, 828)]))
            self.app.event('frame', frame(0.06, [(7, 1332, 828)]))
            self.assertTrue(self.app.pressed)
            self.app.event('stopped', 'Alt：松开')
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual([c[0] for c in calls],
                         [move, P.MOUSEEVENTF_LEFTDOWN,
                          move, move, P.MOUSEEVENTF_LEFTUP])
        self.assertEqual(calls[0][1:], (0, 0))
        self.assertEqual(calls[-2][1:], (P.absolute(1332, 828, 0, 0, 1332, 828)))
        self.assertFalse(self.app.pressed)

    def test_duplicate_screen_pixel_is_not_reinjected(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 100, 100)]))
            self.app.event('frame', frame(0.02, [(7, 100, 100)]))
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual([c[0] for c in calls],
                         [move, P.MOUSEEVENTF_LEFTDOWN])

    def test_one_frame_dropout_keeps_the_button_down(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.000, [(7, 100, 100)]))
            self.app.event('frame', frame(0.007, []))
            self.app.event('frame', frame(0.014, [(7, 120, 100)]))
            self.assertFalse(self.app.expire_if_stale(
                self.app.last_frame_wall + 0.03))
        self.assertNotIn(P.MOUSEEVENTF_LEFTUP, [c[0] for c in calls])
        self.assertTrue(self.app.pressed)

    def test_fast_lift_and_retouch_is_two_strokes(self):
        # Fast handwriting lifts and re-touches within one report interval;
        # the pad must lift and press again instead of dragging a connector.
        self._capture()
        calls = []

        def contact(t, tip, x=10, y=10):
            return {'t': t, 'contacts': [
                {'id': 7, 'x': x, 'y': y, 'tip': tip, 'confidence': True}]}

        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', contact(0.000, True))
            self.app.event('frame', contact(0.020, False, 700, 400))
            self.app.event('frame', contact(0.030, True, 50, 60))
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual([c[0] for c in calls],
                         [move, P.MOUSEEVENTF_LEFTDOWN,
                          P.MOUSEEVENTF_LEFTUP,
                          move, P.MOUSEEVENTF_LEFTDOWN])
        self.assertEqual(self.app.active_id, 7)
        self.assertTrue(self.app.pressed)

    def test_moves_are_throttled_and_the_last_point_is_flushed(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.000, [(7, 100, 100)]))
            self.app.event('frame', frame(0.007, [(7, 110, 100)]))
            self.app.event('frame', frame(0.014, [(7, 120, 100)]))
            self.app.event('stopped', 'Alt：松开并识别')
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual([c[0] for c in calls],
                         [move, P.MOUSEEVENTF_LEFTDOWN, move, move,
                          P.MOUSEEVENTF_LEFTUP])
        expected = P.absolute(120, 100, 0, 0, 1332, 828)
        self.assertEqual(calls[-2][1:], expected)

    def test_smoothing_lags_but_release_sends_the_true_end(self):
        self._capture()
        self.app.smooth_alpha = 0.5
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 0, 0)]))
            self.app.event('frame', frame(0.02, [(7, 1332, 828)]))
            self.app.event('stopped', 'Alt：松开并识别')
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        self.assertEqual([c[0] for c in calls],
                         [move, P.MOUSEEVENTF_LEFTDOWN, move, move,
                          P.MOUSEEVENTF_LEFTUP])
        # The live move is eased half-way, the release is the raw end point.
        self.assertEqual(calls[2][1:],
                         P.absolute(666, 414, 0, 0, 1332, 828))
        self.assertEqual(calls[-2][1:],
                         P.absolute(1332, 828, 0, 0, 1332, 828))

    def test_min_step_debounces_tiny_jitter(self):
        self._capture()
        self.app.min_step = 3.0
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 100, 100)]))
            self.app.event('frame', frame(0.02, [(7, 101, 100)]))
            self.app.event('frame', frame(0.04, [(7, 102, 100)]))
            self.app.event('frame', frame(0.06, [(7, 104, 100)]))
            self.app.event('stopped', 'Alt：松开并识别')
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        moves = [c for c in calls if c[0] == move]
        self.assertEqual(len(moves), 2)  # press + the one move over 3 px
        self.assertEqual(moves[-1][1:],
                         P.absolute(104, 100, 0, 0, 1332, 828))
        self.assertEqual(calls[-1], (P.MOUSEEVENTF_LEFTUP,))

    def test_new_stroke_restarts_the_filter(self):
        self._capture()
        self.app.smooth_alpha = 0.5
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 1000, 800)]))
            self.app.event('frame', frame(0.02, [(7, 0, 0)]))
            self.app.event('frame', frame(0.04, [(7, 500, 400)]))
            self.app.event('frame', frame(0.30, [(7, 500, 400)]))
        move = P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE \
            | P.MOUSEEVENTF_VIRTUALDESK
        moves = [c for c in calls if c[0] == move]
        # Second press starts exactly on the new point, no eased carry-over.
        self.assertEqual(moves[-1][1:],
                         P.absolute(500, 400, 0, 0, 1332, 828))
        self.assertEqual([c for c in calls if c[0] == P.MOUSEEVENTF_LEFTDOWN]
                         .__len__(), 2)

    def test_release_lifts_button_when_contact_leaves(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 10, 10)]))
            self.app.event('frame', frame(0.02, []))
            self.assertTrue(self.app.expire_if_stale(
                self.app.last_frame_wall + 0.2))
        self.assertEqual(calls[-1], (P.MOUSEEVENTF_LEFTUP,))
        self.assertIsNone(self.app.active_id)
        self.assertFalse(self.app.pressed)

    def test_tracker_gap_releases_and_presses_again(self):
        self._capture()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 10, 10)]))
            self.app.event('frame', frame(0.30, [(7, 10, 10)]))
        self.assertEqual([c[0] for c in calls],
                         [P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE
                          | P.MOUSEEVENTF_VIRTUALDESK,
                          P.MOUSEEVENTF_LEFTDOWN,
                          P.MOUSEEVENTF_LEFTUP,
                          P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE
                          | P.MOUSEEVENTF_VIRTUALDESK,
                          P.MOUSEEVENTF_LEFTDOWN])
        self.assertTrue(self.app.pressed)

    def test_resting_second_contact_never_zooms_or_steals_the_ink(self):
        # The pad reports a palm/wrist like a tip: two contacts are normal
        # while writing. The still one must not zoom or become the stroke.
        self._capture()
        calls = []
        # Keep the patch over every frame: tests must never inject input.
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 100, 100)]))
            for i, (x, y) in enumerate([(110, 104), (120, 110),
                                        (130, 118), (140, 126)]):
                # palm listed first, as the pad happens to report it
                self.app.event('frame', frame(
                    0.02 + i * 0.02, [(9, 700, 700), (7, x, y)]))
            self.assertTrue(self.app.pressed)
            self.assertEqual(self.app.active_id, 7)
            self.assertFalse(self.app.zooming)
            self.assertEqual(self.app.region_scale, P.REGION_SCALE)
            self.assertIsNone(self.app.frame)
            # Lifting the pen while the palm rests must not dot the palm.
            calls.clear()
            self.app.event('frame', frame(0.20, [(9, 700, 700)]))
            self.assertEqual(calls, [(P.MOUSEEVENTF_LEFTUP,)])
            self.assertIsNone(self.app.active_id)
            self.assertFalse(self.app.pressed)
            # A new stroke starts on the finger, not on the resting palm.
            calls.clear()
            self.app.event('frame', frame(
                0.30, [(9, 700, 700), (7, 200, 200)]))
            move = (P.MOUSEEVENTF_MOVE | P.MOUSEEVENTF_ABSOLUTE
                    | P.MOUSEEVENTF_VIRTUALDESK)
            self.assertEqual([c[0] for c in calls],
                             [move, P.MOUSEEVENTF_LEFTDOWN])
            # The hand moved from (140,126) to (200,200) while lifted, so
            # the pen is carried by that same delta (scale 1 here). It is
            # the chosen contact, not the resting palm, that drives it.
            self.assertEqual(calls[0][1:],
                             P.absolute(200, 200, 0, 0, 1332, 828))
            self.assertEqual(self.app.active_id, 7)

    def test_two_finger_pinch_zooms_and_lifts_the_stroke(self):
        self._capture()
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 450, 400)]))
            for i, (x1, x2) in enumerate([(470, 530), (465, 535),
                                          (458, 542), (450, 550),
                                          (440, 560)]):
                # both fingers move apart around the same midpoint
                self.app.event('frame', frame(0.02 + i * 0.02,
                                              [(8, x1, 400), (9, x2, 400)]))
        self.assertTrue(self.app.zooming)
        self.assertFalse(self.app.pressed)
        self.assertIn(P.MOUSEEVENTF_LEFTUP, [c[0] for c in calls])
        self.assertLess(self.app.region_scale, 2.0)  # fingers apart: clearer

    def test_a_pair_sweeping_across_the_pad_is_not_a_pinch(self):
        self._capture()
        self.app.region_scale = 2.0
        with patch.object(P, 'send_mouse'):
            for i in range(8):
                self.app.event('frame', frame(
                    0.02 * i, [(1, 300 + i * 20, 300), (2, 420 + i * 20, 300)]))
        self.assertFalse(self.app.zooming)
        self.assertEqual(self.app.region_scale, 2.0)

    def test_a_pinch_takes_over_while_alt_moves_the_region(self):
        self._capture()
        self.app.moving = True
        self.app.worker.suppress = False
        self.app.region_scale = 2.0
        with patch.object(P, 'send_mouse'):
            for i in range(5):
                # both fingers move apart around the same midpoint
                self.app.event('frame', frame(
                    0.02 * i, [(1, 300 - i * 10, 300),
                               (2, 420 + i * 10, 300)]))
        self.assertTrue(self.app.zooming)
        self.assertTrue(self.app.zoom_hold)
        self.assertTrue(self.app.worker.suppress)  # pointer held for zoom
        self.assertLess(self.app.region_scale, 2.0)

        with patch.object(P, 'send_mouse'):
            self.app.event('frame', frame(0.20, [(1, 350, 300)]))
        # Fingers lift: move mode gets the pad back, still without ink.
        self.assertFalse(self.app.zooming)
        self.assertFalse(self.app.zoom_hold)
        self.assertTrue(self.app.moving)
        self.assertFalse(self.app.worker.suppress)

    def test_stopped_closes_session_and_releases(self):
        self._capture()
        session = self.app.session
        self.app.pressed = True
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('stopped', 'Alt：松开')
        self.assertEqual(calls, [(P.MOUSEEVENTF_LEFTUP,)])
        self.assertTrue(session.closed)
        self.assertIsNone(self.app.session)
        self.assertEqual(self.app.state, 'idle')
        self.assertIsNone(self.app.target)
        self.assertIsNone(self.app.worker.target)

    def test_stopped_ends_the_host_menu_mode(self):
        self._capture()
        self.app.target = 100
        with patch.object(P.U, 'IsWindow', return_value=True), \
             patch.object(P.U, 'PostMessageW') as post:
            self.app.event('stopped', 'Alt：松开并识别')
        post.assert_called_once_with(100, 0x001F, 0, 0)

    def test_frames_are_recorded_even_when_idle(self):
        self.app.state = 'starting'
        self.app.session = FakeSession('fake', prefix='ppt')
        self.app.event('frame', frame(0.0, [(1, 5, 5)]))
        self.assertEqual(len(self.app.session.frames), 1)
        self.assertFalse(self.app.pressed)

    def test_cancel_releases_capture(self):
        self._capture()
        self.app.pressed = True
        with patch.object(P, 'send_mouse') as send:
            self.app.cancel()
        send.assert_called_once_with(P.MOUSEEVENTF_LEFTUP)
        self.assertEqual(self.app.worker.commands, ['release'])
        self.assertEqual(self.app.expect, 'cancel')

    def test_clear_annotation_releases_only_our_button(self):
        self._capture()
        self.app.pressed = True
        with patch.object(P, 'send_mouse') as send:
            self.app.clear_annotation()
        send.assert_called_once_with(P.MOUSEEVENTF_LEFTUP)
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_error_releases_button_and_disables_capture(self):
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        self.app.pressed = True
        with patch.object(P, 'send_mouse') as send:
            self.app.event('error', 'capture failed')
        send.assert_called_once_with(P.MOUSEEVENTF_LEFTUP)
        self.assertFalse(self.app.ready)
        self.assertFalse(self.app.pressed)

    def test_alt_hold_starts_after_gate_only_once(self):
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(self.app, 'preview_region'), \
             patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.1)
            self.assertEqual(started, [])
            self.app.poll_alt(10.2)
            self.app.poll_alt(10.3)
        self.assertEqual(started, [1])

    def test_poll_alt_does_not_cancel_while_capturing(self):
        # The worker's Alt key-up ends capture; polling must stay out of it.
        self.app.state = 'capturing'
        self.app.alt_held = True
        with patch.object(P.U, 'GetAsyncKeyState', return_value=0):
            self.app.poll_alt(10.0)
        self.assertEqual(self.app.worker.commands, [])
        self.assertEqual(self.app.state, 'capturing')
        self.assertFalse(self.app.alt_held)

    def test_alt_press_while_capturing_is_ignored(self):
        self.app.state = 'capturing'
        self.app.alt_held = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])
        self.assertEqual(self.app.worker.commands, [])
        self.assertEqual(self.app.state, 'capturing')

    def test_release_event_stops_capture(self):
        self._capture()
        session = self.app.session
        self.app.pressed = True
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('stopped', 'Alt：松开并识别')
        self.assertEqual(calls, [(P.MOUSEEVENTF_LEFTUP,)])
        self.assertTrue(session.closed)
        self.assertEqual(self.app.state, 'idle')

    def test_new_hold_arms_again_after_release(self):
        self.app.state = 'capturing'
        self.app.alt_held = True
        self.app.event('stopped', 'Alt：松开并识别')
        self.assertEqual(self.app.state, 'idle')
        started = []
        self.app.start = lambda: started.append(1)
        self.app.alt_held = False
        self.app.alt_blocked = False
        with patch.object(self.app, 'preview_region'), \
             patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(20.0)   # new Alt press
            self.app.poll_alt(20.5)   # gate passed
        self.assertEqual(started, [1])

    def test_press_down_previews_the_region_before_the_gate(self):
        self.app.state = 'idle'
        self.app.frame_deadline = 0.0
        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos'):
            self._poll(21.0, True)
        self.assertTrue(self.app.frame.visible)
        self.assertEqual(self.app.frame.rect, self.app.region)
        self.assertEqual(self.app.region,
                         P.region_rect(self.app.rect, self.app.anchor,
                                       P.REGION_SCALE, P.REGION_ANCHOR))
        self.assertGreater(self.app.frame_deadline, time.monotonic())
        self.assertEqual(self.app.worker.commands, [])  # still only a preview
        self.assertEqual(self.app.state, 'idle')

    def test_start_keeps_the_preview_flash_deadline(self):
        self.app.state = 'idle'
        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos'):
            self._poll(21.0, True)
            preview_deadline = self.app.frame_deadline
            self._poll(21.5, True)  # held past the gate
        self.assertEqual(self.app.state, 'starting')
        self.assertEqual(self.app.worker.commands, ['start'])
        # Showing it early must not stretch the 0.8 s flash to 1.08 s.
        self.assertEqual(self.app.frame_deadline, preview_deadline)
        self.assertTrue(self.app.frame.visible)

    def test_blocked_press_does_not_preview(self):
        self.app.state = 'idle'
        with patch.object(self.app, 'preview_region') as preview, \
             patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk in (0x12, 0x11) else 0):
            self.app.poll_alt(10.0)
        preview.assert_not_called()

    # ---------- continuous / zoom / HUD ----------
    def _poll(self, now, down):
        with patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if down and vk == 0x12 else 0):
            self.app.poll_alt(now)

    def _double_alt(self, start):
        self._poll(start, True)
        self._poll(start + 0.08, False)
        self._poll(start + 0.20, True)
        self._poll(start + 0.28, False)

    def _enter_continuous(self, start=10.0, cursor=(700, 400)):
        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = cursor
            return True

        self.app.state = 'idle'
        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self._double_alt(start)

    def test_double_alt_enters_continuous(self):
        self._enter_continuous()
        self.assertTrue(self.app.continuous)
        self.assertEqual(self.app.state, 'starting')
        self.assertTrue(self.app.worker.continuous)
        self.assertTrue(self.app.worker.free_hold)
        self.assertTrue(self.app.worker.suppress)
        self.assertEqual(self.app.worker.commands, ['start'])
        self.assertIsNone(self.app.restore_pointer)  # no pointer restoration
        self.assertFalse(self.app.taps.down)

    def test_continuous_alt_frees_the_pointer_then_rebuilds_the_region(self):
        self._enter_continuous(cursor=(700, 400))
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.assertEqual(self.app.state, 'capturing')
        self._poll(20.0, True)   # Alt down: physical pad moves the pointer
        self.assertTrue(self.app.moving)
        self.assertFalse(self.app.worker.suppress)

        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = (900, 500)
            return True

        with patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self._poll(20.2, False)  # Alt up: new region under the pointer
        self.assertFalse(self.app.moving)
        self.assertTrue(self.app.worker.suppress)
        self.assertEqual(self.app.anchor, (900, 500))
        self.assertEqual(self.app.region,
                         P.region_rect(self.app.rect, (900, 500),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))

    def test_double_alt_exits_continuous_in_manual_mode(self):
        self._enter_continuous()
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.app.worker.commands.clear()
        with patch.object(P.U, 'GetCursorPos'):
            self._double_alt(20.0)
        self.assertFalse(self.app.continuous)
        self.assertEqual(self.app.expect, 'cancel')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_a_right_click_leaves_continuous(self):
        self._enter_continuous()
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.app.worker.commands.clear()
        self.app.event('exit_continuous', '右键退出连写')
        self.assertFalse(self.app.continuous)
        self.assertFalse(self.app.worker.continuous)
        self.assertFalse(self.app.worker.free_hold)
        self.assertEqual(self.app.expect, 'cancel')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_a_stale_right_click_notice_does_not_cancel_a_short_stroke(self):
        self.app.continuous = False
        self.app.state = 'capturing'
        self.app.expect = 'cancel'
        self.app.worker.commands.clear()
        self.app.event('exit_continuous', '右键退出连写')
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.worker.commands, [])

    def test_short_hold_after_leaving_continuous_recentres_the_region(self):
        self._enter_continuous(cursor=(700, 400))
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.app.worker.commands.clear()
        with patch.object(P.U, 'GetCursorPos'):
            self._double_alt(20.0)
        self.app.event('stopped', 'Alt：松开')
        self.assertEqual(self.app.state, 'idle')
        self.app.worker.commands.clear()

        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = (1200, 700)
            return True

        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self._poll(21.0, True)
            self._poll(21.5, True)  # held past the start gate
        self.assertEqual(self.app.worker.commands, ['start'])
        self.assertEqual(self.app.state, 'starting')
        self.assertEqual(self.app.anchor, (1200, 700))
        self.assertEqual(self.app.region,
                         P.region_rect((100, 50, 1600, 900), (1200, 700),
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        # The freshly chosen region is visible for a moment (PPT only).
        self.assertTrue(self.app.frame.visible)
        self.assertGreater(self.app.frame_deadline, time.monotonic())

    def test_two_long_alt_presses_leave_continuous(self):
        self._enter_continuous()
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.app.worker.commands.clear()
        with patch.object(P.U, 'GetCursorPos'):
            self._poll(20.0, True)   # start moving
            self._poll(20.30, False)  # held longer than a tap
            self._poll(20.40, True)   # second press-down: leave 连写
        self.assertFalse(self.app.continuous)
        self.assertEqual(self.app.expect, 'cancel')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_auto_views_leave_the_double_exit_to_the_host(self):
        self.app.trigger_enabled = False
        with patch.object(P, 'client_rect', return_value=(100, 50, 1600, 900)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos'):
            self.app.enter_continuous()
        self.app.event('started', {'path': Path('fake_ppt.bin')})
        self.app.worker.commands.clear()
        with patch.object(P.U, 'GetCursorPos'):
            self._double_alt(20.0)
        self.assertTrue(self.app.continuous)
        self.assertNotIn('release', self.app.worker.commands)

    def test_pinch_out_enlarges_the_region_and_the_frame_disappears(self):
        self._capture()
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        with patch.object(P, 'send_mouse'):
            self.app.event('frame', frame(0.00, [(1, 450, 400), (2, 550, 400)]))
            self.app.event('frame', frame(0.02, [(1, 440, 400), (2, 560, 400)]))
            self.app.event('frame', frame(0.04, [(1, 420, 400), (2, 580, 400)]))
            self.assertTrue(self.app.zooming)
            self.assertEqual(self.app.region_scale, 2.0)
            self.assertTrue(self.app.frame.visible)
            self.app.event('frame', frame(0.06, [(1, 340, 400), (2, 660, 400)]))
            self.assertAlmostEqual(self.app.region_scale, 1.0, places=3)
            self.assertEqual(self.app.region, (0, 0, 1000, 800))
            # Fingers lift: the zoom ends and the light-blue frame goes away.
            self.app.event('frame', frame(0.10, [(1, 340, 400)]))
        self.assertFalse(self.app.zooming)
        self.assertFalse(self.app.frame.visible)

    def test_live_pinch_redraws_are_throttled_but_the_scale_stays_exact(self):
        # Raw frames arrive at ~125 Hz; repainting the translucent frame on
        # every one made the pinch stutter. A live zoom keeps the scale exact
        # for every frame and paints the frame at ~33 fps instead.
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        self.app.zooming = True
        self.app.show_frame()
        self.app.frame.shows = 0
        self.app.zoom_show_at = 0.0
        with patch.object(P.time, 'monotonic', return_value=100.0):
            self.app.set_scale(1.8, live=True)
        self.assertEqual(self.app.frame.shows, 1)
        self.assertAlmostEqual(self.app.region_scale, 1.8, places=6)
        # Two more raw frames inside the 30 ms window: the region follows the
        # pinch exactly, but the UI is not repainted again.
        with patch.object(P.time, 'monotonic', return_value=100.01):
            self.app.set_scale(1.6, live=True)
            self.app.set_scale(1.4, live=True)
        self.assertEqual(self.app.frame.shows, 1)
        self.assertAlmostEqual(self.app.region_scale, 1.4, places=6)
        with patch.object(P.time, 'monotonic', return_value=100.04):
            self.app.set_scale(1.2, live=True)
        self.assertEqual(self.app.frame.shows, 2)
        # A wheel notch is a single deliberate step: it paints immediately.
        with patch.object(P.time, 'monotonic', return_value=100.04):
            self.app.set_scale(1.1)
        self.assertEqual(self.app.frame.shows, 3)

    def test_wheel_zooms_and_arms_the_frame_deadline(self):
        self._capture()
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        before = self.app.region
        self.app.event('wheel', 120)
        self.assertAlmostEqual(self.app.region_scale, 2.0 / P.ZOOM_STEP,
                               places=3)
        self.assertNotEqual(self.app.region, before)
        self.assertTrue(self.app.frame.visible)
        self.assertGreater(self.app.frame_deadline, time.monotonic())
        self.app.event('wheel', -120)
        self.assertAlmostEqual(self.app.region_scale, 2.0, places=3)

    def test_a_midstroke_zoom_never_teleports_the_pen(self):
        # Hybrid mapping: the stroke keeps the scale it started with, so a
        # wheel/pinch zoom that re-anchors the region cannot make the pen
        # jump to a new place while the finger is still down.
        self._capture()
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        old_scale = 500.0 / P.PAD_MAX_X
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 300, 300)]))
            self.app.event('frame', frame(0.02, [(7, 400, 300)]))
            self.assertTrue(self.app.pressed)
            self.assertAlmostEqual(self.app.pen_xy[0], 510.15, places=2)
            self.app.set_scale(1.0)  # wheel: the region becomes the slide
            sent = len(calls)
            self.app.event('frame', frame(0.06, [(7, 400, 300)]))
            self.assertEqual(len(calls), sent)  # same finger: no jump
            self.assertAlmostEqual(self.app.pen_xy[0], 510.15, places=2)
            # The frozen stroke scale, not the new whole-slide scale.
            self.assertAlmostEqual(self.app.stroke_scale[0], old_scale,
                                   places=6)
            self.app.event('frame', frame(0.08, [(7, 410, 300)]))
            self.assertEqual(calls[-1][1:],
                             P.absolute(514, 420, 0, 0, 1332, 828))

    def test_the_first_stroke_after_alt_starts_at_the_pointer(self):
        # The frame is built at the mouse position, so the first pen-down
        # must ink there even if the finger lands on the far side of the pad.
        self.app.state = 'idle'
        self._start_with_cursor((900, 500))
        self.assertEqual(self.app.pen_anchor, (900, 500))
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 1100, 700)]))
        self.assertTrue(self.app.pressed)
        self.assertEqual(calls[0][1:],
                         P.absolute(900, 500, 0, 0, 2560, 1600))
        self.assertEqual(calls[1], (P.MOUSEEVENTF_LEFTDOWN,))

    def test_a_later_stroke_follows_the_hand_not_the_pad(self):
        # Pad deltas only: the next character starts at the old ink tip plus
        # however far the hand moved while it was lifted, never at the new
        # pad contact's absolute spot, so it neither chains onto the last
        # stroke nor jumps across the slide. A nearby start keeps the region
        # where it is (no frame jumping on every stroke).
        self.app.state = 'idle'
        self._start_with_cursor((900, 500))
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 300, 300)]))
            self.app.event('frame', frame(0.02, [(7, 900, 700)]))
            end = self.app.pen_xy
            first_region = self.app.region
            self.app.event('frame', frame(0.10, []))  # real lift
            self.assertFalse(self.app.pressed)
            self.assertAlmostEqual(self.app.pen_anchor[0], end[0], places=6)
            self.assertEqual(self.app.pen_pad_end, (900, 700))
            calls.clear()
            # A small, natural hand move: fully applied.
            self.app.event('frame', frame(0.20, [(7, 800, 650)]))
        sx, sy = self.app.stroke_scale
        start = (end[0] + (800 - 900) * sx, end[1] + (650 - 700) * sy)
        self.assertEqual(
            calls[0][1:],
            P.absolute(int(round(start[0])), int(round(start[1])),
                       0, 0, 2560, 1600))
        absolute_map = P.pad_to_screen(first_region, 800, 650)
        self.assertGreater(abs(start[0] - absolute_map[0]), 1.0)
        self.assertEqual(self.app.region, first_region)  # frame stays put
        self.assertEqual(self.app.frame.extra, ())

    def test_a_big_leftward_lift_is_a_hand_reset_not_a_pen_move(self):
        # Writing rightward, the hand eventually returns to its resting spot
        # on the left of the pad. That must not drag the ink back left.
        self.app.state = 'idle'
        self._start_with_cursor((900, 500))
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 600, 300)]))
            self.app.event('frame', frame(0.02, [(7, 1000, 300)]))
            end = self.app.pen_xy
            first_region = self.app.region
            self.app.event('frame', frame(0.10, []))
            calls.clear()
            self.app.event('frame', frame(0.20, [(7, 100, 300)]))
        self.assertEqual(calls[0][1:],
                         P.absolute(int(round(end[0])), int(round(end[1])),
                                    0, 0, 2560, 1600))
        self.assertLess(self.app.pen_xy[0], end[0] + 1)  # no leftward jump
        self.assertEqual(self.app.region, first_region)

    def test_one_lift_can_only_nudge_the_pen(self):
        # A large forward lift is capped; a small one applies in full.
        self.app.pen_anchor = (1000.0, 500.0)
        self.app.stroke_scale = (0.5, 0.5)
        self.app.pen_pad_end = (200, 300)
        start = self.app.stroke_start((1200, 300))
        self.assertAlmostEqual(start[0],
                               1000.0 + P.LIFT_MAX_TRAVEL * 0.5, places=6)
        self.assertAlmostEqual(start[1], 500.0, places=6)
        self.app.pen_pad_end = (250, 320)
        start = self.app.stroke_start((260, 330))
        self.assertAlmostEqual(start[0], 1005.0, places=6)
        self.assertAlmostEqual(start[1], 505.0, places=6)

    def test_the_region_shifts_only_when_the_ink_runs_out_of_room(self):
        # Writing towards the right keeps the frame still until little room
        # is left; then the same-size box moves forward and the old one is
        # kept as an outline (compound frame).
        self.app.state = 'idle'
        self._start_with_cursor((900, 500))
        self.app.state = 'capturing'
        self.app.session = FakeSession('fake', prefix='ppt')
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 100, 100)]))
            self.app.event('frame', frame(0.02, [(7, 1000, 300)]))
            first_region = self.app.region
            end = self.app.pen_xy
            self.app.event('frame', frame(0.10, []))
            calls.clear()
            # Plant the finger back on the same pad point: the start is the
            # old tip, now close to the right edge of the box.
            self.app.event('frame', frame(0.20, [(7, 1000, 300)]))
        self.assertNotEqual(self.app.region, first_region)
        self.assertEqual(self.app.region,
                         P.region_rect(self.app.rect, end,
                                       self.app.region_scale,
                                       P.REGION_ANCHOR))
        self.assertEqual(self.app.frame.extra, (first_region,))

    def test_a_new_anchor_in_continuous_mode_arms_the_pointer_again(self):
        self._capture()
        self.app.rect = (100, 50, 1600, 900)
        self.app.region_scale = 3.0
        self.app.anchor = (300, 300)
        self.app.anchor_region()

        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = (900, 500)
            return True

        with patch.object(P.U, 'GetCursorPos', side_effect=get_cursor), \
             patch.object(P, 'send_mouse'):
            self.app.anchor_at_pointer()
        self.assertEqual(self.app.pen_anchor, (900, 500))
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(7, 60, 760)]))
        self.assertEqual(calls[0][1:],
                         P.absolute(900, 500, 0, 0, 1332, 828))

    def test_a_pen_down_on_the_pad_border_never_inks(self):
        # A wrist resting on the edge is reported exactly like a tip; one
        # that drags along the border used to sweep out a long stroke.
        self._capture()
        self.app.edge_start = (P.EDGE_START_X, P.EDGE_START_Y)
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)), \
             patch('sys.stdout'):
            self.app.event('frame', frame(0.00, [(2, 9, 376)]))
            self.app.event('frame', frame(0.02, [(2, 422, 0)]))
            self.app.event('frame', frame(0.04, [(2, 80, 156)]))
            self.assertFalse(self.app.pressed)
            self.assertIsNone(self.app.active_id)
            self.assertEqual(calls, [])
            self.assertEqual(self.app.edge_ignored, {2})
            # Top border too: palms and cuffs land there as well.
            self.app.event('frame', frame(0.20, [(3, 820, 13)]))
            self.app.event('frame', frame(0.22, [(3, 700, 400)]))
            self.assertFalse(self.app.pressed)
            self.assertEqual(calls, [])

    def test_a_stroke_that_starts_inside_may_run_into_the_margin(self):
        # The gate judges the contact's first point only, so the last
        # centimetres of a normal stroke near the border still draw.
        self._capture()
        self.app.edge_start = (P.EDGE_START_X, P.EDGE_START_Y)
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)):
            self.app.event('frame', frame(0.00, [(4, 300, 300)]))
            self.app.event('frame', frame(0.02, [(4, 1050, 40)]))
            self.assertTrue(self.app.pressed)
            self.assertTrue(calls)
            self.assertEqual(self.app.pen_pad, (1050, 40))

    def test_an_edge_contact_that_later_moves_still_never_starts(self):
        self._capture()
        self.app.edge_start = (P.EDGE_START_X, P.EDGE_START_Y)
        calls = []
        with patch.object(P, 'send_mouse',
                          side_effect=lambda *a: calls.append(a)), \
             patch('sys.stdout'):
            self.app.event('frame', frame(0.00, [(5, 30, 696)]))
            self.app.event('frame', frame(0.02, [(5, 200, 700)]))
            self.app.event('frame', frame(0.04, [(5, 800, 704)]))
            self.assertFalse(self.app.pressed)
            self.assertIsNone(self.app.active_id)
            self.assertEqual(calls, [])
            self.assertEqual(self.app.edge_ignored, {5})
            # A real pen-down inside still works in the same capture.
            self.app.event('frame', frame(0.20, [(6, 400, 400)]))
            self.app.event('frame', frame(0.22, [(6, 500, 400)]))
            self.assertTrue(self.app.pressed)
            self.assertEqual(self.app.active_id, 6)

    def test_a_second_finger_inside_can_start_while_a_palm_rests_on_the_edge(
            self):
        self._capture()
        self.app.edge_start = (P.EDGE_START_X, P.EDGE_START_Y)
        with patch.object(P, 'send_mouse'), patch('sys.stdout'):
            self.app.event('frame', frame(0.00, [(8, 20, 400),
                                                 (9, 400, 400)]))
            self.app.event('frame', frame(0.02, [(8, 22, 402),
                                                 (9, 430, 400)]))
            self.assertEqual(self.app.active_id, 9)
            self.assertTrue(self.app.pressed)

    def test_alt_press_shows_mode_state_inside_the_region_frame(self):
        self.app.state = 'idle'
        self.app.region = (10, 20, 300, 180)
        with patch.object(self.app, 'preview_region'), \
             patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
        # PPT has no separate card any more; the frame carries the state.
        self.assertIsNone(self.app.hud)
        self.assertTrue(self.app.frame.visible)
        lines = self.app.frame.status
        self.assertIn('PPT 墨迹 · 准备写字', lines)
        self.assertTrue(any(line.startswith('区域 300×180')
                            for line in lines))
        self.assertIn('双按 Alt 连写 · 捏合/滚轮缩放', lines)
        self.assertGreater(self.app.frame_deadline, time.monotonic())

    def test_the_press_down_preview_frame_shows_the_state_itself(self):
        # Auto mode never runs poll_alt's show_status: the frame is built by
        # the host, so the frame alone has to carry the state.
        self.app.state = 'idle'

        def get_cursor(pointer):
            target = C.cast(pointer, C.POINTER(W.POINT)).contents
            target.x, target.y = (700, 400)
            return True

        with patch.object(P, 'client_rect',
                          return_value=(100, 50, 1600, 900)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.U, 'GetCursorPos', side_effect=get_cursor):
            self.assertTrue(self.app.preview_region())
        self.assertTrue(self.app.frame.visible)
        self.assertIn('PPT 墨迹 · 准备写字', self.app.frame.status)

    def test_a_capture_frame_shows_the_state_as_it_appears(self):
        self.app.state = 'idle'
        self._start_with_cursor((900, 500))
        self.assertTrue(self.app.frame.visible)
        self.assertIn('PPT 墨迹 · 准备写字', self.app.frame.status)

    def test_a_wheel_zoom_frame_reports_the_zoom_state(self):
        self._capture()
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        self.app.event('wheel', 120)
        self.assertTrue(self.app.frame.visible)
        self.assertIn('PPT 墨迹 · 区域缩放', self.app.frame.status)

    def test_a_pinch_zoom_frame_reports_the_zoom_state(self):
        self._capture()
        self.app.rect = (0, 0, 1000, 800)
        self.app.region_scale = 2.0
        self.app.anchor = (500, 400)
        self.app.anchor_region()
        with patch.object(P, 'send_mouse'):
            self.app.event('frame', frame(0.00, [(1, 450, 400),
                                                 (2, 550, 400)]))
            self.app.event('frame', frame(0.02, [(1, 440, 400),
                                                 (2, 560, 400)]))
            self.app.event('frame', frame(0.04, [(1, 420, 400),
                                                 (2, 580, 400)]))
        self.assertTrue(self.app.zooming)
        self.assertTrue(self.app.frame.visible)
        self.assertIn('PPT 墨迹 · 区域缩放', self.app.frame.status)

    def test_alt_with_ctrl_never_starts(self):
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(
                P.U, 'GetAsyncKeyState',
                side_effect=lambda vk: 0x8000 if vk in (0x12, 0x11) else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_alt_while_not_idle_never_restarts(self):
        self.app.state = 'starting'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_disabled_trigger_never_starts_on_its_own(self):
        self.app.trigger_enabled = False
        self.app.alt_held = False
        self.app.auto_pending = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_auto_pending_starts_without_the_hold_gate(self):
        self.app.trigger_enabled = False
        self.app.auto_pending = True
        self.app.alt_held = True
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(P.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x12 else 0):
            self.app.poll_alt(10.0)
        self.assertEqual(started, [1])
        self.assertFalse(self.app.auto_pending)

    def test_only_process_guard_refuses_another_foreground(self):
        self.app.only_process = 'onenote'
        self.app.state = 'idle'
        with patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.auto_mode, 'process_matches', return_value=False):
            self.app.start()
        self.assertEqual(self.app.state, 'idle')
        self.assertEqual(self.app.worker.commands, [])

    def test_only_process_guard_accepts_the_expected_app(self):
        self.app.only_process = 'onenote'
        self.app.state = 'idle'
        with patch.object(P, 'client_rect', return_value=(10, 20, 800, 600)), \
             patch.object(P, 'virtual_screen', return_value=(0, 0, 2560, 1600)), \
             patch.object(P.U, 'GetForegroundWindow', return_value=300), \
             patch.object(P.U, 'IsIconic', return_value=False), \
             patch.object(P.auto_mode, 'process_matches',
                          return_value=True) as matches:
            self.app.start()
        self.assertEqual(self.app.state, 'starting')
        self.assertEqual(self.app.worker.commands, ['start'])
        matches.assert_called_once_with(300, 'onenote')

    def test_session_prefix_follows_the_view(self):
        self.app.prefix = 'note'
        self.app.state = 'starting'
        with patch.object(P.U, 'GetAsyncKeyState', return_value=0x8000):
            self.app.event('started', {'path': Path('fake_note.bin')})
        self.assertEqual(self.app.session.prefix, 'note')

    def test_safe_preview_never_captures_or_injects(self):
        with patch.object(P, 'LiveInput', FakeWorker), \
             patch.object(P.U, 'GetAsyncKeyState', return_value=0x8000):
            app = P.PptInk(tk.Toplevel(self.root), safe_preview=True)
        try:
            with patch.object(P.U, 'GetForegroundWindow', return_value=300), \
                 patch.object(P.U, 'IsIconic', return_value=False), \
                 patch.object(P, 'send_mouse') as send:
                app.poll_alt(10.0)
                app.poll_alt(10.5)
                app.start()
            self.assertEqual(app.state, 'idle')
            self.assertEqual(app.worker.commands, [])
            send.assert_not_called()
        finally:
            app.close()


@unittest.skipUnless(sys.platform == 'win32', 'Win32 raw input host')
class NoteInkTests(unittest.TestCase):
    def test_note_is_onenote_only_and_uses_its_own_prefix(self):
        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.withdraw()
        with patch.object(P, 'LiveInput', FakeWorker), \
             patch.dict('os.environ', {'TOUCHPAD_INK_REGION': '3'}):
            app = P.NoteInk(win)
        try:
            self.assertEqual(app.kind, 'note')
            self.assertEqual(app.prefix, 'note')
            self.assertEqual(app.only_process, A.ONENOTE_PROCESS)
            self.assertEqual(app.region_scale, 1.0)  # never zooms the page
            self.assertEqual(app.edge_start, (0.0, 0.0))  # page edges are fine
            self.assertFalse(app.reanchor_strokes)  # page stays absolute
            self.assertTrue(app.trigger_enabled)
            self.assertFalse(app.pressed)
        finally:
            app.close()
            try:
                root.destroy()
            except tk.TclError:
                pass


class AltHoldBeginTests(unittest.TestCase):
    """begin() must refuse an armed session when Alt is no longer down."""

    def test_begin_requires_physical_alt(self):
        events = queue.Queue()
        worker = L.LiveInput(events, 123, start_hotkey=False, alt_hold=True,
                             alt_releases=False, alt_stops=True)
        worker.focused = lambda: True
        with patch.object(L.U, 'GetAsyncKeyState', return_value=0):
            worker.begin()
        self.assertFalse(worker.guard.active)
        kind, text = events.get_nowait()
        self.assertEqual(kind, 'notice')
        self.assertIn('Alt', text)


class HookPassThroughTests(unittest.TestCase):
    """Our own SendInput strokes must survive the physical-mouse hook."""

    def setUp(self):
        self.worker = L.LiveInput(queue.Queue(), 123)
        self.worker.safety = lambda: None
        self.worker.guard.active = True

    def test_injected_event_passes_through(self):
        info = L.MSLLHOOKSTRUCT()
        info.flags = L.LLMHF_INJECTED
        with patch.object(L.U, 'CallNextHookEx',
                          return_value=99) as next_hook:
            result = self.worker._mouse(0, 0x200, C.cast(
                C.byref(info), C.c_void_p).value)
        self.assertEqual(result, 99)
        next_hook.assert_called_once()

    def test_physical_event_is_swallowed(self):
        info = L.MSLLHOOKSTRUCT()
        with patch.object(L.U, 'CallNextHookEx') as next_hook:
            result = self.worker._mouse(0, 0x200, C.cast(
                C.byref(info), C.c_void_p).value)
        self.assertEqual(result, 1)
        next_hook.assert_not_called()

    def test_right_click_in_continuous_reaches_the_app_and_exits(self):
        self.worker.continuous = True
        info = L.MSLLHOOKSTRUCT()
        pointer = C.cast(C.byref(info), C.c_void_p).value
        with patch.object(L.U, 'CallNextHookEx', return_value=99) as next_hook:
            down = self.worker._mouse(0, L.WM_RBUTTONDOWN, pointer)
            up = self.worker._mouse(0, L.WM_RBUTTONUP, pointer)
        self.assertEqual((down, up), (99, 99))
        self.assertEqual(next_hook.call_count, 2)
        self.assertEqual(self.worker.events.get_nowait(),
                         ('exit_continuous', '右键退出连写'))
        self.assertTrue(self.worker.events.empty())
        self.assertFalse(self.worker.right_pass)

    def test_the_release_still_passes_after_continuous_exits(self):
        self.worker.continuous = True
        info = L.MSLLHOOKSTRUCT()
        pointer = C.cast(C.byref(info), C.c_void_p).value
        with patch.object(L.U, 'CallNextHookEx', return_value=99):
            self.worker._mouse(0, L.WM_RBUTTONDOWN, pointer)
        # The view handles the notice and drops 连写 before the release.
        self.worker.continuous = False
        with patch.object(L.U, 'CallNextHookEx', return_value=99) as next_hook:
            up = self.worker._mouse(0, L.WM_RBUTTONUP, pointer)
        self.assertEqual(up, 99)
        next_hook.assert_called_once()
        self.assertFalse(self.worker.right_pass)
        # A fresh right click outside 连写 is still swallowed as before.
        with patch.object(L.U, 'CallNextHookEx') as next_hook:
            again = self.worker._mouse(0, L.WM_RBUTTONDOWN, pointer)
        self.assertEqual(again, 1)
        next_hook.assert_not_called()

    def test_ctrl_wheel_never_zooms_the_slide_while_moving(self):
        self.worker.suppress = False  # 连写 move mode
        info = L.MSLLHOOKSTRUCT()
        info.mouseData = 120 << 16
        pointer = C.cast(C.byref(info), C.c_void_p).value
        with patch.object(L.U, 'GetAsyncKeyState', return_value=0x8000), \
             patch.object(L.U, 'CallNextHookEx') as next_hook:
            result = self.worker._mouse(0, 0x020A, pointer)
        self.assertEqual(result, 1)
        next_hook.assert_not_called()
        self.assertTrue(self.worker.events.empty())  # no wheel to the region

    def test_plain_wheel_still_scrolls_while_moving(self):
        self.worker.suppress = False
        info = L.MSLLHOOKSTRUCT()
        info.mouseData = 120 << 16
        pointer = C.cast(C.byref(info), C.c_void_p).value
        with patch.object(L.U, 'GetAsyncKeyState', return_value=0), \
             patch.object(L.U, 'CallNextHookEx', return_value=99) as next_hook:
            result = self.worker._mouse(0, 0x020A, pointer)
        self.assertEqual(result, 99)
        next_hook.assert_called_once()

    def test_release_when_not_capturing_passes_everything(self):
        self.worker.guard.active = False
        with patch.object(L.U, 'CallNextHookEx', return_value=7):
            self.assertEqual(self.worker._mouse(0, 0x200, 0), 7)

    def test_alt_release_can_keep_capture_armed(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=False,
                             alt_stops=False)
        worker.guard.active = True
        virtual_key = C.c_uint(0x12)
        with patch.object(L.U, 'CallNextHookEx', return_value=5):
            result = worker._keyboard(0, 0x101, C.cast(
                C.byref(virtual_key), C.c_void_p).value)
        self.assertEqual(result, 5)
        self.assertTrue(worker.guard.active)

    def test_alt_release_stops_by_default(self):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.guard.active = True
        virtual_key = C.c_uint(0x12)
        with patch.object(L.U, 'CallNextHookEx', return_value=5):
            worker._keyboard(0, 0x101, C.cast(
                C.byref(virtual_key), C.c_void_p).value)
        self.assertFalse(worker.guard.active)


if __name__ == '__main__':
    unittest.main()
