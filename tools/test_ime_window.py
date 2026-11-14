import sys
import time
import tkinter as tk
import traceback
from pathlib import Path
import unittest
from unittest.mock import patch

import ime_window as I
from test_windows_ink import first_character

BASE = Path(__file__).resolve().parent.parent


class PlacementTests(unittest.TestCase):
    def test_below_caret(self):
        x, y = I.place_window(500, 300, 20, 300, 230, (0, 0, 1920, 1080))
        self.assertEqual((x, y), (440, 310))

    def test_flips_above_near_bottom(self):
        x, y = I.place_window(500, 1000, 20, 300, 230, (0, 0, 1920, 1080))
        self.assertEqual((x, y), (440, 740))

    def test_clamps_at_right_and_top(self):
        self.assertEqual(I.place_window(1900, 300, 20, 300, 230, (0, 0, 1920, 1080))[0], 1616)
        # 10 px below the caret still fits, so only the horizontal edge clamps.
        self.assertEqual(I.place_window(10, 8, 20, 300, 230, (0, 0, 1920, 1080))[1], 18)
        # A caret above the work area clamps to the top margin.
        self.assertEqual(I.place_window(10, -40, 20, 300, 230, (0, 0, 1920, 1080))[1], 4)

    def test_tiny_work_area_keeps_origin(self):
        x, y = I.place_window(1900, 1000, 20, 300, 230, (0, 0, 200, 150))
        self.assertEqual((x, y), (4, 4))


class CaretPointTests(unittest.TestCase):
    def test_uia_caret_wins_when_win32_has_none(self):
        # Chromium/Electron editors expose no Win32 caret; the pad used to
        # stay at the mouse while the real caret advanced (reported drift).
        with patch.object(I, 'GetGUIThreadInfo', return_value=False), \
             patch.object(I.auto_mode, 'caret_bounds',
                          return_value=(750, 1332, 26)) as bounds:
            self.assertEqual(I.caret_point(123, 18), (750, 1332, 26))
        bounds.assert_called_once_with(123)

    def test_cursor_is_the_last_resort(self):
        def cursor(pointer):
            pointer._obj.x = 640
            pointer._obj.y = 480
            return 1

        with patch.object(I, 'GetGUIThreadInfo', return_value=False), \
             patch.object(I.auto_mode, 'caret_bounds', return_value=None), \
             patch.object(I, 'U') as u:
            u.GetCursorPos.side_effect = cursor
            self.assertEqual(I.caret_point(123, 18), (640, 480, 18))

    def test_uia_failure_also_falls_back_to_the_cursor(self):
        def cursor(pointer):
            pointer._obj.x = 5
            pointer._obj.y = 6
            return 1

        with patch.object(I, 'GetGUIThreadInfo', return_value=False), \
             patch.object(I.auto_mode, 'caret_bounds',
                          side_effect=OSError('uia')), \
             patch.object(I, 'U') as u:
            u.GetCursorPos.side_effect = cursor
            self.assertEqual(I.caret_point(123, 18), (5, 6, 18))


class InkTrimTests(unittest.TestCase):
    class FakeCanvas:
        def __init__(self):
            self.items = {'ink': list(range(10000)),
                          'halo': list(range(100000, 112000))}

        def find_withtag(self, tag):
            return tuple(self.items[tag])

        def delete(self, item):
            for tag in self.items:
                if item in self.items[tag]:
                    self.items[tag].remove(item)

    def test_halo_is_bounded_together_with_ink(self):
        # Only 'ink' used to be trimmed, so 'halo' grew without limit.
        app = I.ImeWindow.__new__(I.ImeWindow)
        app.canvas = self.FakeCanvas()
        app.trim_ink()
        self.assertEqual(len(app.canvas.find_withtag('ink')), 4000)
        self.assertEqual(len(app.canvas.find_withtag('halo')), 4000)


class CandidateTests(unittest.TestCase):
    def test_first_result_candidates_with_tail(self):
        payload = {'results': [
            {'candidates': ['手', '龵', '丰'], 'top': '手'},
            {'candidates': ['写'], 'top': '写'}]}
        self.assertEqual(I.candidate_entries(payload), ['手写', '龵写', '丰写'])

    def test_dedupes_and_limits(self):
        payload = {'results': [{'candidates': ['a', 'a', 'b', 'c', 'd', 'e', 'f', 'g']}]}
        self.assertEqual(I.candidate_entries(payload), ['a', 'b', 'c', 'd', 'e', 'f', 'g'][:7])

    def test_empty(self):
        self.assertEqual(I.candidate_entries({}), [])
        self.assertEqual(I.candidate_entries({'results': []}), [])


class InputTests(unittest.TestCase):
    def test_unicode_units(self):
        self.assertEqual(I.unicode_inputs('手'), [(0x624B, I.KEYEVENTF_UNICODE),
                                                 (0x624B, I.KEYEVENTF_UNICODE | I.KEYEVENTF_KEYUP)])
        self.assertEqual(len(I.unicode_inputs('😀')), 4)  # surrogate pair

    def test_send_unicode_batch(self):
        with patch.object(I, 'U') as u:
            u.SendInput.return_value = 2
            self.assertTrue(I.send_unicode('手'))
            self.assertEqual(u.SendInput.call_args[0][0], 2)
        with patch.object(I, 'U') as u:
            self.assertTrue(I.send_unicode(''))
            u.SendInput.assert_not_called()

    def test_send_unicode_failure_is_reported(self):
        with patch.object(I, 'U') as u:
            u.SendInput.return_value = 0
            self.assertFalse(I.send_unicode('手'))


class StaleHideTests(unittest.TestCase):
    class Fake:
        def __init__(self):
            self.hide_token = 0
            self.hidden = 0
            self.root = self

        def after(self, delay, callback):
            self.callback = callback

        def hide(self):
            self.hidden += 1
            self.hide_token += 1

    def test_timer_hides_only_once(self):
        app = self.Fake()
        I.ImeWindow.schedule_hide(app)
        app.callback()
        self.assertEqual(app.hidden, 1)

    def test_newer_show_cancels_pending_hide(self):
        app = self.Fake()
        I.ImeWindow.schedule_hide(app)
        app.hide_token += 1  # a new show() happened after scheduling
        app.callback()
        self.assertEqual(app.hidden, 0)


class StopReasonTests(unittest.TestCase):
    def test_explicit_and_alt_recognize(self):
        self.assertTrue(I.should_recognize('鼠标已释放', 'recognize'))
        self.assertTrue(I.should_recognize('Alt：鼠标已释放', 'start'))
        # Real-time candidates already exist: the release keeps them.
        self.assertTrue(I.should_recognize('鼠标已释放', 'keep'))

    def test_safety_and_escape_do_not(self):
        for reason in ('Esc：取消并释放', '窗口失去焦点：鼠标已释放',
                       '界面响应超时：鼠标已释放', '90 秒无触控输入：鼠标已释放',
                       '单次达到 10 分钟：已停止并释放', '窗口关闭：鼠标已释放',
                       '输入异常：鼠标已释放'):
            self.assertFalse(I.should_recognize(reason, 'recognize'), reason)
        self.assertFalse(I.should_recognize('鼠标已释放', 'cancel'))


class FakeOverlay:
    target = 100
    continuous = False
    focus_element = None

    def __init__(self):
        self.hidden = False
        self.copied = None

    def hide(self):
        self.hidden = True

    def copy(self, text):
        self.copied = text


class InsertionTests(unittest.TestCase):
    def _choose(self, *, window=True, foreground=100, sent=True,
                focus=None):
        overlay = FakeOverlay()
        with patch.object(I, 'U') as u, \
             patch.object(I, 'send_unicode', return_value=sent) as send, \
             patch.object(I, 'input_log'), \
             patch.object(I, 'focus_ready',
                          return_value=focus or I.FOCUS_FOCUSED) as ready, \
             patch.object(I, 'describe_window', return_value='fake'), \
             patch.object(I.time, 'sleep'):
            u.IsWindow.return_value = window
            if isinstance(foreground, tuple):
                # choose() reads the foreground before and after refocusing
                # and on both sides of the SendInput call.
                u.GetForegroundWindow.side_effect = list(foreground) + [
                    foreground[-1]] * 4
            else:
                u.GetForegroundWindow.return_value = foreground
            I.ImeWindow.choose(overlay, '手')
        return overlay, send, u, ready

    def test_inserts_into_focused_target(self):
        overlay, send, u, _ready = self._choose()
        self.assertTrue(overlay.hidden)
        self.assertIsNone(overlay.copied)
        # Alt hold may leave the target in menu mode; cancel it before typing.
        u.PostMessageW.assert_called_once_with(100, 0x001F, 0, 0)
        send.assert_called_once_with('手')

    def test_alt_focus_loss_is_restored_before_insert(self):
        # Normal browser windows move focus to the toolbar on Alt release;
        # the saved field is focused again and the text goes in.
        overlay, send, _u, ready = self._choose(focus=I.FOCUS_RESTORED)
        ready.assert_called_once_with(100, overlay.focus_element)
        send.assert_called_once_with('手')
        self.assertIsNone(overlay.copied)

    def test_unrestorable_focus_copies_instead_of_typing(self):
        overlay, send, _u, _ready = self._choose(focus=I.FOCUS_LOST)
        send.assert_not_called()
        self.assertEqual(overlay.copied, '手')

    def test_no_saved_element_still_types(self):
        # QQ exposes only a Group yet accepts the keys; keep that behaviour.
        overlay, send, _u, _ready = self._choose(focus=I.FOCUS_UNKNOWN)
        send.assert_called_once_with('手')
        self.assertIsNone(overlay.copied)

    def test_missing_target_falls_back_to_clipboard(self):
        overlay, send, _u, _ready = self._choose(window=False)
        self.assertEqual(overlay.copied, '手')
        send.assert_not_called()

    def test_refocuses_before_insert(self):
        overlay, send, u, _ready = self._choose(foreground=(200, 100))
        u.SetForegroundWindow.assert_called_once_with(100)
        send.assert_called_once_with('手')

    def test_failed_refocus_copies(self):
        overlay, send, _u, _ready = self._choose(foreground=(200, 200))
        self.assertEqual(overlay.copied, '手')
        send.assert_not_called()

    def test_injection_failure_copies(self):
        overlay, send, _u, _ready = self._choose(sent=False)
        self.assertEqual(overlay.copied, '手')


class FocusReadyTests(unittest.TestCase):
    """Alt release in a browser moves focus to the toolbar; the field must
    be focused and verified again before the chosen text is typed."""

    def _ready(self, *, current=None, saved=None, set_ok=True, control=0):
        with patch.object(I.auto_mode, 'process_id', return_value=42) as pid, \
             patch.object(I.auto_mode, 'focused_editable',
                          return_value=current) as focused, \
             patch.object(I.auto_mode, 'release_element') as release, \
             patch.object(I.auto_mode, 'set_focus',
                          return_value=set_ok) as setfocus, \
             patch.object(I.auto_mode, 'focus_control_type',
                          return_value=control), \
             patch.object(I.time, 'sleep') as sleep:
            state = I.focus_ready(100, saved)
        return state, pid, focused, release, setfocus, sleep

    def test_current_editable_focus_is_used_as_is(self):
        current = object()
        state, pid, focused, release, setfocus, sleep = self._ready(
            current=current)
        self.assertEqual(state, I.FOCUS_FOCUSED)
        pid.assert_called_once_with(100)
        focused.assert_called_once_with(42)
        release.assert_called_once_with(current)
        setfocus.assert_not_called()
        sleep.assert_not_called()

    def test_nothing_saved_leaves_the_app_alone(self):
        state, _pid, _focused, _release, setfocus, sleep = self._ready()
        self.assertEqual(state, I.FOCUS_UNKNOWN)
        setfocus.assert_not_called()
        sleep.assert_not_called()

    def test_saved_field_is_focused_and_verified(self):
        saved = object()
        state, _pid, _focused, _release, setfocus, sleep = self._ready(
            saved=saved, control=I.auto_mode.UIA_EDIT)
        self.assertEqual(state, I.FOCUS_RESTORED)
        setfocus.assert_called_once_with(saved)
        sleep.assert_called_once_with(I.FOCUS_SETTLE)

    def test_focus_must_land_on_an_editable_element(self):
        state, _pid, _focused, _release, _setfocus, _sleep = self._ready(
            saved=object(), control=50000)  # Button, e.g. the Edge toolbar
        self.assertEqual(state, I.FOCUS_LOST)

    def test_setfocus_failure_is_reported(self):
        state, _pid, _focused, _release, _setfocus, sleep = self._ready(
            saved=object(), set_ok=False)
        self.assertEqual(state, I.FOCUS_LOST)
        sleep.assert_not_called()

    def test_missing_target_is_lost(self):
        state = I.focus_ready(None, None)
        self.assertEqual(state, I.FOCUS_LOST)


@unittest.skipUnless(sys.platform == 'win32', 'Win32 overlay style')
class StyleTests(unittest.TestCase):
    def test_transparent_and_non_activating(self):
        root = tk.Tk()
        root.withdraw()
        try:
            hwnd = I.apply_overlay_style(root)
            self.assertEqual(str(root.wm_attributes('-transparentcolor')).lower(), I.KEY)
            style = I.U.GetWindowLongPtrW(hwnd, I.GWL_EXSTYLE)
            self.assertTrue(style & I.WS_EX_NOACTIVATE)
            self.assertTrue(style & I.WS_EX_TOOLWINDOW)
        finally:
            root.destroy()


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
        self.continuous = False
        self.suppress = True
        self.free_hold = False
        self.guard = type('Guard', (), {'active': False, 'heartbeat': 0.0})()
        self.stopping = __import__('threading').Event()
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
        self.base = Path('fake_ime_session')
        self.frames = []
        self.closed = False
        self.reason = None

    def write(self, frame):
        self.frames.append(frame)

    def close(self, reason):
        self.closed = True
        self.reason = reason


@unittest.skipUnless(sys.platform == 'win32', 'Tk overlay state machine')
class FlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.session_patch = patch.object(I, 'Session', FakeSession)
        self.session_patch.start()
        # Insertion diagnostics touch UIA and append data/ime_input.log; the
        # state machine tests must stay offline.
        self.log_patch = patch.object(I, 'input_log')
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.describe_patch = patch.object(I, 'describe_window',
                                           return_value='fake')
        self.describe_patch.start()
        self.addCleanup(self.describe_patch.stop)
        # Saving the field's element is a UIA read; the state machine tests
        # stay offline and keep no saved element.
        self.focus_patch = patch.object(I.auto_mode, 'focused_editable',
                                        return_value=None)
        self.focus_patch.start()
        self.addCleanup(self.focus_patch.stop)
        with patch.object(I, 'LiveInput', FakeWorker):
            self.app = I.ImeWindow(self.root, safe_preview=False)
        self.app.alt_held = self.app.alt_blocked = False
        self.app.alt_since = 0.0
        self.app.visible = True  # pretend it was shown at the caret
        self.app.state = 'starting'
        self.app.expect = 'start'
        self.app.target = 100
        self.app.ready = True
        self.recognized = []
        self.app.recognize = lambda strokes: self.recognized.append(strokes)

    def tearDown(self):
        # Some tests leave a placeholder element saved; close() releases it.
        with patch.object(I.auto_mode, 'release_element'):
            self.app.close()
        self.session_patch.stop()

    def _frames(self):
        # The started handler ends a capture whose Alt already went up.
        with patch.object(I.U, 'GetAsyncKeyState', return_value=0x8000):
            self.app.event('started', {'path': 'fake-device'})
        self.assertEqual(self.app.state, 'capturing')
        self.session = self.app.session
        self.app.event('frame', {'t': 0.0, 'contacts': [
            {'id': 0, 'x': 100, 'y': 100, 'tip': True, 'confidence': True}]})
        self.app.event('frame', {'t': 0.02, 'contacts': [
            {'id': 0, 'x': 110, 'y': 120, 'tip': True, 'confidence': True}]})

    def test_status_hints_never_take_room_from_the_candidates(self):
        # Routine hints were removed from the bar; show_status must stay a
        # no-op so the candidates keep the whole strip (and nothing pops up
        # at the pointer).
        self.app.visible = True
        self.app.info.config(text='')
        self.app.show_status('准备写字')
        self.assertEqual(self.app.info.cget('text'), '')
        self.assertFalse(hasattr(self.app, 'hud'))
        self.app.visible = False
        self.app.show_status('准备写字')
        self.assertFalse(hasattr(self.app, 'hud'))

    def test_status_never_overwrites_the_candidate_hint(self):
        self.app.state = 'choosing'
        self.app.candidate_texts = ['手']
        self.app.info.config(text='连写中 · 数字或点击选字')
        self.app.show_status('准备写字')
        self.assertEqual(self.app.info.cget('text'),
                         '连写中 · 数字或点击选字')

    def test_records_draws_and_recognizes_after_stop(self):
        self._frames()
        self.assertEqual(len(self.session.frames), 2)
        self.app.event('stopped', '鼠标已释放')
        self.assertTrue(self.session.closed)
        self.assertEqual(self.app.last_strokes, [[(100, 100), (110, 120)]])
        self.assertEqual(self.recognized[-1], [[(100, 100), (110, 120)]])

    def test_escape_stop_hides_without_recognizing(self):
        self._frames()
        self.app.expect = 'recognize'
        self.app.event('stopped', 'Esc：取消并释放')
        self.assertFalse(self.app.visible)
        self.assertEqual(self.recognized, [])
        self.assertTrue(self.session.closed)

    def test_stopping_without_ink_hides_the_empty_bar(self):
        # Hint text is gone from the bar, so a no-stroke round must not leave
        # a blank strip with only the close button on screen.
        self.app.visible = True
        self.app.state = 'capturing'
        self.app.expect = 'recognize'
        self.app.event('stopped', '鼠标已释放')
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.state, 'idle')

    def test_candidates_render_and_insert(self):
        self._frames()
        self.app.event('stopped', '鼠标已释放')
        payload = {'token': 0, 'results': [
            {'candidates': ['手', '龵'], 'top': '手'}], 'text': '手'}
        self.app.event('recognized', payload)
        self.assertEqual(self.app.state, 'choosing')
        self.assertEqual(self.app.candidate_texts, ['手', '龵'])
        # One canvas draws all cells: rounded background, digit and glyph.
        texts = [self.app.cand_frame.itemcget(item, 'text')
                 for item in self.app.cand_frame.find_all()
                 if self.app.cand_frame.type(item) == 'text']
        self.assertEqual(texts, ['1', '手', '2', '龵'])
        cells = [item for item in self.app.cand_frame.find_all()
                 if self.app.cand_frame.type(item) == 'polygon']
        self.assertEqual(len(cells), 2)
        self.assertEqual(self.app.cand_frame.itemcget(cells[0], 'fill'),
                         I.BAR_SEL_BG)
        self.assertEqual(self.app.cand_frame.itemcget(cells[1], 'fill'),
                         I.BAR_BG)
        # Sampled arcs, not a four-point rectangle: no square selection box.
        self.assertGreater(len(self.app.cand_frame.coords(cells[0])), 8)
        self.assertIn(('select_on', 2), self.app.worker.commands)
        inserted = []
        with patch.object(I, 'U') as u, \
             patch.object(I, 'send_unicode',
                          side_effect=lambda text: inserted.append(text) or True):
            u.IsWindow.return_value = True
            u.GetForegroundWindow.return_value = 100
            self.app.choose('手')
        self.assertEqual(inserted, ['手'])
        self.assertFalse(self.app.visible)
        self.assertIn('select_off', self.app.worker.commands)

    def test_number_key_selects_matching_candidate(self):
        self._frames()
        self.app.event('stopped', '鼠标已释放')
        self.app.event('recognized', {'token': 0, 'results': [
            {'candidates': ['手', '龵', '丰'], 'top': '手'}]})
        inserted = []
        with patch.object(I, 'U') as u, \
             patch.object(I, 'send_unicode',
                          side_effect=lambda text: inserted.append(text) or True):
            u.IsWindow.return_value = True
            u.GetForegroundWindow.return_value = 100
            self.app.event('select', 2)
        self.assertEqual(inserted, ['龵'])
        self.assertFalse(self.app.visible)

    def test_out_of_range_number_is_ignored(self):
        self._frames()
        self.app.event('stopped', '鼠标已释放')
        self.app.event('recognized', {'token': 0, 'results': [
            {'candidates': ['手'], 'top': '手'}]})
        inserted = []
        with patch.object(I, 'send_unicode',
                          side_effect=lambda text: inserted.append(text) or True):
            self.app.event('select', 3)
        self.assertEqual(inserted, [])
        self.assertTrue(self.app.visible)

    def test_typing_event_hides_the_stale_candidate_bar(self):
        self._frames()
        self.app.event('stopped', '鼠标已释放')
        self.app.event('recognized', {'token': 0, 'results': [
            {'candidates': ['手', '龵'], 'top': '手'}]})
        self.assertTrue(self.app.visible)
        self.assertEqual(self.app.state, 'choosing')
        self.app.event('typing', None)
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.state, 'idle')
        self.assertIn('select_off', self.app.worker.commands)

    def test_start_anchors_at_caret_and_requests_capture(self):
        self.app.visible = False
        self.app.state = 'idle'
        self.app.expect = None
        self.app.target = None
        work = (0, 0, 1920, 1080)
        with patch.object(I, 'caret_point', return_value=(500, 300, 20)), \
             patch.object(I, 'work_area', return_value=work), \
             patch.object(I, 'U') as u:
            u.GetForegroundWindow.return_value = 200
            u.IsIconic.return_value = False
            self.app.start()
        expected = I.place_window(500, 300, 20, self.app.width,
                                  self.app.height, work,
                                  gap=int(10 * self.app.ui),
                                  margin=int(4 * self.app.ui))
        self.assertEqual(self.app.pos, expected)
        self.assertEqual(self.app.target, 200)
        self.assertEqual(self.app.worker.target, 200)
        self.assertEqual(self.app.worker.commands, ['select_off', 'start'])
        self.assertEqual(self.app.state, 'starting')

    def test_alt_hold_starts_after_gate_only_once(self):
        self.app.visible = False
        self.app.state = 'idle'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.1)
            self.assertEqual(started, [])
            self.app.poll_alt(10.2)
            self.app.poll_alt(10.3)
        self.assertEqual(started, [1])

    def test_disabled_trigger_never_starts_on_its_own(self):
        self.app.state = 'idle'
        self.app.visible = False
        self.app.trigger_enabled = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_auto_pending_starts_immediately_even_while_choosing(self):
        self.app.state = 'choosing'
        self.app.visible = True
        self.app.alt_held = True
        self.app.auto_pending = True
        self.app.trigger_enabled = False
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
            self.app.poll_alt(10.0)
        self.assertEqual(started, [1])
        self.assertFalse(self.app.auto_pending)

    def test_auto_pending_is_dropped_when_alt_ends_early(self):
        self.app.state = 'idle'
        self.app.trigger_enabled = False
        self.app.auto_pending = True
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            self.app.poll_alt(10.0)
        self.assertEqual(started, [])
        self.assertFalse(self.app.auto_pending)

    def test_alt_with_ctrl_never_starts(self):
        self.app.state = 'idle'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk in (0x12, 0x11) else 0)
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_alt_hold_aborts_when_tab_is_pressed(self):
        self.app.state = 'idle'
        started = []
        self.app.start = lambda: started.append(1)
        down = {0x12}

        def keys(vk):
            return 0x8000 if vk in down else 0

        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = keys
            self.app.poll_alt(10.0)
            down.add(0x09)
            self.app.poll_alt(10.2)
            down.discard(0x09)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_alt_release_requests_recognition(self):
        self.app.state = 'capturing'
        self.app.alt_held = True
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            self.app.poll_alt(10.0)
        self.assertEqual(self.app.worker.commands, ['release'])
        self.assertEqual(self.app.expect, 'recognize')

    def test_alt_press_while_recognizing_is_ignored(self):
        self.app.state = 'recognizing'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.5)
        self.assertEqual(started, [])

    def test_alt_press_while_choosing_starts_new_capture(self):
        self.app.state = 'choosing'
        started = []
        self.app.start = lambda: started.append(1)
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
            self.app.poll_alt(10.0)
            self.app.poll_alt(10.3)
        self.assertEqual(started, [1])

    def test_alt_restart_clears_previous_ink_and_candidates(self):
        self.app.visible = True
        self.app.state = 'choosing'
        self.app.candidate_texts = ['手']
        self.app.canvas.create_line(1, 1, 2, 2, tags='ink')
        with patch.object(I, 'caret_point', return_value=(500, 300, 20)), \
             patch.object(I, 'work_area', return_value=(0, 0, 1920, 1080)), \
             patch.object(I, 'U') as u:
            u.GetForegroundWindow.return_value = 200
            u.IsIconic.return_value = False
            self.app.start()
        self.assertEqual(self.app.candidate_texts, [])
        self.assertEqual(self.app.canvas.find_withtag('ink'), ())
        self.assertEqual(self.app.worker.commands[-2:], ['select_off', 'start'])
        self.assertEqual(self.app.state, 'starting')

    def test_start_without_foreground_is_silent(self):
        self.app.visible = False
        self.app.state = 'idle'
        with patch.object(I, 'U') as u:
            u.GetForegroundWindow.return_value = 0
            self.app.start()
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.worker.commands, [])

    # ---------- prearm: the first stroke is never lost ----------
    def _prearm(self, now=10.0):
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk == 0x12 else 0)
            u.GetForegroundWindow.return_value = 200
            u.IsIconic.return_value = False
            self.app.poll_alt(now)

    def test_alt_down_arms_capture_before_the_hold_gate(self):
        self.app.visible = False
        self.app.state = 'idle'
        self.app.target = None
        self._prearm()
        self.assertTrue(self.app.prearmed)
        self.assertEqual(self.app.state, 'starting')
        self.assertEqual(self.app.target, 200)
        self.assertEqual(self.app.worker.target, 200)
        self.assertEqual(self.app.worker.commands, ['select_off', 'start'])

    def test_a_tap_before_the_gate_drops_the_hidden_capture(self):
        self.app.visible = False
        self.app.state = 'idle'
        self._prearm()
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            self.app.poll_alt(10.1)
        self.assertFalse(self.app.prearmed)
        self.assertFalse(self.app.visible)
        self.assertEqual(self.app.state, 'idle')
        self.assertIsNone(self.app.target)
        self.assertIn('release', self.app.worker.commands)
        # Still counts as the first tap of a double Alt (连写).
        self.assertNotEqual(self.app.taps.last_tap, 0.0)

    def test_the_gate_shows_the_prearmed_capture_without_restarting_it(self):
        self.app.visible = False
        self.app.state = 'idle'
        self._prearm()
        self.app.worker.commands.clear()
        work = (0, 0, 1920, 1080)
        with patch.object(I, 'caret_point', return_value=(500, 300, 20)), \
             patch.object(I, 'work_area', return_value=work), \
             patch.object(I, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk == 0x12 else 0)
            u.GetForegroundWindow.return_value = 200
            u.IsIconic.return_value = False
            self.app.poll_alt(10.3)
        self.assertTrue(self.app.visible)
        self.assertFalse(self.app.prearmed)
        self.assertEqual(self.app.worker.commands, [])  # no second start
        self.assertEqual(self.app.pos, I.place_window(
            500, 300, 20, self.app.width, self.app.height, work,
            gap=int(10 * self.app.ui), margin=int(4 * self.app.ui)))

    def test_a_first_stroke_shows_the_pad_before_the_gate(self):
        self.app.visible = False
        self.app.state = 'idle'
        self._prearm()
        with patch.object(I.U, 'GetAsyncKeyState', return_value=0x8000):
            self.app.event('started', {'path': 'fake-device'})
            self.assertEqual(self.app.state, 'capturing')
            self.assertFalse(self.app.visible)
            self.app.event('frame', {'t': 0.0, 'contacts': [
                {'id': 0, 'x': 100, 'y': 100, 'tip': True,
                 'confidence': True}]})
        self.assertTrue(self.app.show_due)
        shown = []
        self.app.start = lambda: shown.append(1)
        self.app.alt_since = time.monotonic()  # gate has not elapsed yet
        with patch.object(I.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0):
            self.app.tick()
        self.assertEqual(shown, [1])

    # ---------- browser Alt release moves focus to the toolbar ----------
    def test_alt_down_saves_the_text_field_element(self):
        element = object()
        with patch.object(I.auto_mode, 'focused_editable',
                          return_value=element) as focused, \
             patch.object(I.auto_mode, 'process_id', return_value=42):
            self.app.remember_focus()
        focused.assert_called_once_with(42)
        self.assertIs(self.app.focus_element, element)

    def test_remember_focus_releases_the_old_element(self):
        old, new = object(), object()
        self.app.focus_element = old
        with patch.object(I.auto_mode, 'focused_editable', return_value=new), \
             patch.object(I.auto_mode, 'process_id', return_value=42), \
             patch.object(I.auto_mode, 'release_element') as release:
            self.app.remember_focus()
        release.assert_called_once_with(old)
        self.assertIs(self.app.focus_element, new)

    def test_hide_releases_the_saved_field(self):
        element = object()
        self.app.focus_element = element
        with patch.object(I.auto_mode, 'release_element') as release:
            self.app.hide()
        release.assert_called_once_with(element)
        self.assertIsNone(self.app.focus_element)

    def test_candidates_put_the_browser_caret_back(self):
        self.app.visible = True
        self.app.target = 100
        element = object()
        self.app.focus_element = element
        with patch.object(I, 'focus_ready',
                          return_value=I.FOCUS_RESTORED) as ready:
            self.app.show_recognition({'results': [{'candidates': ['手']}]})
        ready.assert_called_once_with(100, element)

    def test_continuous_live_recognition_leaves_focus_alone(self):
        self.app.visible = True
        self.app.target = 100
        self.app.focus_element = object()
        with patch.object(I, 'focus_ready') as ready:
            self.app.show_recognition({'results': [{'candidates': ['手']}]},
                                      live=True)
        ready.assert_not_called()

    # ---------- real-time recognition while Alt stays held ----------
    def test_writing_recognizes_without_waiting_for_a_pause(self):
        self._frames()
        # The finger is still on the pad; recognition no longer waits for it
        # to lift or for a stillness timeout.
        self.assertTrue(self.app.tracker.active)
        self.app.last_live_wall = time.monotonic() - 1
        calls = []
        self.app.live_recognize = lambda: calls.append(1)
        self.app.worker.commands.clear()
        with patch.object(I.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0):
            self.app.tick()
        self.assertEqual(calls, [1])
        self.assertNotIn('release', self.app.worker.commands)
        self.assertEqual(self.app.state, 'capturing')

    def test_live_recognition_is_throttled_to_the_refresh_gap(self):
        self._frames()
        self.app.last_live_wall = time.monotonic()
        calls = []
        self.app.live_recognize = lambda: calls.append(1)
        with patch.object(I.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0):
            self.app.tick()
        self.assertEqual(calls, [])  # too soon: no point re-reading the ink
        self.app.last_live_wall = (
            time.monotonic() - I.LIVE_REFRESH - 0.01)
        with patch.object(I.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0):
            self.app.tick()
        self.assertEqual(calls, [1])

    def test_live_result_fills_the_bar_without_stopping_the_capture(self):
        self._frames()
        self.app.recognition_token = 7
        self.app.live_pending = True
        self.app.worker.commands.clear()
        self.app.event('recognized', {'token': 7, 'results': [
            {'candidates': ['手', '龵'], 'top': '手'}]})
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.candidate_texts, ['手', '龵'])
        self.assertTrue(self.app.live_ready)
        # Live candidates can be picked with 1-2 (or a click) even while Alt
        # is still held; a new stroke sends select_off again.
        self.assertIn(('select_on', 2), self.app.worker.commands)
        # No hint text: the bar width belongs to the candidates.
        self.assertEqual(self.app.info.cget('text'), '')
        self.assertEqual(len(self.app.cand_frame.find_all()), 6)  # 2 bg + 4 text

    def test_same_live_answer_repaints_nothing_but_rearms_digits(self):
        self._frames()
        payload = {'results': [{'candidates': ['手'], 'top': '手'}]}
        self.app.live_pending = True
        self.app.event('recognized', dict(payload, token=self.app.recognition_token))
        items = tuple(self.app.cand_frame.find_all())
        self.app.worker.commands.clear()
        self.app.live_pending = True
        self.app.event('recognized', dict(payload, token=self.app.recognition_token))
        # The engine often returns the same answer for a few more points;
        # redrawing would flicker, but the digit window must keep sliding.
        self.assertEqual(tuple(self.app.cand_frame.find_all()), items)
        self.assertIn(('select_on', 1), self.app.worker.commands)
        self.assertTrue(self.app.live_ready)

    def test_a_digit_selects_live_candidates_while_still_capturing(self):
        self._frames()
        self.app.recognition_token = 7
        self.app.live_pending = True
        self.app.event('recognized', {'token': 7, 'results': [
            {'candidates': ['手', '龵'], 'top': '手'}]})
        # Live recognition keeps capturing, so 'choosing' never happens here;
        # digits must still pick the candidate (clicks already did).
        self.assertEqual(self.app.state, 'capturing')
        self.assertTrue(self.app.live_ready)
        picked = []
        self.app.choose = lambda text, source='click': picked.append(text)
        self.app.event('select', 2)
        self.assertEqual(picked, ['龵'])
        # Stale or cleared candidates: the digit stays with the app.
        self.app.live_ready = False
        self.app.event('select', 1)
        self.assertEqual(picked, ['龵'])

    def test_a_new_stroke_keeps_the_bar_live_instead_of_blanking_it(self):
        self._frames()
        self.app.live_pending = True
        self.app.live_version = self.app.ink_version
        self.app.event('recognized', {'token': self.app.recognition_token,
                                      'results': [{'candidates': ['手'],
                                                   'top': '手'}]})
        self.assertTrue(self.app.live_ready)
        token = self.app.recognition_token
        before = self.app.ink_version
        self.app.event('frame', {'t': 5.0, 'contacts': [
            {'id': 1, 'x': 200, 'y': 200, 'tip': True, 'confidence': True}]})
        # Continuous refresh: the cells stay until a newer answer replaces
        # them, so multi-stroke writing does not flicker the bar.
        self.assertEqual(self.app.candidate_texts, ['手'])
        self.assertTrue(self.app.live_ready)
        self.assertGreater(self.app.ink_version, before)
        self.assertEqual(self.app.recognition_token, token)

    def test_release_keeps_live_candidates_and_arms_selection(self):
        self._frames()
        self.app.live_pending = True
        self.app.live_version = self.app.ink_version
        self.app.event('recognized', {'token': self.app.recognition_token,
                                      'results': [{'candidates': ['手', '龵'],
                                                   'top': '手'}]})
        self.assertTrue(self.app.live_ready)
        self.app.alt_held = True
        self.app.worker.commands.clear()
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            self.app.poll_alt(10.0)
        self.assertEqual(self.app.expect, 'keep')
        self.assertEqual(self.app.worker.commands, ['release'])
        self.app.event('stopped', '鼠标已释放')
        self.assertEqual(self.app.state, 'choosing')
        self.assertTrue(self.app.visible)
        self.assertIn(('select_on', 2), self.app.worker.commands)
        self.assertEqual(self.recognized, [])

    def test_release_re_reads_ink_written_after_the_live_answer(self):
        self._frames()
        self.app.live_pending = True
        self.app.live_version = self.app.ink_version
        self.app.event('recognized', {'token': self.app.recognition_token,
                                      'results': [{'candidates': ['手'],
                                                   'top': '手'}]})
        self.assertTrue(self.app.live_ready)
        # The throttled refresh has not caught up with this point yet.
        self.app.ink_version += 1
        self.app.alt_held = True
        with patch.object(I, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            self.app.poll_alt(10.0)
        self.assertEqual(self.app.expect, 'recognize')
        self.app.event('stopped', '鼠标已释放')
        self.assertEqual(self.recognized[-1], [[(100, 100), (110, 120)]])

    def test_live_error_keeps_the_capture_running(self):
        self._frames()
        self.app.live_pending = True
        self.app.event('recognized', {'token': self.app.recognition_token,
                                      'error': 'boom'})
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.candidate_texts, [])

    def test_ready_event_marks_worker_ready(self):
        self.app.ready = False
        self.app.event('ready', {'global_f8': False, 'start_hotkey': False,
                                 'hotkey': 'Alt', 'quit': True,
                                 'quit_key': 'Ctrl+Shift+F8'})
        self.assertTrue(self.app.ready)

    # ---------- continuous (连写) ----------
    def _poll(self, now, down):
        with patch.object(I.U, 'SetForegroundWindow'), \
             patch.object(I.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if down and vk == 0x12 else 0):
            self.app.poll_alt(now)

    def _double_alt(self, start):
        self._poll(start, True)
        self._poll(start + 0.08, False)
        self._poll(start + 0.20, True)
        self._poll(start + 0.28, False)

    def test_double_alt_enters_continuous(self):
        self.app.state = 'idle'
        self.app.locate_overlay = lambda: (100, 30, 40)
        self.app.worker.commands.clear()
        self._double_alt(10.0)
        self.assertTrue(self.app.continuous)
        self.assertEqual(self.app.target, 100)
        self.assertEqual(self.app.state, 'starting')
        self.assertTrue(self.app.worker.continuous)
        self.assertTrue(self.app.worker.free_hold)
        # Both taps may pre-arm; continuous entry ends with one fresh start.
        self.assertEqual(self.app.worker.commands[-2:], ['select_off', 'start'])
        self.assertFalse(self.app.taps.down)

    def test_single_alt_tap_clears_the_round_in_continuous(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.canvas.create_line(1, 1, 2, 2, tags='ink')
        self.app.recording_strokes = [
            {'contact': 0, 'points': [(0.0, 1, 1)]}]
        token = self.app.recognition_token
        self._poll(10.0, True)
        self._poll(10.08, False)
        self.assertTrue(self.app.continuous)
        self.assertEqual(self.app.canvas.find_withtag('ink'), ())
        self.assertEqual(self.app.recording_strokes, [])
        self.assertEqual(self.app.recognition_token, token + 1)
        # Clearing the round empties the strip, so only the terse 连写 status
        # shows (no old "已清空" explanation).
        self.assertEqual(self.app.info.cget('text'), '连写中')

    def test_continuous_recognition_needs_no_pause_and_keeps_capture(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.expect = 'recognize'
        self.app.session = FakeSession('fake')
        self.app.recording_strokes = [
            {'contact': 0, 'points': [(0.0, 1, 1)]}]
        self.app.last_live_wall = time.monotonic() - 2
        calls = []
        self.app.live_recognize = lambda: calls.append(1)
        self.app.worker.commands.clear()
        with patch.object(I.U, 'GetAsyncKeyState', return_value=0):
            self.app.tick()
        self.assertEqual(calls, [1])
        self.assertNotIn('release', self.app.worker.commands)
        self.assertEqual(self.app.expect, 'recognize')
        self.assertEqual(self.app.state, 'capturing')

    def test_continuous_live_result_arms_digits_but_keeps_writing(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.expect = 'recognize'
        self.app.session = FakeSession('fake')
        self.app.canvas.create_line(1, 1, 2, 2, tags='ink')
        self.app.recording_strokes = [
            {'contact': 0, 'points': [(0.0, 1, 1)]}]
        self.app.stroke_version = 5
        self.app.recognition_token = 9
        self.app.live_pending = True
        self.app.worker.commands.clear()
        self.app.event('recognized', {'token': 9, 'results': [
            {'candidates': ['手', '龵'], 'top': '手'}]})
        self.assertEqual(self.app.state, 'capturing')
        self.assertTrue(self.app.live_ready)
        self.assertIn(('select_on', 2), self.app.worker.commands)
        # The bar shows only the candidates, no explanatory hint.
        self.assertEqual(self.app.info.cget('text'), '')
        # The next stroke keeps the bar; the refresh replaces it in place.
        self.app.worker.commands.clear()
        self.app.event('frame', {'t': 5.0, 'contacts': [
            {'id': 1, 'x': 200, 'y': 200, 'tip': True, 'confidence': True}]})
        self.assertEqual(self.app.candidate_texts, ['手', '龵'])
        self.assertNotIn('select_off', self.app.worker.commands)
        self.assertEqual(self.app.info.cget('text'), '')

    def test_continuous_shows_status_only_while_the_strip_is_empty(self):
        self.app.continuous = True
        self.app.visible = True
        self.app.state = 'capturing'
        self.app.tracker.active.clear()
        self.app.info.config(text='')
        self.app.show_idle_hint()
        self.assertEqual(self.app.info.cget('text'), '连写中')
        # The first stroke takes the strip over; no status while writing.
        self.app.session = FakeSession('fake')
        self.app.event('frame', {'t': 0.0, 'contacts': [
            {'id': 0, 'x': 100, 'y': 100, 'tip': True, 'confidence': True}]})
        self.assertEqual(self.app.info.cget('text'), '')
        # Candidates keep the whole strip; the hint waits.
        self.app.recognition_token += 1
        self.app.live_pending = True
        self.app.event('recognized', {'token': self.app.recognition_token,
                                      'results': [{'candidates': ['手'],
                                                   'top': '手'}]})
        self.assertEqual(self.app.info.cget('text'), '')
        # Picking a candidate clears the round in 连写: the strip is empty
        # again, so the mode status comes back.
        self.app.clear_ink()
        self.app.clear_candidates()
        self.app.show_idle_hint()
        self.assertEqual(self.app.info.cget('text'), '连写中')
        # Leaving 连写 drops the status.
        self.app.exit_continuous()
        self.assertEqual(self.app.info.cget('text'), '')

    def test_picking_a_candidate_in_continuous_keeps_the_capture(self):
        # 连写 never releases between rounds; re-arming must reuse the active
        # capture. Sending 'start' would make the worker release it and drop
        # the user out of 连写 right after picking a candidate.
        self.app.continuous = True
        self.app.visible = True
        self.app.state = 'capturing'
        self.app.target = 100
        self.app.worker.target = 100
        self.app.worker.guard.active = True
        self.app.locate_overlay = lambda: (100, 30, 40)
        self.app.worker.commands.clear()
        self.app.arm_continuous()
        self.assertTrue(self.app.continuous)
        self.assertEqual(self.app.state, 'capturing')
        self.assertEqual(self.app.expect, 'recognize')
        self.assertNotIn('start', self.app.worker.commands)
        self.assertNotIn('release', self.app.worker.commands)
        self.assertIn('select_off', self.app.worker.commands)
        self.assertEqual(self.app.info.cget('text'), '连写中')

    def test_double_alt_exits_continuous_in_manual_mode(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.worker.commands.clear()
        self._double_alt(20.0)
        self.assertFalse(self.app.continuous)
        self.assertEqual(self.app.expect, 'cancel')
        # The first tap clears the round (digits off), the second exits.
        self.assertEqual(self.app.worker.commands[-1:], ['release'])

    def test_a_right_click_leaves_continuous(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.worker.commands.clear()
        self.app.event('exit_continuous', '右键退出连写')
        self.assertFalse(self.app.continuous)
        self.assertFalse(self.app.worker.continuous)
        self.assertFalse(self.app.worker.free_hold)
        self.assertEqual(self.app.expect, 'cancel')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_a_right_click_keeps_candidates_when_choosing(self):
        self.app.continuous = True
        self.app.state = 'choosing'
        self.app.visible = True
        self.app.candidate_texts = ['手']
        self.app.worker.commands.clear()
        self.app.event('exit_continuous', '右键退出连写')
        self.assertFalse(self.app.continuous)
        self.assertIn('select_off', self.app.worker.commands)
        # Leaving 连写 keeps the bar as it is; no hint text is printed.
        self.assertEqual(self.app.info.cget('text'), '')

    def test_two_long_alt_presses_leave_continuous(self):
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.worker.commands.clear()
        self._poll(20.0, True)    # held press: not a tap
        self._poll(20.30, False)
        self._poll(20.40, True)   # second press-down: leave 连写
        self.assertFalse(self.app.continuous)
        self.assertEqual(self.app.expect, 'cancel')
        self.assertEqual(self.app.worker.commands, ['release'])

    def test_auto_views_leave_the_double_exit_to_the_host(self):
        self.app.trigger_enabled = False
        self.app.continuous = True
        self.app.state = 'capturing'
        self.app.worker.commands.clear()
        self._double_alt(20.0)
        self.assertTrue(self.app.continuous)
        self.assertNotIn('release', self.app.worker.commands)

    def test_choose_writes_and_rearms_in_continuous(self):
        self.app.continuous = True
        self.app.state = 'choosing'
        self.app.visible = True
        self.app.target = 100
        self.app.candidate_texts = ['手']
        self.app.locate_overlay = lambda: (100, 30, 40)
        inserted = []
        with patch.object(I, 'U') as u, \
             patch.object(I, 'send_unicode',
                          side_effect=lambda text: inserted.append(text)
                          or True):
            u.IsWindow.return_value = True
            u.GetForegroundWindow.return_value = 100
            self.app.choose('手')
        self.assertEqual(inserted, ['手'])
        self.assertTrue(self.app.visible)
        self.assertTrue(self.app.continuous)
        self.assertEqual(self.app.state, 'starting')
        self.assertEqual(self.app.worker.commands[-2:], ['select_off', 'start'])

    def test_continuous_failed_insert_flashes_after_rearming(self):
        # The old order copied (flash) and then re-armed, which wiped the
        # 已复制 notice: a failed insertion looked like nothing happened.
        self.app.continuous = True
        self.app.state = 'choosing'
        self.app.visible = True
        self.app.target = 100
        order = []
        with patch.object(I, 'U') as u, \
             patch.object(I, 'send_unicode', return_value=False), \
             patch.object(self.app, 'arm_continuous',
                          side_effect=lambda: order.append('arm')), \
             patch.object(self.app, 'copy',
                          side_effect=lambda text: order.append(('copy', text))):
            u.IsWindow.return_value = True
            u.GetForegroundWindow.return_value = 100
            self.app.choose('手')
        self.assertEqual(order, ['arm', ('copy', '手')])

    def test_select_blocked_tells_the_user_why_digits_do_nothing(self):
        self.app.state = 'capturing'
        self.app.visible = True
        self.app.event('select_blocked', '输入法组词中')
        self.assertEqual(self.app.info.cget('text'), '输入法组词中：数字暂不可选')

    def test_focus_loss_while_choosing_still_hides(self):
        self.app.continuous = True
        self.app.state = 'choosing'
        self.app.visible = True
        self.app.target = 100
        self.app.expect = None
        with patch.object(I.U, 'GetAsyncKeyState', return_value=0), \
             patch.object(I.U, 'IsWindow', return_value=True), \
             patch.object(I.U, 'GetForegroundWindow', return_value=999):
            self.app.tick()
        self.assertFalse(self.app.visible)

    def test_the_candidate_bar_has_rounded_win11_corners(self):
        self.assertEqual(len(self.app.bar_masks), 4)
        for mask in self.app.bar_masks:
            self.assertEqual(mask.winfo_manager(), 'place')
            self.assertEqual(str(mask.cget('bg')).lower(), I.KEY)
            kinds = [mask.type(item) for item in mask.find_all()]
            self.assertEqual(kinds, ['arc', 'arc'])  # fill + border arc
        # The corner masks must not resize or re-pack the bar itself.
        self.assertEqual(self.app.bar.winfo_manager(), 'pack')
        # No Tk highlight border: it lives outside the children's area, so a
        # mask could never cover it and a square line stayed at each corner
        # (the reported "black corner"). Straight edges use 1px strips.
        self.assertEqual(int(self.app.bar.cget('highlightthickness')), 0)
        self.assertEqual(len(self.app.bar_edges), 4)
        for strip in self.app.bar_edges:
            self.assertEqual(strip.winfo_manager(), 'place')
            self.assertEqual(str(strip.cget('bg')).lower(),
                             I.BAR_EDGE.lower())


def visual_check(app):
    """Run from --self-check; replays real recorded ink and verifies pixels."""
    try:
        style = I.U.GetWindowLongPtrW(app.hwnd, I.GWL_EXSTYLE)
        assert style & I.WS_EX_NOACTIVATE, 'overlay must never activate'
        assert style & I.WS_EX_TOOLWINDOW, 'overlay must stay out of Alt+Tab'
        assert str(app.root.wm_attributes('-transparentcolor')).lower() == I.KEY
        strokes = first_character()
        assert len(strokes) == 4
        app.demo(strokes)
    except BaseException:
        app.self_check_failed = True
        traceback.print_exc()
        app.close()
        return

    deadline = time.monotonic() + 10

    def poll():
        try:
            if app.state == 'choosing' and app.candidate_texts:
                app.root.after(400, capture)
            elif time.monotonic() > deadline:
                raise RuntimeError('recognition did not produce candidates')
            else:
                app.root.after(120, poll)
        except BaseException:
            app.self_check_failed = True
            traceback.print_exc()
            app.close()

    def capture():
        try:
            from PIL import ImageGrab
            image = ImageGrab.grab(bbox=(app.pos[0], app.pos[1],
                                         app.pos[0] + app.width,
                                         app.pos[1] + app.height))
            out = BASE / 'data' / 'ime_preview.png'
            image.save(out)
            pixels = list(image.getdata())
            ink = sum(1 for p in pixels if abs(p[0] - 30) <= 8
                      and abs(p[1] - 51) <= 8 and abs(p[2] - 80) <= 8)
            bar = sum(1 for p in pixels if abs(p[0] - 246) <= 6
                      and abs(p[1] - 246) <= 6 and abs(p[2] - 246) <= 6)
            assert app.candidate_texts[0] == '手', app.candidate_texts
            texts = [app.cand_frame.itemcget(item, 'text')
                     for item in app.cand_frame.find_all()
                     if app.cand_frame.type(item) == 'text']
            assert texts[:2] == ['1', '手'], texts
            assert ink > 150, 'ink pixels: %d' % ink
            assert bar > 2000, 'bar pixels: %d' % bar
            # ascii() keeps rare glyphs from breaking a GBK console.
            print('visual check ok: candidates=%s ink=%d bar=%d saved=%s'
                  % (ascii(app.candidate_texts), ink, bar, out))
        except BaseException:
            app.self_check_failed = True
            traceback.print_exc()
        finally:
            app.close()

    app.root.after(120, poll)


if __name__ == '__main__':
    unittest.main()
