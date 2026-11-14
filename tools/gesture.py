"""Alt tap / double-tap detection shared by the views and the tray host.

A tap is a press released within TAP_MAX seconds; two taps whose releases are
at most DOUBLE_WINDOW apart make a double tap. A longer press resets the
sequence, so holding Alt to write never counts as the first half of a double
tap. All times are ``time.monotonic()`` values.
"""

TAP_MAX = 0.22
DOUBLE_WINDOW = 0.45
START_GATE = 0.28  # longer than TAP_MAX: a tap never starts a capture


class TapDetector:
    def __init__(self, tap_max=TAP_MAX, window=DOUBLE_WINDOW):
        self.tap_max = tap_max
        self.window = window
        self.down = False
        self.since = 0.0
        self.last_tap = 0.0

    def reset(self):
        self.down = False
        self.since = 0.0
        self.last_tap = 0.0

    def update(self, down, now):
        """Return 'tap' / 'double' when a press completes, else None."""
        down = bool(down)
        if down and not self.down:
            self.down = True
            self.since = now
            return None
        if down or not self.down:
            return None
        self.down = False
        if now - self.since > self.tap_max:
            self.last_tap = 0.0  # a real hold: not part of a double tap
            return None
        if self.last_tap and now - self.last_tap <= self.window:
            self.last_tap = 0.0
            return 'double'
        self.last_tap = now
        return 'tap'


class DoublePress:
    """Two Alt presses within ``window``, regardless of hold length.

    连写 mode uses this to leave the mode: the user often rests on Alt while
    repositioning, so requiring short tap releases (TapDetector) makes the
    exit feel broken. Reports 'double' on the second press-down; a release
    alone never reports anything.
    """
    def __init__(self, window=DOUBLE_WINDOW):
        self.window = window
        self.down = False
        self.last = 0.0

    def reset(self):
        self.down = False
        self.last = 0.0

    def update(self, down, now):
        down = bool(down)
        if down and not self.down:
            self.down = True
            if self.last and now - self.last <= self.window:
                self.last = 0.0
                return 'double'
            self.last = now
            return None
        if not down:
            self.down = False
        return None
