"""Resident notification-area icon for the touchpad handwriting modes.

Pure ctypes Shell_NotifyIcon: no pystray/Pillow runtime dependency. A dedicated
thread owns the hidden window, the icon and the popup menu, so Tk's event loop
is never blocked by TrackPopupMenu. Menu choices are delivered through a
queue to the Tk host; mode changes come back as a posted message.

The icon is generated once as data/tray.ico (a 32 px original mark: a rounded
touchpad outline with a handwriting stroke), so the repository does not need a
binary asset and the app has no extra packages.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import uuid

import app_paths
import raw_probe as R

U = R.user32
kernel32 = C.WinDLL('kernel32', use_last_error=True)
shell32 = C.WinDLL('shell32', use_last_error=True)

WNDPROC = R.WNDPROC
WNDCLASSEXW = R.WNDCLASSEXW
MSG = R.MSG

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

# Private window messages: tray callback, mode refresh.
WM_TRAY = 0x8000 + 20
WM_MODE = 0x8000 + 21

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1

MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49

MODE_INDEX = {'auto': 0, 'typing': 1, 'draw': 2, 'ppt': 3, 'note': 4}
MODE_ORDER = ('auto', 'typing', 'draw', 'ppt', 'note')
MODE_LABELS = {'auto': '自动识别', 'typing': '局部打字', 'draw': '全域画图',
               'ppt': 'PPT 墨迹', 'note': 'Note 墨迹'}

MENU_TYPING = 1
MENU_DRAW = 2
MENU_CLEAR = 3
MENU_QUIT = 4
MENU_PPT = 5
MENU_NOTE = 6
MENU_AUTO = 7


class GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD),
                ('Data4', C.c_ubyte * 8)]


class NOTIFYICONDATAW(C.Structure):
    _fields_ = [
        ('cbSize', W.DWORD), ('hWnd', W.HWND), ('uID', W.UINT),
        ('uFlags', W.UINT), ('uCallbackMessage', W.UINT), ('hIcon', W.HICON),
        ('szTip', W.WCHAR * 128), ('dwState', W.DWORD),
        ('dwStateMask', W.DWORD), ('szInfo', W.WCHAR * 256),
        ('uTimeoutOrVersion', W.UINT), ('szInfoTitle', W.WCHAR * 64),
        ('dwInfoFlags', W.DWORD), ('guidItem', GUID),
        ('hBalloonIcon', W.HICON)]


class NOTIFYICONIDENTIFIER(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('hWnd', W.HWND), ('uID', W.UINT),
                ('guidItem', GUID)]


def api(name, restype, *args):
    fn = getattr(U, name)
    fn.restype, fn.argtypes = restype, list(args)
    return fn


api('DefWindowProcW', R.LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
api('DestroyWindow', W.BOOL, W.HWND)
api('PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
api('PostQuitMessage', None, C.c_int)
api('GetMessageW', W.BOOL, C.POINTER(MSG), W.HWND, W.UINT, W.UINT)
api('GetSystemMetrics', C.c_int, C.c_int)
api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))
api('SetForegroundWindow', W.BOOL, W.HWND)
api('LoadImageW', W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT,
    C.c_int, C.c_int, W.UINT)
api('RegisterWindowMessageW', W.UINT, W.LPCWSTR)
api('CreatePopupMenu', W.HMENU)
api('DestroyMenu', W.BOOL, W.HMENU)
api('AppendMenuW', W.BOOL, W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR)
api('SetMenuDefaultItem', W.BOOL, W.HMENU, W.UINT, W.UINT)
api('TrackPopupMenu', W.UINT, W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int,
    W.HWND, C.c_void_p)

shell32.Shell_NotifyIconW.argtypes = [W.DWORD, C.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = W.BOOL
shell32.Shell_NotifyIconGetRect.argtypes = [
    C.POINTER(NOTIFYICONIDENTIFIER), C.POINTER(W.RECT)]
shell32.Shell_NotifyIconGetRect.restype = C.c_long

_icon_dll = C.WinDLL('user32', use_last_error=True)
_icon_dll.LoadIconW.argtypes = [W.HINSTANCE, C.c_void_p]
_icon_dll.LoadIconW.restype = C.c_void_p


# ---------------------------------------------------------------- icon file
# Original mark: a teal tile, an off-white touchpad outline and a coral
# handwriting stroke. Deliberately not a pen nib or any vendor logo.
BG = (14, 92, 85)
EDGE = (242, 251, 248)
INK = (255, 158, 106)
PAD = (6.5, 9.0, 25.5, 23.0)     # left, top, right, bottom in a 32 unit grid
PAD_RADIUS = 3.2
PAD_STROKE = 1.6
ICON_POINTS = ((9.8, 18.6), (12.4, 14.2), (15.6, 19.6), (19.2, 13.8),
               (22.4, 18.2))
ICON_SEGMENTS = tuple((a[0], a[1], b[0], b[1])
                      for a, b in zip(ICON_POINTS, ICON_POINTS[1:]))
INK_STROKE = 2.0


def _distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if not length:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _rounded_rect(px, py, left, top, right, bottom, radius):
    """Signed distance to a rounded rectangle; positive means outside."""
    half_w = (right - left) / 2.0 - radius
    half_h = (bottom - top) / 2.0 - radius
    qx = abs(px - (left + right) / 2.0) - half_w
    qy = abs(py - (top + bottom) / 2.0) - half_h
    return ((max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
            + min(max(qx, qy), 0.0) - radius)


def render_icon(size=32, supersample=4):
    """RGBA touchpad-and-ink icon, supersampled for smooth tray edges."""
    n = size * supersample
    scale = size / 32.0
    radius = size * 0.22
    margin = size * 0.055
    half = size / 2 - margin
    pad_l, pad_t, pad_r, pad_b = (value * scale for value in PAD)
    pad_radius = PAD_RADIUS * scale
    pad_stroke = PAD_STROKE * scale / 2.0
    ink_stroke = INK_STROKE * scale / 2.0
    samples = []
    for y in range(n):
        row = []
        py = (y + 0.5) / supersample
        for x in range(n):
            px = (x + 0.5) / supersample
            # Rounded-box signed distance: positive means outside the tile.
            qx = abs(px - size / 2) - (half - radius)
            qy = abs(py - size / 2) - (half - radius)
            outside = ((max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
                       + min(max(qx, qy), 0.0) - radius)
            if outside > 0:
                row.append((0, 0, 0, 0))
                continue
            color = BG
            pad = _rounded_rect(px, py, pad_l, pad_t, pad_r, pad_b,
                                pad_radius)
            if abs(pad) <= pad_stroke:
                color = EDGE
            if min(_distance(px, py, ax * scale, ay * scale,
                             bx * scale, by * scale)
                   for ax, ay, bx, by in ICON_SEGMENTS) <= ink_stroke:
                color = INK
            row.append((*color, 255))
        samples.append(row)

    out = bytearray(size * size * 4)
    area = supersample * supersample
    for y in range(size):
        for x in range(size):
            red = green = blue = alpha = 0
            for sy in range(supersample):
                for sx in range(supersample):
                    r, g, b, a = samples[y * supersample + sy][x * supersample + sx]
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
            if alpha:
                red, green, blue = red // alpha, green // alpha, blue // alpha
            index = (y * size + x) * 4
            out[index:index + 4] = bytes((red, green, blue, alpha // area))
    return bytes(out)


def build_icon(path):
    """Write a single-image 32x32/32bpp ICO; regenerate invalid files."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 100:
        return path
    size = 32
    rgba = render_icon(size)
    pixels = bytearray()
    for y in range(size - 1, -1, -1):  # ICO pixel rows are bottom-up
        for x in range(size):
            index = (y * size + x) * 4
            r, g, b, a = rgba[index:index + 4]
            pixels += bytes((b, g, r, a))
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = 0
        for x in range(size):
            if rgba[(y * size + x) * 4 + 3] < 128:
                bits |= 1 << (31 - x)
        mask += bits.to_bytes(4, 'little')
    header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    data = header + bytes(pixels) + bytes(mask)
    entry = struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(data), 22)
    payload = struct.pack('<HHH', 0, 1, 1) + entry + data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------- tray icon
class TrayIcon(threading.Thread):
    def __init__(self, commands, icon_path=None, title='触控板手写'):
        super().__init__(name='touchpad tray icon', daemon=True)
        self.commands = commands
        self.title = title
        self.icon_path = Path(icon_path) if icon_path else (
            app_paths.data_dir() / 'tray.ico')
        self.mode = 'auto'
        self.hwnd = None
        self.hinst = None
        self.added = False
        self.error = ''
        self.wm_tray = WM_TRAY
        self.wm_mode = WM_MODE
        self.taskbar_created = 0
        self._hicon = 0
        self._fallback_icon = 0
        self._class = 'TouchpadTray_' + uuid.uuid4().hex
        self._wndproc = WNDPROC(self._on_message)
        self.removed = False

    # -- main-thread side --
    def set_mode(self, mode):
        if mode not in MODE_INDEX:
            return
        self.mode = mode
        if self.hwnd:
            U.PostMessageW(self.hwnd, self.wm_mode, MODE_INDEX[mode], 0)

    def stop(self, timeout=2.0):
        hwnd = self.hwnd
        if hwnd:
            U.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self.is_alive():
            self.join(timeout)

    def icon_rect(self):
        if not self.hwnd:
            return None
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize = C.sizeof(ident)
        ident.hWnd = self.hwnd
        ident.uID = 1
        rect = W.RECT()
        if shell32.Shell_NotifyIconGetRect(C.byref(ident), C.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right - rect.left,
                rect.bottom - rect.top)

    def handle_menu(self, command):
        """Map a TrackPopupMenu result to a host command (kept pure for tests)."""
        if command == MENU_AUTO:
            self.commands.put(('mode', 'auto'))
        elif command == MENU_TYPING:
            self.commands.put(('mode', 'typing'))
        elif command == MENU_DRAW:
            self.commands.put(('mode', 'draw'))
        elif command == MENU_PPT:
            self.commands.put(('mode', 'ppt'))
        elif command == MENU_NOTE:
            self.commands.put(('mode', 'note'))
        elif command == MENU_CLEAR:
            self.commands.put(('clear',))
        elif command == MENU_QUIT:
            self.commands.put(('quit',))

    # -- tray thread --
    def _tip(self):
        return '%s · %s' % (self.title, MODE_LABELS.get(self.mode, ''))

    def _load_icon(self):
        try:
            path = build_icon(self.icon_path)
        except OSError as exc:
            print('tray icon file unavailable: %s' % exc, file=sys.stderr)
            path = None
        size = U.GetSystemMetrics(SM_CXSMICON) or 16
        if path is not None:
            icon = U.LoadImageW(None, str(path), IMAGE_ICON, size, size,
                                LR_LOADFROMFILE)
            if not icon:
                icon = U.LoadImageW(None, str(path), IMAGE_ICON, 32, 32,
                                    LR_LOADFROMFILE)
            if icon:
                return icon
        self._fallback_icon = _icon_dll.LoadIconW(None, C.c_void_p(32512))
        return self._fallback_icon or 0

    def _data(self, flags):
        data = NOTIFYICONDATAW()
        data.cbSize = C.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self.wm_tray
        data.hIcon = self._hicon
        data.szTip = self._tip()
        return data

    def _add_icon(self):
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO)
        data.szInfoTitle = self.title + '已常驻'
        data.szInfo = ('右键或左键图标切换 自动识别 / 局部打字 / 全域画图 / '
                       'PPT 墨迹 / Note 墨迹；自动识别只在按住 Alt 时判断一次：'
                       'OneNote 写进笔记自己的墨迹，PPT 放映用放映的笔，'
                       '文本框旁走局部打字。所有手写都是按住 Alt 写、松开结束。'
                       'Windows 11 若把新图标收进 ^ 隐藏区，可拖到任务栏。')
        data.dwInfoFlags = NIIF_INFO
        data.uTimeoutOrVersion = 5000
        if not shell32.Shell_NotifyIconW(NIM_ADD, C.byref(data)):
            raise RuntimeError('Shell_NotifyIconW NIM_ADD failed')
        self.added = True
        self.removed = False

    def _modify_tip(self):
        if not self.added:
            return
        data = self._data(NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, C.byref(data))

    def _remove_icon(self):
        if not self.added or self.removed:
            return
        data = self._data(0)
        shell32.Shell_NotifyIconW(NIM_DELETE, C.byref(data))
        self.removed = True
        self.added = False

    def _build_menu(self):
        menu = U.CreatePopupMenu()
        if not menu:
            return None
        check = MF_CHECKED if self.mode == 'auto' else 0
        U.AppendMenuW(menu, MF_STRING | check, MENU_AUTO, '自动识别（推荐）')
        check = MF_CHECKED if self.mode == 'typing' else 0
        U.AppendMenuW(menu, MF_STRING | check, MENU_TYPING, '局部打字（光标旁）')
        check = MF_CHECKED if self.mode == 'draw' else 0
        U.AppendMenuW(menu, MF_STRING | check, MENU_DRAW, '全域画图（铺满屏幕）')
        check = MF_CHECKED if self.mode == 'ppt' else 0
        U.AppendMenuW(menu, MF_STRING | check, MENU_PPT, 'PPT 墨迹（左键笔画）')
        check = MF_CHECKED if self.mode == 'note' else 0
        U.AppendMenuW(menu, MF_STRING | check, MENU_NOTE,
                      'Note 墨迹（OneNote 左键笔画）')
        U.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        flags = MF_STRING | (0 if self.mode == 'draw' else MF_GRAYED)
        U.AppendMenuW(menu, flags, MENU_CLEAR, '清除全屏笔迹')
        U.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        U.AppendMenuW(menu, MF_STRING, MENU_QUIT, '退出')
        default = {'auto': MENU_AUTO, 'typing': MENU_TYPING,
                   'draw': MENU_DRAW, 'ppt': MENU_PPT,
                   'note': MENU_NOTE}.get(self.mode, MENU_AUTO)
        U.SetMenuDefaultItem(menu, default, 0)
        return menu

    def _popup(self):
        menu = self._build_menu()
        if not menu:
            return
        point = W.POINT()
        U.GetCursorPos(C.byref(point))
        U.SetForegroundWindow(self.hwnd)
        try:
            command = U.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                point.x, point.y, 0, self.hwnd, None)
        finally:
            U.DestroyMenu(menu)
            U.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        if command:
            self.handle_menu(command)

    def _on_message(self, hwnd, message, wparam, lparam):
        try:
            if message == self.wm_tray:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_RBUTTONUP,
                             WM_LBUTTONDBLCLK, WM_CONTEXTMENU):
                    self._popup()
            elif message == self.wm_mode:
                if wparam < len(MODE_ORDER):
                    self.mode = MODE_ORDER[wparam]
                self._modify_tip()
            elif message == self.taskbar_created and message:
                self._remove_icon()
                self._add_icon()
            elif message == WM_CLOSE:
                U.DestroyWindow(hwnd)
            elif message == WM_DESTROY:
                self._remove_icon()
                U.PostQuitMessage(0)
        except BaseException as exc:
            print('tray callback error: %s' % exc, file=sys.stderr)
        return U.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        try:
            self.hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = C.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = self.hinst
            wc.lpszClassName = self._class
            if not U.RegisterClassExW(C.byref(wc)):
                raise C.WinError(C.get_last_error())
            self.hwnd = U.CreateWindowExW(0, self._class, 'TouchpadTray', 0,
                                          0, 0, 1, 1, None, None,
                                          self.hinst, None)
            if not self.hwnd:
                raise C.WinError(C.get_last_error())
            self._hicon = self._load_icon()
            self.taskbar_created = U.RegisterWindowMessageW('TaskbarCreated')
            self._add_icon()
            msg = MSG()
            while U.GetMessageW(C.byref(msg), None, 0, 0) > 0:
                U.TranslateMessage(C.byref(msg))
                U.DispatchMessageW(C.byref(msg))
        except BaseException as exc:
            self.error = str(exc)
            print('tray unavailable: %s' % exc, file=sys.stderr)
        finally:
            self._remove_icon()
            if self.hwnd:
                U.DestroyWindow(self.hwnd)
                self.hwnd = None
            if self.hinst:
                U.UnregisterClassW(self._class, self.hinst)


def self_check():
    """Add the real icon briefly, report its tray rect, then remove it."""
    commands = queue.Queue()
    tray = TrayIcon(commands)
    tray.start()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and not tray.hwnd:
        if tray.error:
            break
        time.sleep(0.05)
    time.sleep(0.8)
    added = tray.added
    rect = tray.icon_rect()
    print('tray self-check: hwnd=%s added=%s rect=%s error=%s'
          % (bool(tray.hwnd), added, rect, tray.error or 'none'))
    print('note: on Windows 11 a new icon may sit in the ^ overflow until pinned')
    try:
        from PIL import Image
        icon = tray.icon_path or build_icon(
            app_paths.data_dir() / 'tray.ico')
        out = app_paths.data_dir() / 'tray_preview.png'
        with Image.open(build_icon(icon)) as source:
            source.resize((128, 128), Image.NEAREST).save(out)
        print('saved:', out)
    except BaseException as exc:
        print('icon preview skipped:', exc)
    tray.stop()
    print('tray self-check done: icon removed=%s' % tray.removed)
    return 0 if added and not tray.error else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true',
                        help='briefly add/remove the real tray icon')
    parser.add_argument('--icon-out', default='')
    args = parser.parse_args()
    if args.icon_out:
        print(build_icon(args.icon_out))
        return 0
    if args.self_check:
        return self_check()
    # Standalone mode: only the icon, menu choices are printed.
    commands = queue.Queue()
    tray = TrayIcon(commands)
    tray.start()
    try:
        while True:
            try:
                print(commands.get(timeout=1))
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
