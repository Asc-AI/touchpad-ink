import ctypes as C
from ctypes import wintypes as W
import unittest
from unittest.mock import patch

import auto_mode as A


class ClassifyTests(unittest.TestCase):
    def test_onenote_wins_even_with_a_caret(self):
        self.assertEqual(A.classify('C:/Program Files/ONENOTE.EXE',
                                    'Framework::CFrame', caret=True), 'note')

    def test_powerpoint_slide_show_class(self):
        self.assertEqual(A.classify('powerpnt.exe', 'screenClass',
                                    fullscreen=True), 'ppt')

    def test_powerpoint_fullscreen_window(self):
        self.assertEqual(A.classify('POWERPNT.EXE', 'PPTFrameClass',
                                    fullscreen=True), 'ppt')

    def test_powerpoint_editor_with_caret_is_typing(self):
        self.assertEqual(A.classify('powerpnt.exe', 'PPTFrameClass',
                                    fullscreen=False, caret=True), 'typing')

    def test_powerpoint_editor_without_caret_does_nothing(self):
        self.assertIsNone(A.classify('powerpnt.exe', 'PPTFrameClass',
                                     fullscreen=False))

    def test_wps_presentation_fullscreen(self):
        self.assertEqual(A.classify('wpp.exe', 'WppShowWnd', fullscreen=True),
                         'ppt')

    def test_other_fullscreen_app_is_not_ppt(self):
        self.assertIsNone(A.classify('vlc.exe', 'Qt5QWindow', fullscreen=True))

    def test_caret_anywhere_else_is_typing(self):
        self.assertEqual(A.classify('notepad.exe', 'Notepad', caret=True),
                         'typing')
        self.assertEqual(A.classify('msedge.exe', 'Chrome_WidgetWin_1',
                                    edit=True), 'typing')

    def test_plain_window_does_nothing(self):
        self.assertIsNone(A.classify('explorer.exe', 'CabinetWClass'))

    def test_uia_edit_is_typing_even_in_a_browser(self):
        self.assertEqual(A.classify('msedge.exe', 'Chrome_WidgetWin_1',
                                    uia=A.UIA_EDIT), 'typing')
        self.assertEqual(A.classify('qoder.exe', 'Chrome_WidgetWin_1',
                                    uia=A.UIA_EDIT), 'typing')

    def test_uia_document_is_typing_in_an_editor(self):
        self.assertEqual(A.classify('winword.exe', 'OpusApp',
                                    uia=A.UIA_DOCUMENT), 'typing')
        self.assertEqual(A.classify('qoder.exe', 'Chrome_WidgetWin_1',
                                    uia=A.UIA_DOCUMENT), 'typing')

    def test_uia_document_in_a_browser_does_nothing(self):
        self.assertIsNone(A.classify('chrome.exe', 'Chrome_WidgetWin_1',
                                     uia=A.UIA_DOCUMENT))
        self.assertIsNone(A.classify('msedge.exe', 'Chrome_WidgetWin_1',
                                     uia=A.UIA_DOCUMENT))

    def test_qq_window_types_without_a_caret(self):
        # The QQ message box exposes no Win32 caret and reports its focused
        # element as Group/Pane, so the Chromium window class is the only cue.
        self.assertEqual(A.classify('qq.exe', 'Chrome_WidgetWin_1'), 'typing')
        self.assertEqual(A.classify('QQ.EXE', 'Chrome_WidgetWin_1',
                                    uia=50026), 'typing')
        self.assertIsNone(A.classify('qq.exe', 'TXGuiFoundation'))

    def test_wechat_qt_window_types_without_a_caret(self):
        # WeChat 4.x draws the whole UI in one Qt window: no Win32 caret and
        # no UIA Edit, but Unicode SendInput does reach the message box.
        self.assertEqual(A.classify('weixin.exe', 'Qt51514QWindowIcon'),
                         'typing')
        self.assertEqual(A.classify('WEIXIN.EXE', 'Qt51514QWindowIcon',
                                    uia=50026), 'typing')
        self.assertIsNone(A.classify('weixin.exe', 'Chrome_WidgetWin_1'))
        self.assertIsNone(A.classify('other.exe', 'Qt51514QWindowIcon'))

    def test_onenote_and_slideshow_still_win_over_uia(self):
        self.assertEqual(A.classify('onenote.exe', 'x', uia=A.UIA_EDIT),
                         'note')
        self.assertEqual(A.classify('powerpnt.exe', 'screenClass',
                                    fullscreen=True, uia=A.UIA_DOCUMENT),
                         'ppt')

    def test_basename_handles_windows_paths(self):
        self.assertEqual(A.basename('C:\\Program Files\\x\\OneNote.EXE'),
                         'onenote.exe')
        self.assertEqual(A.basename(''), '')


class CaretRectTests(unittest.TestCase):
    def test_thin_bar_is_the_exact_caret(self):
        # Measured in Qoder's Chromium composer after typing "abc中".
        self.assertEqual(A.caret_from_rects([(749.0, 1306.0, 1.0, 26.0)]),
                         (750.0, 1332.0, 26.0))

    def test_selection_ends_at_its_last_line(self):
        self.assertEqual(
            A.caret_from_rects([(10.0, 20.0, 100.0, 20.0),
                                (10.0, 40.0, 30.0, 20.0)], 'abc'),
            (40.0, 60.0, 20.0))

    def test_empty_field_line_box_falls_back_to_its_start(self):
        # Measured on an empty composer: the whole 3-line box, caret at start.
        self.assertEqual(A.caret_from_rects([(681.0, 1283.0, 1151.0, 78.0)]),
                         (682.0, 1315.0, 32.0))

    def test_single_line_box_keeps_its_full_height(self):
        self.assertEqual(A.caret_from_rects([(10.0, 20.0, 200.0, 24.0)]),
                         (11.0, 44.0, 24.0))

    def test_unknown_geometry(self):
        self.assertIsNone(A.caret_from_rects([]))
        self.assertIsNone(A.caret_from_rects([(1.0, 2.0, 3.0, 0.0)]))


class SelectorTests(unittest.TestCase):
    def test_needs_the_hold_gate_then_fires_once(self):
        selector = A.AutoSelector()
        self.assertFalse(selector.poll(10.0, True))
        self.assertFalse(selector.poll(10.10, True))
        self.assertFalse(selector.poll(10.27, True))  # still under the gate
        self.assertTrue(selector.poll(10.30, True))
        self.assertFalse(selector.poll(10.35, True))
        self.assertFalse(selector.poll(10.90, True))

    def test_release_rearms_the_next_press(self):
        selector = A.AutoSelector()
        selector.poll(10.0, True)
        self.assertFalse(selector.poll(10.2, True))  # a tap, not a hold
        self.assertTrue(selector.poll(10.3, True))
        self.assertFalse(selector.poll(10.4, False))
        self.assertFalse(selector.poll(20.0, True))
        self.assertTrue(selector.poll(20.3, True))

    def test_modifiers_block_the_whole_press(self):
        selector = A.AutoSelector()
        self.assertFalse(selector.poll(10.0, True, blocked=True))
        self.assertFalse(selector.poll(10.5, True, blocked=False))
        self.assertFalse(selector.poll(11.0, False))
        self.assertFalse(selector.poll(20.0, True, blocked=False))
        self.assertTrue(selector.poll(20.3, True))

    def test_a_late_modifier_also_kills_the_press(self):
        selector = A.AutoSelector()
        self.assertFalse(selector.poll(10.0, True))
        self.assertFalse(selector.poll(10.1, True, blocked=True))
        self.assertFalse(selector.poll(10.5, True))
        self.assertFalse(selector.poll(11.0, True))

    def test_short_tap_never_fires(self):
        selector = A.AutoSelector()
        selector.poll(10.0, True)
        self.assertFalse(selector.poll(10.10, True))
        self.assertFalse(selector.poll(10.12, False))
        self.assertFalse(selector.poll(11.0, False))

    def test_reset_forgets_a_held_alt(self):
        selector = A.AutoSelector()
        selector.poll(10.0, True)
        selector.reset()
        self.assertFalse(selector.poll(10.2, True))
        self.assertTrue(selector.poll(10.5, True))

    def test_block_keys_cover_alt_tab_and_f4(self):
        for key in (0x11, 0x10, 0x5B, 0x5C, 0x09, 0x20, 0x73):
            self.assertIn(key, A.AutoSelector.BLOCK_KEYS)


class ProcessMatchTests(unittest.TestCase):
    def test_prefix_match_is_casefolded(self):
        with patch.object(A, 'process_name', return_value='onenote.exe'):
            self.assertTrue(A.process_matches(1, 'ONENOTE'))
            self.assertTrue(A.process_matches(1, 'onenote.exe'))
            self.assertFalse(A.process_matches(1, 'powerpnt.exe'))

    def test_missing_name_never_matches(self):
        with patch.object(A, 'process_name', return_value=''):
            self.assertFalse(A.process_matches(1, 'onenote'))
            self.assertFalse(A.process_matches(1, ''))


class ResolveTests(unittest.TestCase):
    def test_non_frame_window_is_returned_unchanged(self):
        with patch.object(A, 'window_class', return_value='Notepad'):
            self.assertEqual(A.resolve_uwp(123), 123)

    def test_missing_window_stays_zero(self):
        self.assertEqual(A.resolve_uwp(0), 0)


class SharedBindingTests(unittest.TestCase):
    def test_api_does_not_mutate_the_shared_user32_argtypes(self):
        # Views load the same raw_probe.user32; a shared argtypes change made
        # auto mode raise ArgumentError once draw_window was imported.
        before = A.U.GetAsyncKeyState.argtypes
        A.api('GetAsyncKeyState', W.SHORT, C.c_int)
        self.assertEqual(A.U.GetAsyncKeyState.argtypes, before)

    def test_struct_calls_survive_another_modules_argtypes(self):
        class OtherInfo(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT),
                        ('rcWork', W.RECT), ('dwFlags', W.DWORD)]

        class OtherThreadInfo(C.Structure):
            _fields_ = [('cbSize', W.DWORD)]

        A.U.GetMonitorInfoW.argtypes = [W.HANDLE, C.POINTER(OtherInfo)]
        A.U.GetGUIThreadInfo.argtypes = [W.DWORD, C.POINTER(OtherThreadInfo)]
        try:
            self.assertFalse(A.is_fullscreen(0))
            # The boolean depends on the live foreground thread; the point is
            # that the private bindings survive another module's argtypes.
            self.assertIsInstance(A.caret_present(0), bool)
        finally:
            A.U.GetMonitorInfoW.argtypes = None
            A.U.GetGUIThreadInfo.argtypes = None


if __name__ == '__main__':
    unittest.main()
