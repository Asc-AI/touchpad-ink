import queue
import struct
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import tray_icon as T
import tray_app
from tray_app import Host

BASE = Path(__file__).resolve().parent.parent


class IconTests(unittest.TestCase):
    def test_writes_valid_32px_ico(self):
        with tempfile.TemporaryDirectory() as folder:
            path = T.build_icon(Path(folder) / 'tray.ico')
            data = path.read_bytes()
            self.assertEqual(data[:4], b'\x00\x00\x01\x00')
            count = struct.unpack_from('<H', data, 4)[0]
            self.assertEqual(count, 1)
            width, height, _, _, planes, bits, size, offset = struct.unpack_from(
                '<BBBBHHII', data, 6)
            self.assertEqual((width, height, planes, bits), (32, 32, 1, 32))
            self.assertEqual(offset, 22)
            self.assertEqual(len(data), 22 + size)
            self.assertEqual(struct.unpack_from('<I', data, offset)[0], 40)

    def test_icon_loads_with_transparency(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = T.build_icon(Path(folder) / 'tray.ico')
            with Image.open(path) as source:
                image = source.convert('RGBA')
            self.assertEqual(image.size, (32, 32))
            alpha = image.getchannel('A').getextrema()
            self.assertEqual(alpha, (0, 255))
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            edge = image.getpixel((16, 8))
            self.assertTrue(min(edge[:3]) > 150, edge)  # touchpad outline
            ink = image.getpixel((12, 15))
            self.assertTrue(ink[0] > 240 and 130 < ink[1] < 190 and ink[2] < 140,
                            ink)  # coral handwriting stroke

    def test_existing_file_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tray.ico'
            T.build_icon(path)
            before = path.stat().st_mtime_ns
            T.build_icon(path)
            self.assertEqual(path.stat().st_mtime_ns, before)


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.commands = queue.Queue()
        self.tray = T.TrayIcon(self.commands)

    def test_menu_commands_map_to_host_commands(self):
        self.tray.handle_menu(T.MENU_AUTO)
        self.tray.handle_menu(T.MENU_TYPING)
        self.tray.handle_menu(T.MENU_DRAW)
        self.tray.handle_menu(T.MENU_PPT)
        self.tray.handle_menu(T.MENU_NOTE)
        self.tray.handle_menu(T.MENU_STARTUP)
        self.tray.handle_menu(T.MENU_CLEAR)
        self.tray.handle_menu(T.MENU_QUIT)
        self.tray.handle_menu(999)
        self.assertEqual([self.commands.get_nowait() for _ in range(8)],
                         [('mode', 'auto'), ('mode', 'typing'),
                          ('mode', 'draw'), ('mode', 'ppt'),
                          ('mode', 'note'), ('startup',), ('clear',),
                          ('quit',)])
        self.assertTrue(self.commands.empty())

    def test_startup_item_reflects_the_checker(self):
        self.assertFalse(self.tray.startup_enabled())
        self.tray.startup_check = lambda: True
        self.assertTrue(self.tray.startup_enabled())
        self.tray.startup_check = lambda: 1 / 0
        self.assertFalse(self.tray.startup_enabled())

    def test_set_mode_is_posted_to_the_icon_thread(self):
        self.tray.hwnd = 123
        with patch.object(T.U, 'PostMessageW') as post:
            self.tray.set_mode('auto')
        post.assert_called_once_with(123, T.WM_MODE, 0, 0)
        self.assertEqual(self.tray.mode, 'auto')
        with patch.object(T.U, 'PostMessageW') as post:
            self.tray.set_mode('draw')
        post.assert_called_once_with(123, T.WM_MODE, 2, 0)
        self.assertEqual(self.tray.mode, 'draw')
        with patch.object(T.U, 'PostMessageW') as post:
            self.tray.set_mode('note')
        post.assert_called_once_with(123, T.WM_MODE, 4, 0)
        self.assertEqual(self.tray.mode, 'note')

    def test_tooltip_shows_mode(self):
        self.assertEqual(self.tray._tip(), '触控板手写 · 自动识别')
        self.tray.set_mode('typing')
        self.assertEqual(self.tray._tip(), '触控板手写 · 局部打字')
        self.tray.set_mode('draw')
        self.assertEqual(self.tray._tip(), '触控板手写 · 全域画图')
        self.tray.set_mode('ppt')
        self.assertEqual(self.tray._tip(), '触控板手写 · PPT 墨迹')
        self.tray.set_mode('note')
        self.assertEqual(self.tray._tip(), '触控板手写 · Note 墨迹')

    def test_icon_rect_without_window(self):
        self.assertIsNone(self.tray.icon_rect())

    def test_mode_constants_are_distinct(self):
        ids = {T.MENU_AUTO, T.MENU_TYPING, T.MENU_DRAW, T.MENU_PPT,
               T.MENU_NOTE, T.MENU_STARTUP, T.MENU_CLEAR, T.MENU_QUIT}
        self.assertEqual(len(ids), 8)

    def test_every_mode_has_an_index_and_label(self):
        self.assertEqual(set(T.MODE_INDEX), set(T.MODE_ORDER))
        self.assertEqual(set(T.MODE_LABELS), set(T.MODE_ORDER))


class InstanceTests(unittest.TestCase):
    def test_existing_tray_finds_the_hidden_window(self):
        with patch.object(tray_app, 'FindWindowW', return_value=77):
            self.assertTrue(tray_app.existing_tray())
        with patch.object(tray_app, 'FindWindowW', return_value=0):
            self.assertFalse(tray_app.existing_tray())

    def test_existing_tray_fails_open(self):
        with patch.object(tray_app, 'FindWindowW',
                          side_effect=OSError('nope')):
            self.assertFalse(tray_app.existing_tray())


class FakeTray:
    def __init__(self):
        self.started = self.stopped = False
        self.mode = None

    def start(self):
        self.started = True

    def set_mode(self, mode):
        self.mode = mode

    def stop(self):
        self.stopped = True


class FakeView:
    def __init__(self, name, root, log, trigger_enabled=True):
        self.name = self.kind = name
        self.root = root
        self.log = log
        self.trigger_enabled = trigger_enabled
        self.auto_pending = False
        self.continuous = False
        self.state = 'idle'
        self.log.append(('create', name, trigger_enabled))

    def enter_continuous(self):
        self.continuous = True
        self.log.append(('continuous', self.name))
        return True

    def preview_region(self):
        self.log.append(('preview', self.name))
        return True

    def exit_continuous(self, message='已退出连写'):
        self.continuous = False
        self.log.append(('exit', self.name))

    def close(self):
        self.log.append(('close', self.name))
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def clear_annotation(self):
        self.log.append(('clear', self.name))


@unittest.skipUnless(sys.platform == 'win32', 'Tk host windows')
class HostTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.log = []
        self.tray = FakeTray()
        views = {
            name: (lambda n: lambda win, trigger_enabled=True: FakeView(
                n, win, self.log, trigger_enabled))(name)
            for name in ('typing', 'draw', 'ppt', 'note')
        }
        self.host = Host(self.root, safe_preview=False, tray=self.tray,
                         views=views)

    def tearDown(self):
        if not self.host.closing:
            self.host.quit()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_initial_mode_is_auto_with_a_parked_view(self):
        self.assertTrue(self.tray.started)
        self.assertEqual(self.host.mode, 'auto')
        # Parked so Ctrl+F8 exists before the first Alt press.
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)
        self.assertEqual(self.tray.mode, 'auto')
        self.assertEqual(self.log, [('create', 'typing', False)])

    def test_switch_closes_old_view_and_updates_tray(self):
        self.host.switch('draw')
        old = self.host.view
        self.assertEqual((self.host.mode, self.host.view.name),
                         ('draw', 'draw'))
        self.host.switch('ppt')
        self.assertIn(('close', 'draw'), self.log)
        self.assertEqual(self.host.mode, 'ppt')
        self.assertEqual(self.host.view.name, 'ppt')
        self.assertIsNot(self.host.view, old)
        self.assertEqual(self.tray.mode, 'ppt')
        self.assertTrue(self.host.view.trigger_enabled)

    def test_switching_to_current_mode_is_a_noop(self):
        self.host.switch('draw')
        created = len([item for item in self.log if item[0] == 'create'])
        self.host.switch('draw')
        self.assertEqual(
            len([item for item in self.log if item[0] == 'create']), created)

    def test_unknown_mode_is_ignored(self):
        self.host.switch('video')
        self.assertEqual(self.host.mode, 'auto')
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)

    def test_every_view_mode_is_reachable(self):
        for mode in ('typing', 'draw', 'ppt', 'note'):
            self.host.switch(mode)
            self.assertEqual(self.host.mode, mode)
            self.assertEqual(self.host.view.name, mode)
            self.assertEqual(self.tray.mode, mode)
        self.host.switch('auto')
        self.assertEqual(self.host.mode, 'auto')
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)

    def test_clear_only_reaches_draw_mode(self):
        self.host.handle(('clear',))
        self.assertFalse(any(item[0] == 'clear' for item in self.log))
        self.host.switch('draw')
        self.host.handle(('clear',))
        self.assertEqual(self.log[-1], ('clear', 'draw'))
        self.host.switch('typing')
        self.host.handle(('clear',))
        self.assertNotEqual(self.log[-1][0], 'clear')

    def test_startup_command_toggles_autostart(self):
        with patch.object(tray_app.autostart, 'toggle',
                          return_value=True) as toggle:
            self.host.handle(('startup',))
        toggle.assert_called_once_with()
        with patch.object(tray_app.autostart, 'toggle', return_value=None):
            self.assertIsNone(self.host.toggle_startup())

    def test_quit_command_stops_view_and_tray(self):
        self.host.switch('typing')
        self.host.handle(('quit',))
        self.assertTrue(self.host.closing)
        self.assertTrue(self.tray.stopped)
        self.assertIn(('close', 'typing'), self.log)

    def _press(self, start=10.0, kind='typing', modifiers=()):
        def key_state(vk):
            if vk == 0x12:
                return 0x8000
            return 0x8000 if vk in modifiers else 0
        with patch.object(tray_app.U, 'GetAsyncKeyState',
                          side_effect=key_state), \
             patch.object(tray_app, 'foreground_kind', return_value=kind):
            return [self.host.auto_press(start),
                    self.host.auto_press(start + 0.10),
                    self.host.auto_press(start + 0.30)]

    def _release(self, at=15.0):
        with patch.object(tray_app.U, 'GetAsyncKeyState', return_value=0), \
             patch.object(tray_app, 'foreground_kind') as kind:
            result = self.host.auto_press(at)
        kind.assert_not_called()
        return result

    def test_auto_press_creates_the_matching_view_once(self):
        self.assertEqual(self._press(10.0, 'typing'), [False, False, True])
        self.assertEqual(self.host.mode, 'auto')
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)
        self.assertTrue(self.host.view.auto_pending)
        self.assertFalse(self._release(15.0))
        self.assertEqual(self._press(20.0, 'typing'), [False, False, True])
        self.assertEqual(len([item for item in self.log
                              if item[0] == 'create']), 1)

    def test_alt_down_previews_the_ppt_region_before_the_gate(self):
        with patch.object(tray_app.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0), \
             patch.object(tray_app.U, 'GetForegroundWindow',
                          return_value=500), \
             patch.object(tray_app, 'foreground_kind',
                          return_value='ppt') as kind:
            self.assertFalse(self.host.auto_press(10.0))
            self.assertEqual(self.host.view.kind, 'ppt')
            self.assertIn(('preview', 'ppt'), self.log)
            self.assertFalse(self.host.view.auto_pending)
            self.assertFalse(self.host.auto_press(10.1))
            self.assertTrue(self.host.auto_press(10.3))
        kind.assert_called_once()  # the gate reuses the cached classification
        self.assertTrue(self.host.view.auto_pending)

    def test_text_press_does_not_preview_or_open_ppt(self):
        with patch.object(tray_app.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0), \
             patch.object(tray_app, 'foreground_kind',
                          return_value='typing'), \
             patch.object(tray_app.U, 'GetForegroundWindow', return_value=500):
            self.host.auto_press(10.0)
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertNotIn(('preview', 'typing'), self.log)
        self.assertEqual(
            len([item for item in self.log if item[0] == 'create']), 1)

    def test_auto_press_switches_when_the_foreground_changes(self):
        self._press(10.0, 'typing')
        self._release(15.0)
        self._press(20.0, 'note')
        self.assertIn(('close', 'typing'), self.log)
        self.assertEqual(self.host.view.kind, 'note')
        self.assertTrue(self.host.view.auto_pending)

    def test_auto_press_without_a_caret_does_nothing(self):
        self.assertFalse(any(self._press(10.0, None)))
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.auto_pending)
        self.assertEqual(len([item for item in self.log
                              if item[0] == 'create']), 1)

    def test_auto_press_does_not_steal_a_live_capture(self):
        self._press(10.0, 'typing')
        self.host.view.state = 'capturing'
        self._release(15.0)
        self.assertFalse(any(self._press(20.0, 'ppt')))
        self.assertEqual(self.host.view.kind, 'typing')

    def test_auto_press_is_blocked_by_alt_shortcuts(self):
        self.assertFalse(any(self._press(10.0, 'typing', modifiers=(0x09,))))
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)

    def test_manual_mode_disables_auto_selection(self):
        self.host.switch('typing')
        with patch.object(tray_app.U, 'GetAsyncKeyState',
                          side_effect=lambda vk:
                          0x8000 if vk == 0x12 else 0), \
             patch.object(tray_app, 'foreground_kind') as kind:
            self.host.tick()
        kind.assert_not_called()
        self.assertTrue(self.host.view.trigger_enabled)

    def _tap_twice(self, start, kind='typing'):
        """Two short Alt taps: down/up/down/up inside the double window."""
        calls = [(start, True), (start + 0.08, False),
                 (start + 0.20, True), (start + 0.28, False)]
        results = []
        with patch.object(tray_app, 'foreground_kind', return_value=kind):
            for now, down in calls:
                with patch.object(
                        tray_app.U, 'GetAsyncKeyState',
                        side_effect=lambda vk, d=down:
                        0x8000 if d and vk == 0x12 else 0):
                    results.append(self.host.auto_press(now))
        return results

    def test_double_tap_enters_continuous_in_the_matching_view(self):
        self.assertEqual(self._tap_twice(10.0), [False, False, False, True])
        self.assertIn(('continuous', 'typing'), self.log)
        self.assertTrue(self.host.view.continuous)
        self.assertEqual(self.host.view.kind, 'typing')
        self.assertFalse(self.host.view.trigger_enabled)

    def test_double_tap_exits_continuous_from_the_host(self):
        self._tap_twice(10.0)
        self.host.view.state = 'capturing'
        self._tap_twice(20.0)
        self.assertIn(('exit', 'typing'), self.log)
        self.assertFalse(self.host.view.continuous)
        # The host owns the exit even though auto views ignore the gesture.
        self.assertFalse(self.host.view.trigger_enabled)

    def test_two_long_presses_exit_continuous_from_the_host(self):
        self._tap_twice(10.0)
        self.host.view.state = 'capturing'
        for now, down in ((20.0, True), (20.30, False), (20.40, True)):
            with patch.object(
                    tray_app.U, 'GetAsyncKeyState',
                    side_effect=lambda vk, d=down:
                    0x8000 if d and vk == 0x12 else 0):
                self.host.auto_press(now)
        self.assertIn(('exit', 'typing'), self.log)
        self.assertFalse(self.host.view.continuous)

    def test_double_tap_on_the_draw_mode_is_ignored(self):
        self.host.switch('draw')
        self.assertEqual(self._tap_twice(10.0, 'draw'),
                         [False, False, False, False])
        self.assertNotIn(('continuous', 'draw'), self.log)


if __name__ == '__main__':
    unittest.main()
