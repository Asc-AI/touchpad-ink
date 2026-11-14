"""Status HUD and the light-blue writing-region frame for the ink views.

Both are borderless, topmost, click-through tool windows that never take
focus. The HUD is a small translucent card shown at the pointer; the region
frame is a transparent window with only a light-blue border (plus a light
stipple fill) so the slide underneath stays readable. Every corner uses the
Win11 radius (8 logical px, ``RADIUS``), sampled from real arcs so the frame
and the chip look native next to Windows 11 apps.
"""
import ctypes as C
from ctypes import wintypes as W
import math
import tkinter as tk

from live_input import U

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

KEY = '#010203'          # color key for the transparent frame window
HUD_BG = '#eaf5ff'
HUD_EDGE = '#7fb8e6'
HUD_TITLE = '#123a56'
HUD_TEXT = '#33546c'
FRAME_EDGE = '#7cc5f0'   # light enough to read the slide through
FRAME_FILL = '#dcefff'
RADIUS = 8               # logical Win11 corner radius, scaled at draw time


def _round_points(x0, y0, x1, y1, radius, steps=8):
    """Perimeter points (clockwise) of a Win11-style rounded rectangle."""
    x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
    r = max(0.0, min(float(radius), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r < 1.0:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    corners = (
        (x1 - r, y0 + r, -90.0),   # top-right
        (x1 - r, y1 - r, 0.0),     # bottom-right
        (x0 + r, y1 - r, 90.0),    # bottom-left
        (x0 + r, y0 + r, 180.0),   # top-left
    )
    points = []
    for cx, cy, start in corners:
        for i in range(steps + 1):
            angle = math.radians(start + 90.0 * i / steps)
            points.append((cx + r * math.cos(angle),
                           cy + r * math.sin(angle)))
    return points


def round_rect(canvas, box, radius, fill=None, outline=None, width=1,
               stipple=None, tags=()):
    """Draw one rounded rectangle on a canvas; return its item ids.

    A filled polygon handles translucency (stipple), and a round-joined
    polyline gives the border smooth corners. Both follow the same sampled
    perimeter so the fill and edge line up exactly.
    """
    points = _round_points(box[0], box[1], box[2], box[3], radius)
    items = []
    if fill:
        extra = {'stipple': stipple} if stipple else {}
        items.append(canvas.create_polygon(
            points, fill=fill, outline='', tags=tags, **extra))
    if outline:
        items.append(canvas.create_line(
            points + [points[0]], fill=outline, width=width,
            joinstyle='round', capstyle='round', tags=tags))
    return items


def round_card_corners(widget, radius, fill, edge, key):
    """Round a square Tk widget's four corners with transparent masks.

    The window must already be transparent for ``key``. Each small overlay
    canvas paints the key over the square corner (so it disappears), then puts
    the card colour back inside the quarter circle and redraws the border arc.
    Works with packed children: the overlays are placed, not packed.
    """
    radius = max(2, int(radius))
    masks = []
    corners = (
        (0.0, 0.0, (0, 0, 2 * radius, 2 * radius), 90),
        (1.0, 0.0, (-radius, 0, radius, 2 * radius), 0),
        (0.0, 1.0, (0, -radius, 2 * radius, radius), 180),
        (1.0, 1.0, (-radius, -radius, radius, radius), 270),
    )
    for relx, rely, bbox, start in corners:
        mask = tk.Canvas(widget, width=radius, height=radius, bg=key,
                         highlightthickness=0, bd=0)
        mask.place(relx=relx, rely=rely,
                   x=0 if relx == 0 else -radius,
                   y=0 if rely == 0 else -radius,
                   width=radius, height=radius)
        mask.create_arc(*bbox, start=start, extent=90, style=tk.PIESLICE,
                        fill=fill, outline=fill)
        mask.create_arc(*bbox, start=start, extent=90, style=tk.ARC,
                        outline=edge, width=1)
        masks.append(mask)
    return masks


def _no_activate(win):
    """Click-through / tool window / never activated, on a Toplevel."""
    try:
        win.update_idletasks()
        hwnd = U.GetAncestor(win.winfo_id(), GA_ROOT)
        style = U.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        U.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                            style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                            | WS_EX_TOOLWINDOW)
        return hwnd
    except BaseException:
        return None


def _ui_scale():
    from ime_window import ui_scale
    return ui_scale()


def _work_area(x, y):
    from ime_window import work_area
    return work_area(x, y)


def _cursor():
    point = W.POINT()
    U.GetCursorPos(C.byref(point))
    return point.x, point.y


class StatusHud:
    """Mode/state card that appears next to the pointer for a moment."""

    def __init__(self, root):
        self.root = root
        self.token = 0
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.wm_attributes('-topmost', True)
        self.win.wm_attributes('-alpha', 0.94)
        self.win.configure(bg=KEY)
        self.win.wm_attributes('-transparentcolor', KEY)
        scale = _ui_scale()
        self.frame = tk.Frame(self.win, bg=HUD_BG, bd=0,
                              highlightthickness=1,
                              highlightbackground=HUD_EDGE)
        self.frame.pack(fill='both', expand=True)
        self.title = tk.Label(self.frame, text='', bg=HUD_BG, fg=HUD_TITLE,
                              font=('Microsoft YaHei UI', max(9, int(9 * scale)),
                                    'bold'),
                              anchor='w', justify='left')
        self.title.pack(fill='x', padx=int(8 * scale), pady=(int(4 * scale), 0))
        self.body = tk.Label(self.frame, text='', bg=HUD_BG, fg=HUD_TEXT,
                             font=('Microsoft YaHei UI', max(8, int(8 * scale))),
                             anchor='w', justify='left')
        self.body.pack(fill='x', padx=int(8 * scale),
                       pady=(int(1 * scale), int(4 * scale)))
        self.masks = round_card_corners(
            self.frame, max(4, int(RADIUS * scale)),
            HUD_BG, HUD_EDGE, KEY)
        _no_activate(self.win)

    def show(self, title, lines=(), seconds=1.8):
        self.token += 1
        token = self.token
        self.title.config(text=title)
        self.body.config(text='\n'.join(line for line in lines if line))
        self.win.update_idletasks()
        width = max(self.win.winfo_reqwidth(), int(118 * _ui_scale()))
        height = self.win.winfo_reqheight()
        x, y = _cursor()
        left, top, right, bottom = _work_area(x, y)
        gx = min(max(left + 4, x + int(14 * _ui_scale())), right - width - 4)
        gy = min(max(top + 4, y + int(16 * _ui_scale())), bottom - height - 4)
        self.win.geometry('%dx%d+%d+%d' % (width, height, gx, gy))
        self.win.deiconify()
        self.win.lift()
        self.root.after(int(seconds * 1000), lambda: self.hide(token))

    def hide(self, token=None):
        if token is not None and token != self.token:
            return
        self.token += 1
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def close(self):
        self.token += 1
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class RegionFrame:
    """Rounded light-blue outline around the pad's writing region."""

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.wm_attributes('-topmost', True)
        self.win.configure(bg=KEY)
        self.win.wm_attributes('-transparentcolor', KEY)
        self.canvas = tk.Canvas(self.win, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)
        self.status = ()
        self._shown = False
        self._geom = None
        _no_activate(self.win)

    def show(self, rect, status=None, extra=()):
        """Show the writing region; ``extra`` boxes are outlined behind it.

        Each pen-down re-anchors the region, so a short session can draw a
        compound frame: the current box filled, the regions it replaced
        outlined in the same window (the window covers their union).
        """
        boxes = [tuple(int(v) for v in r)
                 for r in (rect,) + tuple(extra or ())]
        boxes = [b for b in boxes if b[2] >= 4 and b[3] >= 4]
        if not boxes:
            return
        left = min(b[0] for b in boxes)
        top = min(b[1] for b in boxes)
        right = max(b[0] + b[2] for b in boxes)
        bottom = max(b[1] + b[3] for b in boxes)
        width, height = right - left, bottom - top
        geom = (width, height, left, top)
        if geom != self._geom:
            self._geom = geom
            self.win.geometry('%dx%d+%d+%d' % geom)
            self.canvas.config(width=width, height=height)
        self.canvas.delete('all')
        radius = max(4, int(RADIUS * _ui_scale()))
        for index, (bl, bt, bw, bh) in enumerate(boxes):
            x, y = bl - left, bt - top
            box = (x + 2, y + 2, x + bw - 2, y + bh - 2)
            if index == 0:
                round_rect(self.canvas, box, radius, fill=FRAME_FILL,
                           stipple='gray12', tags='box')
            round_rect(self.canvas, box, radius, outline=FRAME_EDGE,
                       width=4, tags='box')
        self.set_status(status)
        if not self._shown:
            # Redrawing an already visible frame needs no z-order work; only
            # the first paint after hide() has to map and raise the window.
            self._shown = True
            self.win.deiconify()
            self.win.lift()

    def set_status(self, status):
        """Mode/state lines drawn inside the frame, instead of a separate card.

        The chip sits in the lower-left corner: the pointer anchors near the
        upper left of the region and ink grows right/down from it, so the
        status never covers the start of the stroke.
        """
        self.status = tuple(status or ())
        self.canvas.delete('status')
        if not self.status:
            return
        scale = _ui_scale()
        height = int(self.canvas.winfo_reqheight())
        text = self.canvas.create_text(
            int(15 * scale), height - int(13 * scale),
            anchor='sw', justify='left',
            text='\n'.join(self.status), fill=HUD_TEXT,
            font=('Microsoft YaHei UI', max(8, int(9 * scale))),
            tags='status')
        left, top, right, bottom = self.canvas.bbox(text)
        pad_x, pad_y = int(7 * scale), int(4 * scale)
        chip = round_rect(
            self.canvas,
            (left - pad_x, top - pad_y, right + pad_x, bottom + pad_y),
            max(3, int(RADIUS * 0.75 * scale)),
            fill=HUD_BG, outline=HUD_EDGE, width=1, tags='status')
        for item in chip:
            self.canvas.tag_lower(item, text)

    def hide(self):
        self._shown = False
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
