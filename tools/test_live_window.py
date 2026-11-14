"""Automated tests: NEVER capture mouse or inject keyboard/mouse input."""
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import live_input as L
from live_window import MotionWindow, Session, BASE


def report(flags=3, x=500, y=300, count=1):
    r = bytearray(30)
    r[0], r[1], r[28] = 3, flags, count
    r[2:4], r[4:6] = x.to_bytes(2, 'little'), y.to_bytes(2, 'little')
    return bytes(r)


def frame(t, flags=3, x=500, y=300, count=1):
    value = L.parse_report(report(flags, x, y, count))
    value['t'] = t
    return value


class DecoderTests(unittest.TestCase):
    def test_hid_verified_flags(self):
        for flags, tip, confidence in [(0, False, False), (1, False, True),
                                      (2, True, False), (3, True, True)]:
            c = frame(0, flags)['contacts'][0]
            self.assertEqual((c['tip'], c['confidence']), (tip, confidence))

    def test_reject_unknown_report(self):
        for bad in [b'', report()[:-1], b'\x02' + report()[1:], report(count=6)]:
            with self.assertRaises(ValueError):
                L.parse_report(bad)

    def test_source_hex_and_coordinate_are_unmodified(self):
        source = report(7, 1332, 828)
        result = L.parse_report(source)
        self.assertEqual(result['hex'], source.hex())
        self.assertEqual(result['contacts'][0]['id'], 1)
        self.assertEqual(result['contacts'][0]['x'], 1332)

    def test_lift_and_gap_split(self):
        t = L.StrokeTracker()
        self.assertIsNone(t.update(frame(0))[0][1])
        self.assertIsNotNone(t.update(frame(.01, x=510))[0][1])
        self.assertEqual(t.update(frame(.02, flags=1)), [])
        self.assertIsNone(t.update(frame(.03))[0][1])
        self.assertIsNone(t.update(frame(.3))[0][1])
        self.assertEqual(t.strokes, 3)
        self.assertEqual(t.update(frame(.31, count=0)), [])
        self.assertEqual(t.active, {})

    def test_missing_report_within_grace_stays_connected(self):
        t = L.StrokeTracker(grace=0.07)
        self.assertIsNone(t.update(frame(0))[0][1])
        self.assertEqual(t.update(frame(0.012, count=0)), [])
        self.assertIsNotNone(t.update(frame(0.024, x=505))[0][1])
        self.assertEqual(t.strokes, 1)

    def test_fast_retouch_after_lift_starts_a_new_stroke(self):
        # 149 recorded lifts re-touched within 75 ms; merging them with grace
        # drew long connectors between separate strokes in PPT ink.
        t = L.StrokeTracker(grace=0.07)
        self.assertIsNone(t.update(frame(0))[0][1])
        self.assertEqual(t.update(frame(0.02, flags=1)), [])
        self.assertIsNone(t.update(frame(0.03, x=700, y=400))[0][1])
        self.assertEqual(t.strokes, 2)

    def test_uncertain_contact_recorded_but_not_drawn(self):
        f = frame(0, flags=2)
        self.assertTrue(f['contacts'][0]['tip'])
        self.assertEqual(L.StrokeTracker().update(f), [])


class SelectDigitTests(unittest.TestCase):
    def setUp(self):
        # Selection diagnostics append data/ime_input.log; keep tests offline.
        self.log_patch = patch.object(L, 'input_log')
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def _worker(self, limit=5):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.selecting = True
        worker.select_limit = limit
        worker._modified = lambda: False
        worker._composing = lambda: False
        return worker

    def test_select_on_installs_keyboard_hook_only(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, \
             patch.object(L, 'ime_composing', return_value=False):
            u.SetWindowsHookExW.return_value = 99
            worker._select_on(5)
        self.assertTrue(worker.selecting)
        self.assertEqual(worker.select_limit, 5)
        self.assertEqual(worker.key_hook, 99)
        self.assertEqual(u.SetWindowsHookExW.call_args[0][0], 13)  # WH_KEYBOARD_LL

    def test_select_on_clamps_and_zero_disables(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, \
             patch.object(L, 'ime_composing', return_value=False):
            u.SetWindowsHookExW.return_value = 99
            worker._select_on(20)
            self.assertEqual(worker.select_limit, 9)
            worker._select_off()
            self.assertFalse(worker.selecting)
            worker._select_on(0)
            self.assertFalse(worker.selecting)

    def test_select_on_skips_while_ime_is_composing(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, \
             patch.object(L, 'ime_composing', return_value=True):
            u.SetWindowsHookExW.return_value = 99
            worker._select_on(5)
            # The view must know why the digits are dead, but only once per
            # episode: candidate refreshes call _select_on repeatedly.
            worker._select_on(5)
            worker._select_on(7)
        self.assertFalse(worker.selecting)
        self.assertIsNone(worker.key_hook)
        u.SetWindowsHookExW.assert_not_called()
        events = []
        while not worker.events.empty():
            events.append(worker.events.get_nowait())
        self.assertEqual(events, [('select_blocked', '输入法组词中')])

    def test_select_blocked_reports_again_after_digits_worked(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, \
             patch.object(L, 'ime_composing', side_effect=[True, False, True]):
            u.SetWindowsHookExW.return_value = 99
            worker._select_on(5)
            worker._select_off()
            worker._select_on(5)
            worker._select_off()
            worker._select_on(5)
        events = []
        while not worker.events.empty():
            events.append(worker.events.get_nowait())
        self.assertEqual(events, [('select_blocked', '输入法组词中'),
                                  ('select_blocked', '输入法组词中')])

    def test_digit_emits_index_and_swallows_both_halves(self):
        worker = self._worker(limit=3)
        vk = W.DWORD(0x32)  # key '2'
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(vk)), 1)
            self.assertEqual(worker.events.get_nowait(), ('select', 2))
            self.assertEqual(worker._keyboard(0, 0x101, C.addressof(vk)), 1)
            u.CallNextHookEx.assert_not_called()

    def test_digit_outside_limit_passes_through(self):
        worker = self._worker(limit=2)
        vk = W.DWORD(0x33)  # key '3'
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(vk)), 777)
            u.CallNextHookEx.assert_called_once()
        # An out-of-range digit is ordinary typing: hand the bar back too.
        self.assertEqual(worker.events.get_nowait(), ('typing', None))
        self.assertFalse(worker.selecting)

    def test_key_up_still_swallowed_after_select_off(self):
        worker = self._worker(limit=5)
        vk = W.DWORD(0x31)  # key '1'
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(vk)), 1)
            worker.events.get_nowait()
            worker._select_off()
            self.assertEqual(worker._keyboard(0, 0x101, C.addressof(vk)), 1)
            # A later digit is ordinary typing again.
            other = W.DWORD(0x34)
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(other)), 777)

    def test_letter_key_hands_digits_back_to_pinyin(self):
        # The bar was showing, then the user started typing pinyin: our hook
        # must stop eating 1-5 so the IME can pick its own candidates.
        worker = self._worker(limit=5)
        letter = W.DWORD(0x4E)  # key 'n'
        digit = W.DWORD(0x32)   # key '2'
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(letter)),
                             777)
            self.assertEqual(worker.events.get_nowait(), ('typing', None))
            self.assertFalse(worker.selecting)
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(digit)),
                             777)
            self.assertEqual(worker._keyboard(0, 0x101, C.addressof(digit)),
                             777)
        self.assertTrue(worker.events.empty())

    def test_space_and_backspace_also_resume_typing(self):
        for vk in (0x20, 0x08, 0x0D, 0xBE):  # space, backspace, enter, '.'
            worker = self._worker(limit=5)
            key = W.DWORD(vk)
            with patch.object(L, 'U') as u:
                u.CallNextHookEx.return_value = 777
                self.assertEqual(
                    worker._keyboard(0, 0x100, C.addressof(key)), 777)
                self.assertEqual(worker.events.get_nowait(),
                                 ('typing', None))
                self.assertFalse(worker.selecting)

    def test_modifier_keys_keep_digit_selection(self):
        worker = self._worker(limit=5)
        digit = W.DWORD(0x31)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            for vk in (0x10, 0x11):  # Shift, Ctrl
                key = W.DWORD(vk)
                self.assertEqual(
                    worker._keyboard(0, 0x100, C.addressof(key)), 777)
            self.assertTrue(worker.selecting)
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(digit)), 1)
            self.assertEqual(worker.events.get_nowait(), ('select', 1))

    def test_expired_selection_is_not_swallowed(self):
        worker = self._worker(limit=5)
        worker.select_deadline = time.monotonic() - 1
        digit = W.DWORD(0x31)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(digit)),
                             777)
            self.assertEqual(worker.events.get_nowait(), ('typing', None))
            self.assertFalse(worker.selecting)

    def test_ctrl_digit_is_a_shortcut_not_a_candidate(self):
        worker = self._worker(limit=5)
        worker._modified = lambda: True
        digit = W.DWORD(0x31)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(digit)),
                             777)
            self.assertEqual(worker._keyboard(0, 0x101, C.addressof(digit)),
                             777)
        self.assertTrue(worker.selecting)  # the bar survives a shortcut
        self.assertTrue(worker.events.empty())

    def test_digit_passes_when_the_ime_owns_a_composition(self):
        worker = self._worker(limit=5)
        worker._composing = lambda: True
        digit = W.DWORD(0x31)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._keyboard(0, 0x100, C.addressof(digit)),
                             777)
            self.assertEqual(worker._keyboard(0, 0x101, C.addressof(digit)),
                             777)
        self.assertEqual(worker.events.get_nowait(), ('typing', None))
        self.assertFalse(worker.selecting)
        self.assertTrue(worker.events.empty())

    def test_modified_detects_held_modifiers(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L.U, 'GetAsyncKeyState',
                          side_effect=lambda vk: 0x8000 if vk == 0x11 else 0):
            self.assertTrue(worker._modified())
        with patch.object(L.U, 'GetAsyncKeyState', return_value=0):
            self.assertFalse(worker._modified())


class HotkeyTests(unittest.TestCase):
    def test_prefers_f8_and_does_not_touch_fallbacks_by_default(self):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.hwnd = 5
        register = MagicMock(return_value=True)
        self.assertEqual(worker.register_hotkey(register), (1, 'F8'))
        register.assert_called_once_with(5, 1, 0x4000, 0x77)

    def test_falls_back_when_f8_is_occupied(self):
        worker = L.LiveInput(queue.Queue(), 123, fallback_hotkey=True)
        worker.hwnd = 5
        register = MagicMock(side_effect=[False, True])
        self.assertEqual(worker.register_hotkey(register), (3, 'Shift+F8'))
        self.assertEqual(register.call_args_list[1][0][2], 0x4004)

    def test_last_fallback_and_total_failure(self):
        worker = L.LiveInput(queue.Queue(), 123, fallback_hotkey=True)
        worker.hwnd = 5
        register = MagicMock(side_effect=[False, False, True])
        self.assertEqual(worker.register_hotkey(register), (4, 'Ctrl+Alt+F8'))
        register.return_value = False
        register.side_effect = None
        self.assertEqual(worker.register_hotkey(register), (0, 'F8'))

    def test_quit_hotkey_fallback(self):
        worker = L.LiveInput(queue.Queue(), 123, fallback_hotkey=True)
        worker.hwnd = 5
        register = MagicMock(side_effect=[False, True])
        self.assertEqual(worker.register_quit_hotkey(register), (5, 'Ctrl+Shift+F8'))
        register = MagicMock(return_value=False)
        self.assertEqual(worker.register_quit_hotkey(register), (0, 'Ctrl+F8'))

    def test_any_registered_hotkey_id_toggles_or_quits(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u:
            u.DefWindowProcW.return_value = 0
            for ident in (1, 3, 4):
                worker._window(5, 0x312, ident, 0)
            self.assertEqual([worker.events.get_nowait() for _ in range(3)],
                             [('toggle', None)] * 3)
            for ident in (2, 5, 6):
                worker._window(5, 0x312, ident, 0)
            self.assertEqual([worker.events.get_nowait() for _ in range(3)],
                             [('quit', None)] * 3)


class AltTriggerTests(unittest.TestCase):
    def test_overlay_mode_keeps_capture_while_alt_is_held(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=False)
        with patch.object(worker, 'focused', return_value=True), \
             patch.object(L, 'U') as u:
            def keys(vk):
                return 0x8000 if vk == 0x12 else 0
            u.GetAsyncKeyState.side_effect = keys
            worker.guard.start(time.monotonic())
            worker.safety()
        self.assertTrue(worker.guard.active)

    def test_overlay_mode_can_begin_while_alt_is_held(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=False)
        with patch.object(worker, 'focused', return_value=True), \
             patch.object(L, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk == 0x12 else 0)
            u.SetWindowsHookExW.side_effect = [10, 11]
            worker.begin()
        self.assertTrue(worker.guard.active)

    def test_fixed_window_still_refuses_start_during_alt(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(worker, 'focused', return_value=True), \
             patch.object(L, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk == 0x12 else 0)
            worker.begin()
        self.assertFalse(worker.guard.active)
        self.assertEqual(worker.events.get_nowait()[0], 'notice')

    def test_alt_release_ends_overlay_capture(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=False)
        worker.guard.start(time.monotonic())
        worker.mouse_hook, worker.key_hook = 10, 11
        vk = W.DWORD(0x12)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 0
            worker._keyboard(0, 0x105, C.addressof(vk))
        self.assertFalse(worker.guard.active)
        self.assertEqual(worker.events.get_nowait(), ('stopped', 'Alt：松开并识别'))
        self.assertEqual(u.UnhookWindowsHookEx.call_count, 2)


class ContinuousTests(unittest.TestCase):
    """连写 keeps the hooks armed across Alt and can free the pointer."""

    def test_alt_key_up_keeps_a_continuous_session_armed(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=False,
                             alt_stops=True)
        worker.continuous = True
        worker.guard.start(time.monotonic())
        worker.mouse_hook, worker.key_hook = 10, 11
        vk = W.DWORD(0x12)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 0
            worker._keyboard(0, 0x101, C.addressof(vk))  # Alt up
        self.assertTrue(worker.guard.active)
        self.assertTrue(worker.events.empty())

    def test_safety_ignores_alt_in_a_continuous_session(self):
        worker = L.LiveInput(queue.Queue(), 123, alt_releases=True)
        worker.continuous = True
        with patch.object(worker, 'focused', return_value=True), \
             patch.object(L, 'U') as u:
            u.GetAsyncKeyState.side_effect = (
                lambda vk: 0x8000 if vk == 0x12 else 0)
            worker.guard.start(time.monotonic())
            worker.safety()
        self.assertTrue(worker.guard.active)

    def test_release_resets_continuous_and_suppression(self):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.continuous, worker.suppress, worker.free_hold = (
            True, False, True)
        worker.guard.start(time.monotonic())
        worker.mouse_hook = worker.key_hook = None
        worker.release('done')
        self.assertFalse(worker.continuous)
        self.assertTrue(worker.suppress)
        self.assertFalse(worker.free_hold)

    def _armed(self, worker):
        worker.guard.start(time.monotonic())
        worker.mouse_hook, worker.key_hook = 10, 11

    def test_unsuppressed_pointer_events_reach_the_app(self):
        worker = L.LiveInput(queue.Queue(), 123)
        self._armed(worker)
        info = L.MSLLHOOKSTRUCT()  # no LLMHF_INJECTED: a physical pad event
        data = C.cast(C.byref(info), C.c_void_p).value
        with patch.object(worker, 'focused', return_value=True), \
             patch.object(L, 'U') as u:
            u.GetAsyncKeyState.return_value = 0
            u.CallNextHookEx.return_value = 778
            worker.suppress = False
            self.assertEqual(worker._mouse(0, 0x200, data), 778)
            worker.suppress = True
            self.assertEqual(worker._mouse(0, 0x200, data), 1)

    def test_wheel_is_forwarded_as_a_zoom_event(self):
        worker = L.LiveInput(queue.Queue(), 123)
        self._armed(worker)
        for raw, expected in ((120, 120), (-120, -120)):
            info = L.MSLLHOOKSTRUCT()
            info.mouseData = (raw & 0xFFFF) << 16
            data = C.cast(C.byref(info), C.c_void_p).value
            with patch.object(worker, 'focused', return_value=True), \
                 patch.object(L, 'U') as u:
                u.GetAsyncKeyState.return_value = 0
                self.assertEqual(worker._mouse(0, 0x020A, data), 1)
            kind, delta = worker.events.get_nowait()
            self.assertEqual((kind, delta), ('wheel', expected))

    def test_injected_pinch_wheel_never_reaches_the_app(self):
        # A precision touchpad turns pinch into Ctrl+wheel, often as injected
        # input. Forwarding it would zoom PowerPoint under our ink.
        worker = L.LiveInput(queue.Queue(), 123)
        self._armed(worker)
        for suppress in (True, False):
            info = L.MSLLHOOKSTRUCT()
            info.mouseData = 120 << 16
            info.flags = L.LLMHF_INJECTED
            data = C.cast(C.byref(info), C.c_void_p).value
            worker.suppress = suppress
            with patch.object(worker, 'focused', return_value=True), \
                 patch.object(L, 'U') as u:
                u.GetAsyncKeyState.return_value = 0
                u.CallNextHookEx.return_value = 778
                self.assertEqual(worker._mouse(0, 0x020A, data), 1)
            self.assertTrue(worker.events.empty())

    def test_free_hold_starts_without_alt_but_normal_hold_does_not(self):
        worker = L.LiveInput(queue.Queue(), 123, start_hotkey=False,
                             alt_hold=True, alt_releases=False,
                             alt_stops=True)
        worker.free_hold = True
        worker.target = 123
        with patch.object(L, 'U') as u:
            u.IsWindow.return_value = True
            u.IsIconic.return_value = False
            u.GetForegroundWindow.return_value = 123
            u.GetAsyncKeyState.return_value = 0
            u.SetWindowsHookExW.side_effect = [10, 11]
            worker.begin()
        self.assertTrue(worker.guard.active)

        normal = L.LiveInput(queue.Queue(), 123, start_hotkey=False,
                             alt_hold=True, alt_releases=False,
                             alt_stops=True)
        normal.target = 123
        with patch.object(L, 'U') as u:
            u.IsWindow.return_value = True
            u.IsIconic.return_value = False
            u.GetForegroundWindow.return_value = 123
            u.GetAsyncKeyState.return_value = 0
            normal.begin()
        self.assertFalse(normal.guard.active)
        self.assertEqual(normal.events.get_nowait()[0], 'notice')


class GuardTests(unittest.TestCase):
    def test_release_conditions(self):
        for kw, now in [({'alt': True}, 1), ({'escape': True}, 1),
                        ({'focused': False}, 1), ({}, 3)]:
            g = L.CaptureGuard()
            g.start(0)
            self.assertIsNotNone(g.reason(now, **kw))
        g = L.CaptureGuard()
        g.start(0)
        g.heartbeat = 92
        self.assertIn('90', g.reason(92))
        g.heartbeat = g.last_input = 601
        self.assertIn('10', g.reason(601))
        g.release()
        self.assertIsNone(g.reason(9999))

    def test_idle_never_suppresses(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.return_value = 777
            self.assertEqual(worker._mouse(0, 0x200, 0), 777)
            u.SetWindowsHookExW.assert_not_called()

    def test_alt_releases_before_next_hook(self):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.guard.start(time.monotonic())
        worker.mouse_hook, worker.key_hook = 10, 11
        vk = W.DWORD(0xA5)  # right Alt, only local memory; not injected
        with patch.object(L, 'U') as u:
            u.CallNextHookEx.side_effect = lambda *args: self.assertFalse(worker.guard.active) or 0
            worker._keyboard(0, 0x104, C.addressof(vk))
            self.assertEqual(u.UnhookWindowsHookEx.call_count, 2)
            self.assertFalse(worker.guard.active)

    def test_partial_hook_failure_rolls_back(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, patch.object(worker, 'focused', return_value=True):
            u.GetAsyncKeyState.return_value = 0
            u.SetWindowsHookExW.side_effect = [1234, 0]
            worker.begin()
            self.assertFalse(worker.guard.active)
            u.UnhookWindowsHookEx.assert_called_once_with(1234)

    def test_safe_preview_cannot_install_hooks(self):
        worker = L.LiveInput(queue.Queue(), 123, allow_capture=False)
        with patch.object(L, 'U') as u:
            worker.begin()
            u.SetWindowsHookExW.assert_not_called()
            self.assertFalse(worker.guard.active)

    def test_shutdown_rejects_queued_start(self):
        worker = L.LiveInput(queue.Queue(), 123)
        worker.stopping.set()
        with patch.object(L, 'U') as u:
            worker.begin()
            u.SetWindowsHookExW.assert_not_called()
            self.assertFalse(worker.guard.active)

    def test_active_mouse_and_focus_release_with_mock_native(self):
        worker = L.LiveInput(queue.Queue(), 123)
        with patch.object(L, 'U') as u, patch.object(worker, 'focused', return_value=True):
            u.GetAsyncKeyState.return_value = 0
            u.SetWindowsHookExW.side_effect = [10, 11]
            u.CallNextHookEx.return_value = 778
            worker.begin()
            self.assertTrue(worker.guard.active)
            self.assertEqual(worker._mouse(0, 0x200, 0), 1)
            self.assertEqual(worker._mouse(-1, 0x200, 0), 778)
            worker.guard.heartbeat -= 10
            self.assertEqual(worker._mouse(0, 0x200, 0), 778)
            self.assertFalse(worker.guard.active)


class SessionTests(unittest.TestCase):
    def test_raw_csv_and_meta_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            s = Session('TEST ONLY', directory)
            frames = [frame(0), frame(.01, flags=1)]
            for f in frames:
                s.write(f)
            s.close('test')
            s.close('test again')
            saved = [json.loads(line) for line in s.base.with_suffix('.jsonl').read_text().splitlines()]
            self.assertEqual(saved, frames)
            metadata = json.loads(s.base.with_suffix('.meta.json').read_text())
            self.assertEqual(metadata['frames'], 2)
            self.assertEqual(metadata['contact_rows'], 2)
            self.assertEqual(len(s.base.with_suffix('.contacts.csv').read_text().splitlines()), 3)

    def test_two_sessions_in_one_clock_tick_do_not_collide(self):
        # The Windows clock can repeat a microsecond stamp; a real tray run
        # died in Session() with FileExistsError when two views started at
        # once. The second session must get its own files.
        with tempfile.TemporaryDirectory() as directory:
            frozen = dt.datetime(2026, 11, 13, 20, 30, 30, 638815)
            with patch('live_window.dt.datetime') as clock:
                clock.now.return_value = frozen
                first = Session('TEST ONLY', directory)
                second = Session('TEST ONLY', directory)
                first.close('first')
                second.close('second')
            self.assertNotEqual(first.base, second.base)
            self.assertTrue(first.base.with_suffix('.jsonl').exists())
            self.assertTrue(second.base.with_suffix('.jsonl').exists())
            self.assertTrue(first.base.with_suffix('.meta.json').exists())
            self.assertTrue(second.base.with_suffix('.meta.json').exists())

    def test_failed_metadata_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            s = Session('TEST ONLY', directory)
            s.write(frame(0))
            with patch.object(Path, 'write_text', side_effect=OSError('test disk failure')):
                with self.assertRaises(OSError):
                    s.close('test')
            self.assertTrue(s.closed)
            self.assertFalse(s.finalized)
            s.close('retry')
            self.assertTrue(s.finalized)
            self.assertEqual(json.loads(s.base.with_suffix('.meta.json').read_text())['frames'], 1)


class ShutdownTests(unittest.TestCase):
    def app(self):
        from live_window import MotionWindow
        app = MotionWindow.__new__(MotionWindow)
        app.closing = app.active = app.pending = False
        app.worker = None
        app.session = None
        app.last_path = ''
        app.events = queue.Queue()
        app.root = MagicMock()
        app.canvas = MagicMock()
        app.badge = MagicMock()
        app.start_btn = MagicMock()
        app.status = MagicMock()
        app.draw = MagicMock()
        app.tracker = L.StrokeTracker()
        app.recording_strokes = []
        app.recording_active = {}
        app.last_capture_strokes = []
        app.recognition_token = 0
        return app

    def test_queued_start_frame_stop_is_saved_on_close(self):
        app = self.app()
        app.events.put(('started', {'path': 'TEST ONLY'}))
        app.events.put(('frame', frame(0)))
        app.events.put(('stopped', 'test'))
        with tempfile.TemporaryDirectory() as directory:
            with patch('live_window.Session', side_effect=lambda device: Session(device, directory)):
                app.close()
            files = list(Path(directory).glob('*.jsonl'))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())['t'], 0)
        app.root.destroy.assert_called_once()

    def test_write_failure_during_close_still_finalizes(self):
        app = self.app()
        app.session = MagicMock()
        app.session.closed = False
        app.session.write.side_effect = OSError('test disk failure')
        app.events.put(('frame', frame(0)))
        with patch('live_window.traceback.print_exception'):
            app.close()
        self.assertGreaterEqual(app.session.close.call_count, 1)
        app.root.destroy.assert_called_once()


class RecognitionTrackingTests(unittest.TestCase):
    def test_stopped_converts_points_for_windows_ink(self):
        app = MotionWindow.__new__(MotionWindow)
        app.closing = False
        app.active = app.pending = True
        app.canvas = MagicMock()
        app.badge = MagicMock()
        app.start_btn = MagicMock()
        app.status = MagicMock()
        app.session = MagicMock()
        app.last_path = ''
        app.tracker = L.StrokeTracker()
        app.recording_active = {0: {'contact': 0, 'points': [(0.1, 10, 20)]}}
        app.recording_strokes = [
            {'contact': 0, 'points': [(0.1, 10, 20), (0.2, 11, 21)]},
            {'contact': 0, 'points': [(0.5, 30, 40)]},
        ]
        app.last_capture_strokes = []
        app.recognition_token = 0
        app.recognize_last = MagicMock()
        session = app.session
        app.event('stopped', 'test')
        self.assertEqual(app.last_capture_strokes,
                         [[(10, 20), (11, 21)], [(30, 40)]])
        session.close.assert_called_once_with('test')
        app.recognize_last.assert_called_once_with()


def snapshot_window(hwnd, path):
    """Print only the off-screen test window, not the user's desktop."""
    from PIL import Image
    gdi = C.WinDLL('gdi32', use_last_error=True)
    user = C.WinDLL('user32', use_last_error=True)
    user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
    user.GetWindowDC.argtypes, user.GetWindowDC.restype = [W.HWND], W.HDC
    user.ReleaseDC.argtypes = [W.HWND, W.HDC]
    user.PrintWindow.argtypes = [W.HWND, W.HDC, W.UINT]
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [W.HDC], W.HDC
    gdi.CreateCompatibleBitmap.argtypes = [W.HDC, C.c_int, C.c_int]
    gdi.CreateCompatibleBitmap.restype = W.HBITMAP
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [W.HDC, W.HANDLE], W.HANDLE
    gdi.DeleteObject.argtypes = [W.HANDLE]
    gdi.DeleteDC.argtypes = [W.HDC]
    gdi.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, W.LPVOID, W.LPVOID, W.UINT]
    rect = W.RECT()
    user.GetWindowRect(hwnd, C.byref(rect))
    width, height = rect.right-rect.left, rect.bottom-rect.top
    dc = user.GetWindowDC(hwnd)
    mem = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    previous = gdi.SelectObject(mem, bitmap)
    try:
        if not user.PrintWindow(hwnd, mem, 2):
            raise RuntimeError('PrintWindow failed')
        gdi.SelectObject(mem, previous)
        import struct
        info = C.create_string_buffer(struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0))
        bits = C.create_string_buffer(width*height*4)
        if not gdi.GetDIBits(mem, bitmap, 0, height, bits, info, 0):
            raise RuntimeError('GetDIBits failed')
        Image.frombytes('RGB', (width, height), bits.raw, 'raw', 'BGRX').save(path)
    finally:
        gdi.SelectObject(mem, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(mem)
        user.ReleaseDC(hwnd, dc)


def visual_check(app):
    """Replay the first recorded character without raw capture or mouse hooks."""
    try:
        assert not app.worker.allow_capture
        assert app.ready, 'Real device / Raw Input registration did not become ready'
        assert not app.worker.guard.active
        app.root.update_idletasks()
        user = C.WinDLL('user32', use_last_error=True)
        user.GetWindowLongPtrW.argtypes, user.GetWindowLongPtrW.restype = [W.HWND, C.c_int], C.c_ssize_t
        user.SetWindowLongPtrW.argtypes, user.SetWindowLongPtrW.restype = [W.HWND, C.c_int, C.c_ssize_t], C.c_ssize_t
        user.SetWindowLongPtrW(app.owner, -20, user.GetWindowLongPtrW(app.owner, -20) | 0x08000000)
        # WM_PRINT cannot paint Tk canvas correctly when entirely off-screen.
        # Briefly show a NOACTIVATE preview, then capture only this window.
        app.root.geometry(f'{app.root.winfo_reqwidth()}x{app.root.winfo_reqheight()}+40+40')
        app.root.deiconify()
        app.badge.config(text='安全回放 · 未捕获鼠标')
        app.status.config(text='自动验证：回放已有采样的第一个「手」并调用 Windows 识别。没有启动鼠标捕获，也没有新采样。')
        records = [json.loads(line) for line in (BASE / 'data' / 'handwrite_01.jsonl').read_text(encoding='utf-8').splitlines()]
        for old in records:
            if old['t'] > 4.7:
                break
            # Explicit legacy conversion for visualization ONLY; old file untouched.
            f = dict(old)
            f['contacts'] = [dict(c, tip=c['inrange'], confidence=c['tip']) for c in old['contacts']]
            app.draw(f)
        assert app.tracker.strokes == 4, app.tracker.strokes
        # Exercise the same Windows Ink path used after a real capture.
        app.last_capture_strokes = [
            [(point[1], point[2]) for point in stroke['points']]
            for stroke in app.recording_strokes if stroke['points']]
        app.recognize_last()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.root.update()
            if not app.recog_label.cget('text').startswith('识别中'):
                break
            time.sleep(0.02)
        assert app.recog_label.cget('text').startswith('识别：手'), app.recog_label.cget('text')
        app.root.update_idletasks()
        app.root.geometry(f'{app.root.winfo_reqwidth()}x{app.root.winfo_reqheight()}+40+40')
        app.root.update()
        png = BASE / 'data' / 'live_window_preview.png'
        snapshot_window(app.owner, png)
        print(json.dumps({'safe_preview': True, 'raw_input_registered': app.ready,
                          'mouse_hooks_installed': bool(app.worker.mouse_hook or app.worker.key_hook),
                          'replay_strokes': app.tracker.strokes, 'replay_frames': app.frame_count,
                          'preview': str(png)}, ensure_ascii=False))
    except BaseException:
        import traceback
        traceback.print_exc()
        app.self_check_failed = True
        app.close()
    else:
        app.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
