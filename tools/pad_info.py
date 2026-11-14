"""The pad geometry shared by every view, resolved once at startup.

``ptp_layout`` discovers the hardware; this module only caches the result so
the overlay, full-screen layer and ink mapping all use the same coordinate
range.  Before discovery (tests, self-checks) the verified Synaptics ranges
remain the defaults, so unit tests stay hardware-independent.
"""
from __future__ import annotations

import os
import sys

import ptp_layout

DEFAULT_MAX_X = 1332
DEFAULT_MAX_Y = 828
# Palm rejection used the outer 110 x 60 pad pixels on the 1332 x 828 pad;
# keep the same relative border on other resolutions.
EDGE_RATIO_X = 110.0 / DEFAULT_MAX_X
EDGE_RATIO_Y = 60.0 / DEFAULT_MAX_Y


class _DefaultPad:
    vendor_id = 0
    product_id = 0
    product = 'default'
    max_x = DEFAULT_MAX_X
    max_y = DEFAULT_MAX_Y
    max_contacts = 5
    layout = None

    def describe(self):
        return 'default 1332x828'


_default = _DefaultPad()
_device = None
_last_error = ''


def configure(preferred='', force=False):
    """Discover and cache one precision touchpad; ``None`` when unavailable.

    ``TOUCHPAD_INK_DEVICE`` (a ``vid:pid`` or HID path fragment) picks a
    specific collection when a laptop exposes more than one touchpad.
    """
    global _device, _last_error
    if not preferred:
        preferred = os.environ.get('TOUCHPAD_INK_DEVICE', '').strip()
    if _device is not None and not force:
        wanted = preferred.casefold()
        tag = '%04x:%04x' % (getattr(_device, 'vendor_id', 0),
                             getattr(_device, 'product_id', 0))
        path = getattr(_device, 'path', '')
        if (not wanted or wanted == tag
                or (path and wanted in path.casefold())):
            return _device
    device, candidates, errors = ptp_layout.discover(preferred)
    _last_error = '; '.join(errors) if errors else ''
    if device is not None:
        _device = device
    return _device


def device():
    return _device


def current():
    return _device if _device is not None else _default


def max_x():
    return current().max_x


def max_y():
    return current().max_y


def max_contacts():
    return current().max_contacts


def edge_x():
    return max_x() * EDGE_RATIO_X


def edge_y():
    return max_y() * EDGE_RATIO_Y


def failure():
    text = '未找到可用的 Windows 精密触控板（0x0D/0x05）'
    if _last_error:
        text += '：' + _last_error
    return text


def reset():
    """Forget the discovered pad; tests only."""
    global _device, _last_error
    _device = None
    _last_error = ''


def main():
    """Print which precision touchpad the app will use (diagnostic only)."""
    chosen = configure(force=True)
    if chosen is None:
        print(failure())
        return 1
    layout = chosen.layout
    print(chosen.describe())
    print('report_ids=%s input_bytes=%d x=%d..%d y=%d..%d'
          % (layout.report_ids, layout.input_length,
             layout.x_min, layout.x_max, layout.y_min, layout.y_max))
    if os.environ.get('TOUCHPAD_INK_DEVICE', '').strip():
        print('override: %s' % os.environ['TOUCHPAD_INK_DEVICE'].strip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
