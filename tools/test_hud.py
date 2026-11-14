import sys
import tkinter as tk
import unittest
from unittest.mock import patch

import hud


@unittest.skipUnless(sys.platform == 'win32', 'Win32 overlay windows')
class StyleTests(unittest.TestCase):
    def test_no_activate_is_click_through_and_never_focuses(self):
        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.withdraw()
        try:
            hwnd = hud._no_activate(win)
            self.assertTrue(hwnd)
            style = hud.U.GetWindowLongPtrW(hwnd, hud.GWL_EXSTYLE)
            self.assertTrue(style & hud.WS_EX_TRANSPARENT)
            self.assertTrue(style & hud.WS_EX_NOACTIVATE)
            self.assertTrue(style & hud.WS_EX_TOOLWINDOW)
        finally:
            root.destroy()


@unittest.skipUnless(sys.platform == 'win32', 'Tk overlay windows')
class RegionFrameTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.frame = hud.RegionFrame(self.root)

    def tearDown(self):
        self.frame.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_tiny_rect_never_shows(self):
        self.frame.show((10, 10, 2, 2))
        self.assertEqual(self.frame.win.state(), 'withdrawn')

    def test_hide_and_close_are_idempotent(self):
        self.frame.hide()
        self.frame.hide()
        self.frame.close()
        self.frame.close()

    def test_status_is_drawn_inside_the_frame_near_the_bottom_left(self):
        self.frame.show((100, 100, 533, 332), ['PPT 墨迹 · 准备写字',
                                              '区域 533×332'])
        self.root.update_idletasks()
        self.assertEqual(self.frame.status, ('PPT 墨迹 · 准备写字',
                                             '区域 533×332'))
        left, top, right, bottom = self.frame.canvas.bbox('status')
        self.assertGreaterEqual(left, 0)
        self.assertLessEqual(right, 533)
        self.assertGreater(top, 332 / 2)  # below the pen-down anchor
        self.assertLessEqual(bottom, 332)
        self.frame.set_status(())
        self.assertIsNone(self.frame.canvas.bbox('status'))

    def test_extra_regions_are_outlined_in_one_compound_window(self):
        # Each pen-down re-anchors the region; while the frame is visible the
        # replaced boxes stay outlined around the current (filled) one.
        self.frame.show((100, 100, 400, 250),
                        extra=[(300, 200, 400, 250)])
        self.root.update_idletasks()
        self.assertEqual(self.frame.canvas.winfo_reqwidth(), 600)
        self.assertEqual(self.frame.canvas.winfo_reqheight(), 350)
        boxes = self.frame.canvas.find_withtag('box')
        self.assertEqual(len(boxes), 3)  # one fill + two outlines
        kinds = [self.frame.canvas.type(item) for item in boxes]
        self.assertEqual(kinds.count('polygon'), 1)  # the filled current box
        self.assertEqual(kinds.count('line'), 2)     # rounded outlines
        self.assertIsNone(self.frame.canvas.bbox('status'))
        self.frame.hide()

    def test_region_corners_are_rounded_not_square(self):
        self.frame.show((100, 100, 400, 250))
        self.root.update_idletasks()
        outline = [item for item in self.frame.canvas.find_withtag('box')
                   if self.frame.canvas.type(item) == 'line'][0]
        coords = self.frame.canvas.coords(outline)
        points = list(zip(coords[0::2], coords[1::2]))
        self.assertEqual(float(self.frame.canvas.itemcget(outline, 'width')), 4)
        self.assertGreater(len(points), 20)
        # No sample sits on the sharp top-left corner; the nearest one is on
        # the arc, a fraction of the radius away from it.
        corner = min(((x - 2.0) ** 2 + (y - 2.0) ** 2) ** 0.5
                     for x, y in points)
        self.assertGreater(corner, 2.0)

    def test_a_tiny_extra_region_is_ignored(self):
        self.frame.show((100, 100, 400, 250), extra=[(0, 0, 2, 2)])
        self.root.update_idletasks()
        self.assertEqual(self.frame.canvas.winfo_reqwidth(), 400)
        self.frame.hide()


@unittest.skipUnless(sys.platform == 'win32', 'Tk overlay windows')
class StatusHudTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.hud = hud.StatusHud(self.root)

    def tearDown(self):
        self.hud.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_starts_hidden(self):
        self.assertEqual(self.hud.win.state(), 'withdrawn')

    def test_the_card_has_four_rounded_corner_masks(self):
        self.assertEqual(len(self.hud.masks), 4)
        for mask in self.hud.masks:
            self.assertEqual(mask.winfo_manager(), 'place')
            self.assertEqual(str(mask.cget('bg')).lower(), hud.KEY)
            self.assertEqual([mask.type(item) for item in mask.find_all()],
                             ['arc', 'arc'])  # card fill + border arc
        self.assertEqual(
            str(self.hud.win.wm_attributes('-transparentcolor')).lower(),
            hud.KEY)

    def test_stale_token_does_not_hide_a_newer_card(self):
        token = self.hud.token
        self.hud.hide(token + 5)
        self.assertEqual(self.hud.token, token)
        self.hud.hide()
        self.assertEqual(self.hud.token, token + 1)

    def test_lines_are_joined_and_empty_lines_skipped(self):
        with patch.object(hud, '_cursor', return_value=(10, 10)), \
             patch.object(hud, '_work_area',
                          return_value=(0, 0, 1920, 1080)):
            self.hud.show('t', ['a', '', 'b'], seconds=0.01)
        self.assertEqual(self.hud.title.cget('text'), 't')
        self.assertEqual(self.hud.body.cget('text'), 'a\nb')
        self.hud.hide()


if __name__ == '__main__':
    unittest.main()
