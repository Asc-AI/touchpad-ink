"""PPT / Note 墨迹：hold Alt, then write with the app's own pen as left drags.

Unlike 全域画图 this view draws nothing itself. While capturing, the touchpad
is mapped onto the client area of the foreground window (the slide show or the
OneNote page) and every stroke becomes a synthetic left-button drag, so the
ink belongs to PowerPoint / WPS / OneNote and survives our process.

* Alt up: the pad moves the mouse normally.
* Alt press: the writing region is a Mac-like pad-aspect rectangle centred on
  the current pointer (scale from TOUCHPAD_INK_REGION, 1 = whole slide). The
  pointer is put back where it was when the annotation ends, so pressing Alt
  again at the same spot always writes the same place (no drift).
* Double-tap Alt: continuous ink. Alt is no longer needed to write; holding
  Alt lifts mouse suppression so the pointer can be moved, and releasing it
  builds a new region under the pointer. Double-tap Alt again to leave, or
  right-click: the right button reaches the app (context menu) and ends the
  continuous session.
* While holding Alt, a two-finger pinch (or the mouse wheel) zooms the region;
  a light-blue frame shows the current region until the fingers lift. The pad
  reports a resting palm as a normal tip, so only a real pinch counts: both
  fingers move, start close, and change their separation without sweeping
  across the pad. A still second contact never zooms and never steals the ink.
  In continuous move mode a real pinch still wins: it holds the pointer for the
  zoom, and the pointer move resumes when the fingers lift. The slide never
  zooms, because Ctrl+wheel is swallowed even while suppression is lifted.
  Every raw frame updates the scale exactly, but the translucent frame is
  repainted at most every ``ZOOM_UI_SECONDS`` so a 125 Hz pinch stays smooth;
  a wheel notch still repaints immediately.

Injected points are lightly smoothed (EMA) and debounced with a minimum screen
step; recordings keep the raw pad coordinates, and the last point of every
stroke is sent unsmoothed so stroke ends stay sharp.

Mapping is hybrid and mouse-like. The first pen-down after Alt starts at the
pointer that built the region. Each release records the exact stroke end as
``pen_anchor`` and the pad point where the finger lifted; the next pen-down
starts at that tip plus the finger's lift-to-plant travel times the region
scale. Strokes therefore neither chain onto the previous end (lifting straight
up) nor jump to the new contact's absolute pad position (moving the hand).
Within a stroke the pen follows contact displacement at the scale captured at
pen-down, so zooming or rebuilding mid-stroke never teleports it.

The lift-to-plant move is filtered like a mouse being re-planted: a long
leftward move with little vertical travel is the hand returning to its
resting spot, not the pen, so the ink tip stays where it was; any one lift
can only nudge the pen by ``LIFT_MAX_TRAVEL``. The region is built from the
pointer that Alt showed, not from a cursor the pad may have pushed aside
while the hold gate was still opening (``preview_anchor``).

The region itself stays put while the ink still has room to the right/down;
it shifts only when the next start would leave the box or run out of room,
and the old box is kept as an outline (a "compound" frame). Near the slide's
right/bottom edge the new box is clamped and leaves less room. Note keeps its
whole-page absolute mapping (``reanchor_strokes = False``).

The user picks the pen in the presentation first; this mode never sends pen
shortcuts or opens menus on its own. Note ink only starts while OneNote is in
the foreground (auto mode already picks it that way; the check is a guard for
the manual menu entry). The physical pad/mouse is still suppressed during
capture, while our own injected events are allowed through LiveInput's hook.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import math
import os
import queue
import sys
import time
import tkinter as tk
import traceback

import auto_mode
from gesture import START_GATE, DoublePress, TapDetector
from hud import RegionFrame, StatusHud
from ime_window import (INPUT, api, apply_overlay_style,
                        enable_dpi_awareness)
from live_input import LiveInput, StrokeTracker, U
from live_window import Session
from draw_window import monitor_rect
import pad_info

PAD_MAX_X, PAD_MAX_Y = pad_info.DEFAULT_MAX_X, pad_info.DEFAULT_MAX_Y
REGION_SCALE = 3.0      # PPT: pad covers a third of the slide
REGION_ANCHOR = (0.28, 0.30)  # pointer sits upper-left *inside* the region
REGION_MIN_SCALE = 1.0
REGION_MAX_SCALE = 6.0
START_ROOM_X = 0.25     # keep the region until only this much is left right
START_ROOM_Y = 0.22     # ... or below; then shift it forward (compound box)
START_INSET_X = 0.04    # a start left/above the box also shifts it
START_INSET_Y = 0.05
LIFT_RESET_X = PAD_MAX_X * 0.22   # ~293 pad px: a big leftward lift = wrist reset
LIFT_MAX_TRAVEL = PAD_MAX_X * 0.35  # ~466 pad px: cap one lift-to-plant move
SMOOTH_ALPHA = 0.55     # EMA per report: light smoothing, ~one frame lag
MIN_STEP = 2.0          # screen pixels; smaller jitter is not injected
ZOOM_STEP = 1.12        # one wheel notch / pinch ratio applied smoothly
ZOOM_UI_SECONDS = 0.03  # ~33 fps for the pale frame while pinching
ZOOM_FRAME_SECONDS = 1.0
START_FRAME_SECONDS = 0.8  # show the new region briefly on each Alt press
PINCH_MIN_MOVE = 3.0    # pad px between two contacts before a scale update
PINCH_TRAVEL = 5.0      # pad px each finger must move to count as a pinch
PINCH_ARM = 6.0         # pad px the finger separation must change to start
PINCH_MAX_SPAN = 300.0  # pad px; a pinch starts with the fingers close
PINCH_MID_RATIO = 0.30  # the midpoint may drift at most this share
PINCH_MID_SLACK = 4.0   # pad px of midpoint drift allowed on top of that
MOTION_WINDOW = 0.12    # s of history used to tell a finger from a palm
PRIMARY_TRAVEL = 3.0    # pad px that mark the contact that is writing
# Palms land on the outer border; use the same share of the pad on every
# resolution (110 x 60 of the verified 1332 x 828 pad).
EDGE_RATIO_X = 110.0 / pad_info.DEFAULT_MAX_X
EDGE_RATIO_Y = 60.0 / pad_info.DEFAULT_MAX_Y
EDGE_START_X = PAD_MAX_X * EDGE_RATIO_X
EDGE_START_Y = PAD_MAX_Y * EDGE_RATIO_Y


def configure_pad():
    """Recompute pad-derived thresholds after pad_info discovery."""
    global PAD_MAX_X, PAD_MAX_Y, LIFT_RESET_X, LIFT_MAX_TRAVEL
    global EDGE_START_X, EDGE_START_Y
    PAD_MAX_X, PAD_MAX_Y = pad_info.max_x(), pad_info.max_y()
    LIFT_RESET_X = PAD_MAX_X * 0.22
    LIFT_MAX_TRAVEL = PAD_MAX_X * 0.35
    EDGE_START_X = PAD_MAX_X * EDGE_RATIO_X
    EDGE_START_Y = PAD_MAX_Y * EDGE_RATIO_Y

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

api('GetClientRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
api('ClientToScreen', W.BOOL, W.HWND, C.POINTER(W.POINT))
api('GetSystemMetrics', C.c_int, C.c_int)
api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))


def pad_to_screen(rect, x, y):
    """Touchpad coordinates -> screen pixels inside the target rectangle."""
    left, top, width, height = rect
    return (left + x * width / PAD_MAX_X,
            top + y * height / PAD_MAX_Y)


def env_region_scale(default=REGION_SCALE):
    """Mac-like precision from TOUCHPAD_INK_REGION; 1 = whole client."""
    try:
        value = float(os.environ.get('TOUCHPAD_INK_REGION', default))
    except (TypeError, ValueError):
        return default
    return max(REGION_MIN_SCALE, min(REGION_MAX_SCALE, value))


def region_rect(rect, cursor, scale=REGION_SCALE, anchor=(0.5, 0.5)):
    """Sub-rectangle of ``rect`` sized by ``scale`` and pivoted at ``anchor``.

    ``scale`` 3.0 means the pad spans a third of the slide width: strokes get
    about three times the pixel resolution and start next to the pointer
    instead of at the slide centre. ``anchor`` is the pointer's position
    inside the region (0.5, 0.5 = centred); PPT uses an upper-left pivot so
    the room for rightward writing is not wasted.
    """
    left, top, width, height = rect
    scale = max(REGION_MIN_SCALE, float(scale))
    if scale <= REGION_MIN_SCALE:
        return (left, top, width, height)
    rw = width / scale
    rh = rw * PAD_MAX_Y / PAD_MAX_X
    if rh > height:  # short client: limit by height, keep the pad aspect
        rh = height
        rw = rh * PAD_MAX_X / PAD_MAX_Y
    rw, rh = min(rw, width), min(rh, height)
    cx, cy = cursor or (left + width / 2, top + height / 2)
    if not (left <= cx <= left + width and top <= cy <= top + height):
        cx, cy = left + width / 2, top + height / 2
    ax = min(max(0.0, float(anchor[0])), 1.0)
    ay = min(max(0.0, float(anchor[1])), 1.0)
    rl = min(max(left, cx - rw * ax), left + width - rw)
    rt = min(max(top, cy - rh * ay), top + height - rh)
    return (int(round(rl)), int(round(rt)),
            int(round(rw)), int(round(rh)))


def absolute(px, py, vx, vy, vw, vh):
    """Screen pixels -> 0..65535 coordinates of the virtual desktop."""
    span_x, span_y = max(1, vw - 1), max(1, vh - 1)
    nx = int(round((px - vx) * 65535 / span_x))
    ny = int(round((py - vy) * 65535 / span_y))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def virtual_screen():
    return (U.GetSystemMetrics(SM_XVIRTUALSCREEN),
            U.GetSystemMetrics(SM_YVIRTUALSCREEN),
            U.GetSystemMetrics(SM_CXVIRTUALSCREEN) or U.GetSystemMetrics(0),
            U.GetSystemMetrics(SM_CYVIRTUALSCREEN) or U.GetSystemMetrics(1))


def client_rect(hwnd):
    """(left, top, width, height) of a window's client area, or None."""
    if not hwnd:
        return None
    rect = W.RECT()
    if not U.GetClientRect(hwnd, C.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    point = W.POINT(0, 0)
    if not U.ClientToScreen(hwnd, C.byref(point)):
        return None
    return (point.x, point.y, width, height)


def send_mouse(flags, dx=0, dy=0):
    """One MOUSEINPUT through SendInput; split out so tests can patch it."""
    event = INPUT()
    event.type = 0  # INPUT_MOUSE
    event.u.mi.dx = dx
    event.u.mi.dy = dy
    event.u.mi.dwFlags = flags
    return U.SendInput(1, C.byref(event), C.sizeof(INPUT))


class PptInk:
    kind = 'ppt'
    default_region_scale = REGION_SCALE
    region_from_env = True
    reanchor_strokes = True  # every pen-down starts at the ink pen itself

    def __init__(self, root, safe_preview=False, on_quit=None,
                 trigger_enabled=True, prefix='ppt', only_process=None,
                 label='ppt ink'):
        configure_pad()
        self.root = root
        self.safe_preview = safe_preview
        self.on_quit = on_quit
        self.events = queue.Queue()
        self.trigger_enabled = trigger_enabled
        self.auto_pending = False
        self.prefix = prefix
        self.only_process = only_process
        self.label = label

        self.ready = self.closing = False
        self.alt_held = self.alt_blocked = False
        self.alt_since = 0.0
        self.esc_down = False
        self.state = 'idle'
        self.expect = None
        self.target = None
        self.rect = (0, 0, 0, 0)
        self.region = (0, 0, 0, 0)
        self.region_target = None  # window used when the region was anchored
        self.region_used = None    # client rect used when anchored
        self.anchor = None         # pointer position that centres the region
        self.preview_anchor = None # pointer cached at Alt-down, before the gate
        self.pen_anchor = None     # screen point where the next stroke starts
        self.restore_pointer = None  # normal mode: put the pointer back here
        self.continuous = False    # 连写: capture without holding Alt
        self.moving = False        # continuous: Alt held, pointer is free
        self.zooming = False
        self.zoom_hold = False     # pinch took over while Alt was moving
        self.zoom_prev = 0.0
        self.pinch_pair = None      # ids of the two contacts in a pinch
        self.pinch_origin = None    # (midpoint, span) when the pinch armed
        self.zoom_mid = None        # midpoint of the last accepted pinch step
        self.zoom_show_at = 0.0     # last pale-frame redraw while pinching
        self.motion = {}            # cid -> window origin, palm vs finger
        self.contact_origin = {}    # cid -> first pad point, for the edge gate
        self.edge_ignored = set()   # cids already reported as edge downs
        self.edge_start = (EDGE_START_X, EDGE_START_Y)
        self.frame_deadline = 0.0
        self.taps = TapDetector()
        self.exit_press = DoublePress()  # 连写 exits on two Alt downs
        self.hud = None
        self.frame = None
        self.frame_shown = False
        self.frame_rects = []       # earlier regions, outlined while visible
        self.status_lines = []      # PPT draws them inside the region frame
        self.region_scale = (env_region_scale(self.default_region_scale)
                             if self.region_from_env
                             else self.default_region_scale)
        self.smooth_alpha = SMOOTH_ALPHA
        self.min_step = MIN_STEP
        self.smooth_xy = None
        self.pen_xy = None      # screen position accumulated during a stroke
        self.pen_pad = None     # smoothed pad point that pen_xy corresponds to
        self.pen_pad_end = None # pad point where the previous stroke lifted
        self.stroke_scale = (1.0, 1.0)  # pad -> screen, frozen at pen-down
        self.virtual = (0, 0, 0, 0)
        self.session = None
        self.last_path = ''
        self.tracker = StrokeTracker(grace=0.07)
        self.active_id = None
        self.pressed = False
        self.last_px = None
        self.last_point = None
        self.last_move_t = -1.0
        self.last_frame_wall = 0.0
        self.self_check_failed = False

        root.withdraw()
        self.hwnd = apply_overlay_style(root)
        root.report_callback_exception = self.on_error

        self.worker = LiveInput(self.events, self.hwnd,
                                allow_capture=not safe_preview,
                                fallback_hotkey=not safe_preview,
                                start_hotkey=False,
                                alt_releases=False, alt_stops=True,
                                alt_hold=True)
        self.worker.start()
        self.alt_held = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        self.alt_blocked = self.alt_held
        root.after(8, self.tick)

    # ---------- injection ----------
    def map(self, x, y):
        rect = self.region if self.region[2] and self.region[3] else self.rect
        return pad_to_screen(rect, x, y)

    def map_scale(self):
        """Screen pixels per pad pixel of the region in force right now."""
        rect = self.region if self.region[2] and self.region[3] else self.rect
        return (rect[2] / float(PAD_MAX_X), rect[3] / float(PAD_MAX_Y))

    def anchor_point(self):
        """The pointer that built the region, kept inside it.

        The region pivots at ``REGION_ANCHOR`` around this pointer, but a
        region clamped by the window edge no longer has the pointer at that
        exact fraction, so clamp the stored pointer instead of recomputing
        it from the pad coordinates.
        """
        rect = self.region if self.region[2] and self.region[3] else self.rect
        left, top, width, height = rect
        x, y = self.anchor
        return (min(max(x, left), left + max(0, width - 1)),
                min(max(y, top), top + max(0, height - 1)))

    def stroke_start(self, point):
        """Where this stroke begins, from the old pen tip and the lift move.

        Between strokes the finger is lifted and planted somewhere else on
        the pad. That travel is applied like a mouse's: lift straight up and
        the next stroke starts at the same point, move the hand to the next
        character and the pen is carried along by the same distance. The pad
        contact's absolute position never decides the spot.
        """
        x, y = self.pen_anchor
        if self.pen_pad_end is not None:
            dx = point[0] - self.pen_pad_end[0]
            dy = point[1] - self.pen_pad_end[1]
            # Writing towards the right, the hand eventually returns to its
            # resting spot on the left. That travel is the wrist moving, not
            # the pen: keep the ink tip where the last stroke left it.
            if dx < -LIFT_RESET_X and abs(dy) < abs(dx) * 0.5:
                return (x, y)
            # One lift may correct the stroke's place, but a huge forward
            # jump is still a reposition, not a pen movement.
            travel = math.hypot(dx, dy)
            if travel > LIFT_MAX_TRAVEL:
                scale = LIFT_MAX_TRAVEL / travel
                dx *= scale
                dy *= scale
            sx, sy = self.stroke_scale
            x += dx * sx
            y += dy * sy
        return (x, y)

    def clamp_point(self, point):
        """Keep an injected position inside the target's client area."""
        left, top, width, height = self.rect
        if width <= 0 or height <= 0:
            return point
        x = min(max(point[0], left), left + width - 1)
        y = min(max(point[1], top), top + height - 1)
        return (x, y)

    def start_fits(self, point):
        """True while the region still leaves room to write right and down."""
        left, top, width, height = self.region
        if width < 4 or height < 4:
            return False
        x, y = point
        return (left + width * START_INSET_X <= x
                <= left + width * (1.0 - START_ROOM_X)
                and top + height * START_INSET_Y <= y
                <= top + height * (1.0 - START_ROOM_Y))

    def reset_pen(self):
        """Forget the accumulated position; no stroke is running any more."""
        self.smooth_xy = None
        self.pen_xy = None
        self.pen_pad = None

    def smooth(self, point):
        """EMA on pad coordinates; each stroke restarts from its first point."""
        if self.smooth_xy is None:
            self.smooth_xy = point
            return point
        ax, ay = self.smooth_xy
        self.smooth_xy = (ax + (point[0] - ax) * self.smooth_alpha,
                          ay + (point[1] - ay) * self.smooth_alpha)
        return self.smooth_xy

    def press(self, point):
        """Start a stroke at the ink pen; keep the region steady.

        The pad only supplies deltas. A pen-down continues from the previous
        tip shifted by how far the finger moved while it was up (lift,
        reposition, plant), so the second stroke neither jumps to the pad
        contact's absolute spot nor chains onto the last stroke when the
        hand was moved to the next character. ``reanchor_strokes`` is off
        for Note, whose whole-page mapping is intentionally absolute.

        The region is rebuilt only when that start no longer leaves room to
        the right/down or falls outside the box, so the pale-blue frame no
        longer jumps on every stroke. When it does move, the old box stays
        outlined behind the new one (compound frame); near the slide's
        right/bottom edge the new box is clamped and leaves less room.
        """
        self.smooth_xy = point
        self.stroke_scale = self.map_scale()
        if self.reanchor_strokes and self.pen_anchor is not None:
            start = self.clamp_point(self.stroke_start(point))
            if not self.start_fits(start):
                previous = self.region
                self.anchor = start
                self.anchor_region()
                if self.frame_shown and previous != self.region:
                    self.frame_rects.append(previous)
                    del self.frame_rects[:-8]  # bound the overlaid boxes
                    self.show_frame()
                start = self.anchor_point()
            self.pen_xy = start
        else:
            self.pen_xy = self.map(*point)
        self.pen_pad = point
        px, py = int(round(self.pen_xy[0])), int(round(self.pen_xy[1]))
        self.move_to(px, py)
        self.last_px = (px, py)
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        self.pressed = True

    def move(self, point, exact=False):
        """Relative continuation: add the hand's delta, not a fresh map.

        The stroke starts where ``press`` mapped the touch, then follows the
        finger's displacement at the scale captured at pen-down. Zooming or
        rebuilding the region mid-stroke therefore no longer moves the pen,
        and the stroke keeps its pace until the finger lifts. ``exact``
        flushes the true stroke end unsmoothed.
        """
        if self.pen_xy is None:
            # Defensive: a move without its press still starts somewhere sane.
            self.pen_xy = self.map(*point)
            self.pen_pad = point
            self.stroke_scale = self.map_scale()
        pad = point if exact else self.smooth(point)
        if exact:
            self.smooth_xy = point
        dx = pad[0] - self.pen_pad[0]
        dy = pad[1] - self.pen_pad[1]
        if dx or dy:
            self.pen_pad = pad
            self.pen_xy = (self.pen_xy[0] + dx * self.stroke_scale[0],
                           self.pen_xy[1] + dy * self.stroke_scale[1])
        px, py = int(round(self.pen_xy[0])), int(round(self.pen_xy[1]))
        if self.last_px == (px, py):
            return  # same screen pixel: do not flood the host app
        if not exact and self.last_px is not None:
            sx, sy = px - self.last_px[0], py - self.last_px[1]
            if sx * sx + sy * sy < self.min_step * self.min_step:
                return  # debounce: sub-step jitter is simply not injected
        self.last_px = (px, py)
        self.move_to(px, py)

    def move_to(self, px, py):
        vx, vy, vw, vh = self.virtual
        dx, dy = absolute(px, py, vx, vy, vw, vh)
        send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                   | MOUSEEVENTF_VIRTUALDESK, dx, dy)

    def release_stroke(self):
        if self.pressed and self.last_point is not None:
            # Smoothing always lags; send the true end of the stroke as-is.
            self.move(self.last_point, exact=True)
        if self.pressed:
            send_mouse(MOUSEEVENTF_LEFTUP)
            self.pressed = False
            if self.reanchor_strokes and self.pen_xy is not None:
                # The next pen-down starts where this stroke left the pen,
                # shifted by however far the finger moves before it lands.
                self.pen_anchor = self.pen_xy
                self.pen_pad_end = self.last_point
        self.last_point = None
        self.reset_pen()

    def inject(self, frame):
        self.track_motion(frame)
        # 连写 move mode still lets a real two-finger pinch zoom the region:
        # the slide must never zoom, and the frame must stay controllable.
        if self.pinch(frame, self.tips(frame)):
            return  # region zoom: no ink this frame
        if self.moving:
            return  # pointer move mode: never ink
        segments = self.tracker.update(frame)
        if self.active_id is not None and self.active_id not in self.tracker.active:
            self.release_stroke()
            self.active_id = None
        segment = self.pick_segment(segments)
        if segment is None:
            return
        cid, previous, point = segment
        if self.active_id is None:
            self.active_id = cid
            self.press(point)
            self.last_move_t = frame['t'] - 0.02
        elif previous is None:
            self.release_stroke()
            self.press(point)
            self.last_move_t = frame['t'] - 0.02
        elif frame['t'] - self.last_move_t < 0.012:
            # ~83 Hz is enough for the host ink and keeps its queue short;
            # the freshest point goes out on the next eligible frame (or
            # in the exact flush at release if no frame follows).
            pass
        else:
            self.last_move_t = frame['t']
            self.move(point)
        self.last_point = point

    # ---------- palm vs writing finger ----------
    def track_motion(self, frame):
        """Remember where each tip was a moment ago.

        The pad reports a palm/wrist exactly like a tip (same flags), so a
        second contact is normal while writing. ``motion`` keeps a short
        window origin per contact: a palm barely moves, a finger does.
        ``contact_origin`` keeps the first point of each contact, which the
        edge gate uses to tell a real pen-down from a palm landing.
        """
        now = frame.get('t', 0.0)
        live = set()
        for contact in frame['contacts']:
            if not (contact['tip'] and contact['confidence']):
                continue
            if not (0 <= contact['x'] <= PAD_MAX_X
                    and 0 <= contact['y'] <= PAD_MAX_Y):
                continue
            cid = contact.get('id')
            live.add(cid)
            if cid not in self.contact_origin:
                self.contact_origin[cid] = (contact['x'], contact['y'])
            origin = self.motion.get(cid)
            if origin is None or now - origin[0] > MOTION_WINDOW:
                self.motion[cid] = (now, contact['x'], contact['y'])
        for cid in [cid for cid in self.motion if cid not in live]:
            del self.motion[cid]
        for cid in [cid for cid in self.contact_origin if cid not in live]:
            del self.contact_origin[cid]
        self.edge_ignored &= live

    def travel(self, cid, x, y):
        """Pad pixels this contact moved inside the current motion window."""
        origin = self.motion.get(cid)
        if origin is None:
            return 0.0
        return math.hypot(x - origin[1], y - origin[2])

    def start_allowed(self, cid, point):
        """Ink may only start away from the pad borders: palms land there.

        A wrist resting on the edge is reported exactly like a tip, and one
        that drags along the border sweeps out a long unwanted stroke. The
        decision uses the contact's first point, so a stroke that begins in
        the interior may still run into the margin.
        """
        mx, my = self.edge_start
        if mx <= 0 and my <= 0:
            return True
        x, y = self.contact_origin.get(cid, point)
        return (mx <= x <= PAD_MAX_X - mx and my <= y <= PAD_MAX_Y - my)

    def notice_edge_down(self, cid, point):
        """Report an ignored border pen-down once per contact."""
        if cid in self.edge_ignored:
            return
        self.edge_ignored.add(cid)
        x, y = self.contact_origin.get(cid, point)
        print('%s: edge pen-down ignored at (%d, %d)'
              % (self.label, x, y), flush=True)

    def pick_segment(self, segments):
        """Follow the contact that is writing instead of a resting palm.

        With two contacts the report order is arbitrary, so following the
        first one would ink the palm while the finger writes. Keep the active
        contact, then the one that actually moved; a lone still contact is
        only a leftover palm and never starts a new stroke. Contacts that
        first appeared on the pad border never start one either.
        """
        if self.active_id is not None:
            for segment in segments:
                if segment[0] == self.active_id:
                    return segment
        allowed = []
        for segment in segments:
            cid, _, point = segment
            if self.start_allowed(cid, point):
                allowed.append(segment)
            else:
                self.notice_edge_down(cid, point)
        segments = allowed
        if not segments:
            return None
        if len(segments) == 1:
            cid, previous, point = segments[0]
            if (previous is not None
                    and self.travel(cid, point[0], point[1]) < PRIMARY_TRAVEL):
                return None  # a still leftover contact is not a new stroke
            return segments[0]
        ranked = sorted(segments,
                        key=lambda s: self.travel(s[0], s[2][0], s[2][1]),
                        reverse=True)
        best, other = ranked[0], ranked[1]
        best_travel = self.travel(best[0], best[2][0], best[2][1])
        if (best_travel >= PRIMARY_TRAVEL
                and best_travel >= self.travel(other[0], other[2][0],
                                               other[2][1]) + 1.0):
            return best
        fresh = [segment for segment in segments if segment[1] is None]
        if len(fresh) == 1:
            return fresh[0]  # only one contact just landed: that is the pen
        return None  # ambiguous until one contact clearly moves

    def expire_if_stale(self, now=None):
        """Lift after a real finger lift: the pad may stop reporting entirely."""
        if not self.pressed:
            return False
        now = time.monotonic() if now is None else now
        if now - self.last_frame_wall <= 0.08:
            return False
        self.release_stroke()
        self.active_id = None
        self.tracker.active.clear()
        return True

    def clear_annotation(self):
        """Tray/Esc: nothing of ours is on screen, just lift the button."""
        self.release_stroke()
        self.active_id = None
        if self.state == 'capturing':
            self.expect = 'cancel'
            self.worker.request('release')

    # ---------- Alt hold / taps ----------
    def poll_alt(self, now=None):
        if not self.ready or self.safe_preview:
            return
        now = time.monotonic() if now is None else now
        down = bool(U.GetAsyncKeyState(0x12) & 0x8000)
        gesture = self.taps.update(down, now)
        exit_press = self.exit_press.update(down, now)
        if self.continuous:
            self.poll_continuous(gesture, down, exit_press)
            return
        if gesture == 'double' and self.trigger_enabled and self.state == 'idle':
            self.alt_held = False
            self.enter_continuous()
            return
        if self.state != 'idle':
            # LiveInput's keyboard hook ends capture on the Alt key-up; do not
            # cancel from here or a mid-capture release would fight that path.
            self.alt_held = down
            self.auto_pending = False
            return
        if not down:
            self.alt_held = False
            self.alt_blocked = False
            self.auto_pending = False
            return
        if not self.alt_held:
            self.alt_held = True
            self.alt_since = now
            self.alt_blocked = any(
                U.GetAsyncKeyState(key) & 0x8000
                for key in (0x11, 0x10, 0x5B, 0x5C, 0x09, 0x20, 0x73))
            if self.trigger_enabled and not self.alt_blocked:
                # Preview before the gate so the frame pops on press-down.
                self.preview_region()
                self.show_status('准备写字')
            return
        if self.auto_pending:
            # The host auto-selector chose this view for the current press.
            self.auto_pending = False
            if self.state == 'idle':
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
            return
        if now - self.alt_since < START_GATE:  # filters taps / double taps
            return
        self.alt_blocked = True
        self.start()

    def poll_continuous(self, gesture, down, exit_press=None):
        """连写: Alt no longer writes; it moves (PPT) or resets (typing)."""
        # In auto mode the host owns the double tap (it entered the mode);
        # manual mode has no host loop, so the view exits by itself.
        # Two Alt downs count even when each press is held while moving.
        if exit_press == 'double' and self.trigger_enabled:
            self.exit_continuous('已退出连写')
            return
        if gesture == 'double' and self.trigger_enabled:
            self.exit_continuous('已退出连写')
            return
        if gesture == 'tap':
            if self.moving:
                # A quick Alt tap after moving still rebuilds at the pointer.
                self.end_move()
            else:
                self.anchor_at_pointer()
            return
        if down and not self.moving and self.state == 'capturing':
            self.begin_move()
        elif not down and self.moving:
            self.end_move()

    # ---------- capture flow ----------
    def prepare_target(self, keep_anchor=False):
        """Resolve the foreground client and centre the region at the pointer.

        The pad moves the mouse normally until capture begins, so the pointer
        can drift between Alt-down (when the frame previews) and the hold
        gate. ``keep_anchor`` pins the region to the pointer that was shown,
        instead of re-reading a cursor the user's hand has pushed aside.
        """
        foreground = U.GetForegroundWindow()
        if not foreground or foreground == self.hwnd or U.IsIconic(foreground):
            return False
        # UWP apps (OneNote for Windows 10) report the ApplicationFrameHost
        # frame as foreground; use the hosted CoreWindow for process/rect.
        target = auto_mode.resolve_uwp(foreground)
        if (self.only_process
                and not auto_mode.process_matches(target,
                                                  self.only_process)):
            print('%s: foreground is not %s; not capturing'
                  % (self.label, self.only_process), flush=True)
            return False
        old_target, old_rect = self.target, self.rect
        rect = client_rect(target)
        cursor = W.POINT()
        U.GetCursorPos(C.byref(cursor))
        anchor = (cursor.x, cursor.y)
        if rect is None:
            rect = monitor_rect(anchor[0], anchor[1])
        rect = tuple(int(v) for v in rect)
        if (keep_anchor and self.anchor is not None
                and target == old_target and rect == old_rect):
            anchor = self.anchor
        self.target = target
        self.worker.target = target
        self.rect = rect
        self.anchor = anchor
        self.anchor_region()
        # The frame is built at the pointer: the first stroke starts there,
        # then each later pen-down continues from the last tip plus the
        # finger's lift-to-plant travel.
        self.pen_anchor = anchor
        self.pen_pad_end = None
        return True

    def anchor_region(self):
        self.region = region_rect(self.rect, self.anchor, self.region_scale,
                                  REGION_ANCHOR)
        self.region_target = self.target
        self.region_used = self.rect

    def anchor_at_pointer(self):
        """Re-centre the region at the current pointer (new annotation)."""
        self.release_stroke()
        self.active_id = None
        self.tracker.active.clear()
        self.reset_pen()
        point = W.POINT()
        U.GetCursorPos(C.byref(point))
        self.anchor = (point.x, point.y)
        self.anchor_region()
        self.pen_anchor = self.anchor
        self.pen_pad_end = None
        self.flash_region()

    def start(self):
        if not self.ready or self.safe_preview or self.state != 'idle':
            return
        # Hold the pointer the preview showed: until capture starts the pad
        # still moves the mouse, so re-reading it here would build the region
        # wherever the hand happened to push the cursor while Alt was held.
        if not self.prepare_target(keep_anchor=self.preview_anchor is not None):
            return
        self.preview_anchor = None
        # A press-down preview already showed this region; keep its 0.8 s
        # deadline instead of restarting the flash when the gate opens.
        keep_flash = self.frame_deadline > time.monotonic()
        self.restore_pointer = self.anchor
        self.continuous = False
        self.begin_capture(continuous=False, keep_flash=keep_flash)
        self.flash_region(restart=not keep_flash)

    def preview_region(self):
        """Flash the new region as soon as Alt goes down, before the gate.

        The hold gate still decides whether a stroke starts, but the frame
        no longer sits silent for the first 0.28 s: the user sees where the
        ink will land as soon as the key is pressed.
        """
        if (self.kind != 'ppt' or self.continuous
                or self.safe_preview or self.state != 'idle'):
            return False
        if not self.prepare_target():
            return False
        self.preview_anchor = self.anchor
        self.flash_region()
        return True

    def begin_capture(self, continuous, keep_flash=False):
        self.virtual = virtual_screen()
        self.tracker = StrokeTracker(grace=0.07)
        self.active_id = None
        self.pressed = False
        self.last_px = None
        self.last_point = None
        self.pen_pad_end = None
        self.preview_anchor = None
        self.reset_pen()
        self.last_move_t = -1.0
        self.last_frame_wall = 0.0
        self.moving = False
        self.zooming = False
        self.zoom_hold = False
        self.pinch_pair = None
        self.pinch_origin = None
        self.zoom_mid = None
        self.motion.clear()
        self.contact_origin.clear()
        self.edge_ignored.clear()
        if not keep_flash:
            self.frame_deadline = 0.0
            self.hide_frame()
        self.worker.continuous = continuous
        self.worker.free_hold = continuous
        self.worker.suppress = True
        self.state = 'starting'
        self.expect = 'start'
        self.worker.request('start')

    def enter_continuous(self):
        """Double Alt: keep writing without holding; pointer moves on Alt."""
        if not self.ready or self.safe_preview:
            return False
        if self.continuous and self.state in ('starting', 'capturing'):
            return True
        if self.state != 'idle':
            return False
        if not self.prepare_target():
            return False
        self.continuous = True
        self.restore_pointer = None
        self.taps.reset()
        self.exit_press.reset()
        self.begin_capture(continuous=True)
        self.flash_region()
        return True

    def exit_continuous(self, message='已退出连写'):
        was = self.continuous
        self.continuous = False
        self.moving = False
        self.zooming = False
        self.zoom_hold = False
        self.pinch_pair = None
        self.pinch_origin = None
        self.zoom_mid = None
        self.restore_pointer = None
        self.worker.continuous = False
        self.worker.free_hold = False
        self.worker.suppress = True
        self.hide_frame()
        self.taps.reset()
        self.exit_press.reset()
        if self.state in ('capturing', 'starting'):
            self.expect = 'cancel'
            self.worker.request('release')
        if was:
            self.show_status(message, seconds=1.4)

    def begin_move(self):
        self.moving = True
        self.zoom_hold = False
        self.release_stroke()
        self.active_id = None
        self.tracker.active.clear()
        self.reset_pen()
        self.last_point = None
        self.zooming = False
        self.pinch_pair = None
        self.pinch_origin = None
        self.zoom_mid = None
        self.hide_frame()
        self.worker.suppress = False  # physical pad moves the pointer again
        self.show_status('移动区域', ('松开 Alt 建新域',))

    def end_move(self):
        if not self.moving:
            return
        self.moving = False
        self.zoom_hold = False
        self.worker.suppress = True
        self.anchor_at_pointer()
        self.tracker = StrokeTracker(grace=0.07)
        self.show_status('连写中', ('已按指针重建',))

    # ---------- writing region / zoom ----------
    def set_scale(self, scale, live=False):
        scale = max(REGION_MIN_SCALE, min(REGION_MAX_SCALE, float(scale)))
        if abs(scale - self.region_scale) < 0.01:
            return False
        self.region_scale = scale
        if self.rect[2] and self.rect[3]:
            self.anchor_region()
            self.frame_rects = []  # zoom shows the one region, not old strokes
            if live:
                # Pinch frames arrive at ~125 Hz; redrawing the translucent
                # frame on every raw frame made the zoom stutter. Keep the
                # scale exact but paint the frame at a steady ~33 fps.
                now = time.monotonic()
                if now - self.zoom_show_at < ZOOM_UI_SECONDS:
                    return True
                self.zoom_show_at = now
            if self.zooming:
                # Keep the size in the visible chip in step with the pinch.
                self.status_lines = self.status_text('区域缩放')
            self.show_frame()
        return True

    def zoom_wheel(self, delta):
        """Wheel up enlarges the region (lower scale), down makes it finer."""
        if not delta or self.moving:
            return
        factor = 1.0 / ZOOM_STEP if delta > 0 else ZOOM_STEP
        self.set_scale(self.region_scale * factor)
        self.show_status('区域缩放')
        self.frame_deadline = time.monotonic() + ZOOM_FRAME_SECONDS
        print('%s: region scale %.2f -> %s'
              % (self.label, self.region_scale, self.region), flush=True)

    @staticmethod
    def tips(frame):
        return [c for c in frame['contacts']
                if c['tip'] and c['confidence']
                and 0 <= c['x'] <= PAD_MAX_X and 0 <= c['y'] <= PAD_MAX_Y]

    def end_zoom(self):
        self.zooming = False
        self.pinch_pair = None
        self.pinch_origin = None
        self.zoom_mid = None
        self.hide_frame()
        if self.zoom_hold:
            # 连写 move mode: give the pad back to the pointer after the zoom.
            self.zoom_hold = False
            if self.moving:
                self.worker.suppress = False
                self.show_status('移动区域', ('松开 Alt 建新域',))

    @staticmethod
    def span_mid(a, b):
        span = math.hypot(b['x'] - a['x'], b['y'] - a['y'])
        mid = ((a['x'] + b['x']) / 2.0, (a['y'] + b['y']) / 2.0)
        return span, mid

    def pinch(self, frame, tips):
        """Two contacts zoom, but only for a deliberate two-finger pinch.

        The pad also reports a resting palm/wrist as a tip, so "two contacts"
        alone used to start a zoom: as the writing finger moved, the distance
        to the palm changed and the region silently shrank, stacking later
        characters. A real pinch therefore needs both fingers to move, to
        start close together, and to change their separation while their
        midpoint stays roughly put. While Alt moves the pointer, a genuine
        pinch still takes over: suppression goes back on for the zoom and the
        pointer move resumes when the fingers lift, so the slide never zooms.

        Fingers apart enlarge the dashboards region (max precision), fingers
        together make it finer. The light-blue frame disappears as soon as
        fewer than two contacts are reported.
        """
        if len(tips) < 2:
            if self.zooming:
                self.end_zoom()
                self.tracker.active.clear()
                self.active_id = None
                self.reset_pen()
                return True
            self.pinch_pair = None
            self.pinch_origin = None
            return False
        a, b = tips[0], tips[1]
        pair = frozenset((a.get('id'), b.get('id')))
        if pair != self.pinch_pair:
            if self.zooming:
                self.end_zoom()
            self.pinch_pair = pair
            self.pinch_origin = None
        span, mid = self.span_mid(a, b)
        if not self.zooming:
            travelled = min(self.travel(a.get('id'), a['x'], a['y']),
                            self.travel(b.get('id'), b['x'], b['y']))
            if travelled < PINCH_TRAVEL or span > PINCH_MAX_SPAN:
                self.pinch_origin = None
                return False  # palm + finger: let the ink follow the finger
            if self.pinch_origin is None:
                self.pinch_origin = (mid, span)
                return False  # one frame to arm; ink may start meanwhile
            old_mid, old_span = self.pinch_origin
            change = abs(span - old_span)
            drift = math.hypot(mid[0] - old_mid[0], mid[1] - old_mid[1])
            if change < PINCH_ARM:
                return False
            if (drift > PINCH_MID_RATIO * change + PINCH_MID_SLACK
                    or travelled < 0.4 * change):
                self.pinch_origin = None
                return False  # the pair swept across the pad, not a pinch
            self.zooming = True
            self.zoom_prev = span
            self.zoom_mid = mid
            if self.moving:
                # 连写 move mode: the pad belongs to the pointer, but this
                # pinch is ours. Hold the pointer still until the fingers lift.
                self.zoom_hold = True
                self.worker.suppress = True
            self.release_stroke()
            self.active_id = None
            self.tracker.active.clear()
            self.reset_pen()
            self.show_status('区域缩放')
            # The start already painted the frame: hold the next live redraw
            # back one UI interval instead of repeating it on the next frame.
            self.zoom_show_at = time.monotonic()
            print('%s: pinch zoom at scale %.2f'
                  % (self.label, self.region_scale), flush=True)
            return True
        if span < 8:
            return True
        if (self.zoom_prev >= 8
                and abs(span - self.zoom_prev) >= PINCH_MIN_MOVE):
            drift = math.hypot(mid[0] - self.zoom_mid[0],
                               mid[1] - self.zoom_mid[1])
            change = abs(span - self.zoom_prev)
            if drift > PINCH_MID_RATIO * change + 2 * PINCH_MID_SLACK:
                # The pair is sweeping across the pad, not pinching: stop
                # before a whole-hand drift keeps warping the region.
                self.end_zoom()
                self.tracker.active.clear()
                self.active_id = None
                self.reset_pen()
                return True
            # Keep the scale exact for every raw frame; set_scale(live=True)
            # paints the pale frame at a steady rate so the zoom stays smooth.
            self.set_scale(self.region_scale * self.zoom_prev / span,
                           live=True)
            self.zoom_prev = span
            self.zoom_mid = mid
        return True

    def show_frame(self):
        """Every frame visible carries a status line, even a bare preview."""
        try:
            if self.frame is None:
                self.frame = RegionFrame(self.root)
            status = self.status_lines
            if not status:
                state, extra = self.frame_state()
                status = self.status_text(state, extra)
            self.frame.show(self.region, status, self.frame_rects)
            self.frame_shown = True
        except BaseException:
            traceback.print_exc()

    def hide_frame(self):
        self.status_lines = []
        self.frame_rects = []
        self.frame_shown = False
        if self.frame is not None:
            try:
                self.frame.hide()
            except BaseException:
                pass

    def flash_region(self, restart=True):
        """Show the freshly chosen region for a moment so Alt visibly re-ranges.

        The full-page Note mapping would cover the whole page, so only the
        local PPT region flashes. Zoom keeps its own longer deadline.
        ``restart=False`` keeps a flash that the press-down preview started,
        so showing the region early does not stretch it past 0.8 s.
        """
        if self.kind != 'ppt' or not self.rect[2] or not self.rect[3]:
            return
        now = time.monotonic()
        self.frame_rects = []  # a fresh build starts a fresh compound frame
        self.show_frame()
        if restart or now >= self.frame_deadline:
            self.frame_deadline = now + START_FRAME_SECONDS

    def status_text(self, state, extra=()):
        """Lines that describe the region and the current state."""
        title = 'Note 墨迹' if self.kind == 'note' else 'PPT 墨迹'
        region = ('区域 %d×%d · 精度 %.1f×'
                  % (self.region[2], self.region[3], self.region_scale))
        lines = ['%s · %s' % (title, state), region]
        if self.continuous:
            lines.append('直接写 · 按住 Alt 移动 · 双按退出')
        else:
            lines.append('双按 Alt 连写 · 捏合/滚轮缩放')
        lines.extend(extra)
        return lines

    def frame_state(self):
        """State a frame shows when it appeared before show_status() ran."""
        if self.moving:
            return '移动区域', ('松开 Alt 建新域',)
        if self.zooming:
            return '区域缩放', ()
        if self.state == 'capturing':
            return ('连写中' if self.continuous else '短时批注'), ()
        return '准备写字', ()

    def show_status(self, state, extra=(), seconds=1.8):
        """Report the mode/state.

        PPT has a frame around the writing region already, so its status is
        drawn inside that frame instead of a second card at the pointer. Note
        maps the whole page and has no frame, so it keeps the small card.
        """
        title = 'Note 墨迹' if self.kind == 'note' else 'PPT 墨迹'
        lines = self.status_text(state, extra)
        if self.kind == 'ppt':
            self.status_lines = lines
            now = time.monotonic()
            if self.frame_deadline < now:
                self.frame_deadline = now + seconds
            self.show_frame()
            return
        try:
            if self.hud is None:
                self.hud = StatusHud(self.root)
            self.hud.show(title, ['状态：%s' % state] + lines[1:], seconds)
        except BaseException:
            traceback.print_exc()

    def event(self, kind, data):
        if kind == 'ready':
            self.ready = True
        elif kind == 'quit':
            if self.on_quit:
                self.on_quit()
            else:
                self.close()
        elif kind == 'started':
            if self.state != 'starting':
                self.worker.request('release')
                return
            self.session = Session(data['path'], prefix=self.prefix)
            self.last_path = str(self.session.base.with_suffix('.jsonl'))
            self.state = 'capturing'
            self.expect = 'cancel'
            self.continuous = self.worker.continuous
            if self.target and U.IsWindow(self.target):
                # Alt key-down can leave OneNote/Office in menu-keytip mode,
                # where the first click only dismisses the tips.
                try:
                    U.PostMessageW(self.target, 0x001F, 0, 0)  # WM_CANCELMODE
                except BaseException:
                    pass
            if not self.continuous and not (U.GetAsyncKeyState(0x12) & 0x8000):
                self.worker.request('release')
                return
            if self.continuous:
                self.show_status('连写中')
            else:
                self.show_status('短时批注')
        elif kind == 'frame':
            self.last_frame_wall = time.monotonic()
            if self.state == 'capturing':
                self.inject(data)  # keep ink latency independent of disk I/O
            if self.session and not self.session.closed:
                self.session.write(data)
        elif kind == 'wheel':
            if self.state == 'capturing':
                self.zoom_wheel(data)
        elif kind == 'exit_continuous':
            # A physical right click belongs to the app; it also leaves 连写.
            # Ignore a stale notice if 连写 already ended.
            if self.continuous:
                self.exit_continuous(data or '右键退出连写')
        elif kind == 'stopped':
            self.release_stroke()
            self.active_id = None
            self.moving = False
            self.zooming = False
            self.zoom_hold = False
            self.pinch_pair = None
            self.pinch_origin = None
            self.zoom_mid = None
            self.motion.clear()
            self.contact_origin.clear()
            self.edge_ignored.clear()
            self.hide_frame()
            self.worker.suppress = True
            if self.continuous:
                # Safety/timeout release or exit: keep the view, drop 连写.
                self.continuous = False
                self.worker.continuous = False
                self.worker.free_hold = False
                self.taps.reset()
                self.exit_press.reset()
            if self.restore_pointer is not None:
                self.move_to(*self.restore_pointer)
                self.restore_pointer = None
            if self.target and U.IsWindow(self.target):
                try:
                    U.PostMessageW(self.target, 0x001F, 0, 0)  # WM_CANCELMODE
                except BaseException:
                    pass
            if self.session:
                self.session.close(data)
                self.session = None
            self.state = 'idle'
            self.expect = None
            self.target = None
            self.worker.target = None
        elif kind in ('notice', 'error'):
            print('ppt ink: %s' % data, flush=True)
            if kind == 'error':
                self.ready = False
            self.release_stroke()
            self.active_id = None
            self.hide_frame()
            if self.state == 'starting':
                self.state = 'idle'
                self.continuous = False
                self.restore_pointer = None
                self.worker.continuous = False
                self.worker.free_hold = False
            elif kind == 'error':
                self.clear_annotation()

    # ---------- loop ----------
    def cancel(self):
        self.release_stroke()
        self.active_id = None
        if self.state == 'capturing':
            self.expect = 'cancel'
            self.worker.request('release')

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
            if (self.frame_deadline and not self.zooming
                    and time.monotonic() > self.frame_deadline):
                self.frame_deadline = 0.0
                self.hide_frame()
            if self.state == 'capturing':
                self.expire_if_stale()
                down = bool(U.GetAsyncKeyState(0x1B) & 0x8000)
                if down and not self.esc_down:
                    self.cancel()
                self.esc_down = down
        except BaseException:
            self.on_error(*sys.exc_info())
        self.root.after(8, self.tick)

    def on_error(self, exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        self.release_stroke()
        if not self.closing:
            self.cancel()

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.release_stroke()
        if self.worker:
            self.worker.stopping.set()
            self.worker.join(timeout=3)
        try:
            if self.hud is not None:
                self.hud.close()
            if self.frame is not None:
                self.frame.close()
        except BaseException:
            pass
        try:
            if self.session:
                self.session.close(self.label + ' 关闭')
        except BaseException:
            traceback.print_exc()
        finally:
            self.root.destroy()


class NoteInk(PptInk):
    """Note 墨迹: the same left-drag ink, but only while OneNote is foreground.

    OneNote keeps the whole client area (region scale 1.0) because a page is
    written across; the smoothing/debounce of the shared injection still
    applies. The ink belongs to OneNote's own pen, so it survives this process
    just like PPT ink.
    """
    kind = 'note'
    default_region_scale = 1.0
    region_from_env = False
    reanchor_strokes = False  # a Note page is written across from any pad spot

    def __init__(self, root, safe_preview=False, on_quit=None,
                 trigger_enabled=True):
        super().__init__(root, safe_preview=safe_preview, on_quit=on_quit,
                         trigger_enabled=trigger_enabled, prefix='note',
                         only_process=auto_mode.ONENOTE_PROCESS,
                         label='note ink')
        # A Note page is mapped whole, so writing up to the page edge is
        # normal; the PPT border gate would throw those starts away.
        self.edge_start = (0.0, 0.0)


def self_check():
    """Safe check: construction, mapping and no capture in preview mode."""
    root = tk.Tk()
    app = PptInk(root, safe_preview=True)
    result = {'ok': False}

    def verify():
        try:
            rect = (100, 50, 1600, 900)
            app.rect = rect
            app.region = rect
            ok = []
            ok.append(app.map(0, 0) == (100.0, 50.0))
            ok.append(app.map(PAD_MAX_X, PAD_MAX_Y) == (1700.0, 950.0))
            ok.append(absolute(0, 0, 0, 0, 2560, 1600) == (0, 0))
            ok.append(absolute(2559, 1599, 0, 0, 2560, 1600) == (65535, 65535))
            ok.append(client_rect(None) is None)
            region = region_rect(rect, (900, 500),
                                 REGION_SCALE, REGION_ANCHOR)
            ok.append(abs(region[2] - 533) <= 1)
            ok.append(abs(region[3] - 332) <= 1)
            ok.append(region[0] <= 900 < region[0] + region[2])
            ok.append(region[0] + region[2] - 900 > 900 - region[0])
            app.region = region
            cx, cy = app.map(PAD_MAX_X * REGION_ANCHOR[0],
                             PAD_MAX_Y * REGION_ANCHOR[1])
            ok.append(abs(cx - 900) <= 2 and abs(cy - 500) <= 2)
            ok.append(region_rect(rect, (900, 500), 1.0) == rect)
            ok.append(app.ready)
            ok.append(not app.pressed)
            note = NoteInk(tk.Toplevel(root), safe_preview=True)
            try:
                ok.append(note.kind == 'note')
                ok.append(note.prefix == 'note')
                ok.append(note.only_process == auto_mode.ONENOTE_PROCESS)
                ok.append(note.region_scale == 1.0)
                ok.append(not note.pressed)
            finally:
                note.close()
            result['ok'] = all(ok)
            print('ppt ink self-check: ready=%s region=%s note=%s ok=%s'
                  % (app.ready, region, note.kind, result['ok']))
        except BaseException:
            traceback.print_exc()
            result['ok'] = False
        finally:
            app.close()

    root.after(600, verify)
    try:
        root.mainloop()
    finally:
        if not app.closing:
            app.close()
    return 0 if result['ok'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='verify mapping only; never injects mouse input')
    args = parser.parse_args()
    enable_dpi_awareness()
    pad_info.configure()
    if args.self_check:
        return self_check()
    root = tk.Tk()
    app = PptInk(root)
    try:
        root.mainloop()
    finally:
        if not app.closing:
            app.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
