"""Windows Raw Input + temporary mouse suppression for the verified SYN1B8B pad.

No driver/registry changes, ClipCursor, BlockInput, or keyboard recording.
All hooks live on a dedicated message thread and are removed on release.

Physical mouse events are swallowed while capturing, except a right click in
continuous (连写): it is delivered to the app and also ends continuous mode.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import queue
import threading
import time
import uuid

import app_paths
import pad_info
import ptp_layout
import ptp_probe as P
import raw_probe as R

U = R.user32
HOOKPROC = C.WINFUNCTYPE(C.c_ssize_t, C.c_int, W.WPARAM, W.LPARAM)

# Start hotkey candidates, in order. MOD_NOREPEAT keeps a held F8 from toggling.
HOTKEY_START = ((1, 'F8', 0x4000),)
HOTKEY_FALLBACK = ((3, 'Shift+F8', 0x4004), (4, 'Ctrl+Alt+F8', 0x4003))
HOTKEY_QUIT = ((2, 'Ctrl+F8', 0x4002),)
HOTKEY_QUIT_FALLBACK = ((5, 'Ctrl+Shift+F8', 0x4006),
                        (6, 'Ctrl+Alt+Shift+F8', 0x4007))
HOTKEY_QUIT_IDS = (2, 5, 6)


def api(name, restype, *args):
    fn = getattr(U, name)
    fn.restype, fn.argtypes = restype, list(args)
    return fn


api('SetWindowsHookExW', W.HANDLE, C.c_int, HOOKPROC, W.HINSTANCE, W.DWORD)
api('UnhookWindowsHookEx', W.BOOL, W.HANDLE)
api('CallNextHookEx', C.c_ssize_t, W.HANDLE, C.c_int, W.WPARAM, W.LPARAM)
api('RegisterHotKey', W.BOOL, W.HWND, C.c_int, W.UINT, W.UINT)
api('UnregisterHotKey', W.BOOL, W.HWND, C.c_int)
api('GetAsyncKeyState', W.SHORT, C.c_int)
api('GetForegroundWindow', W.HWND)
api('GetAncestor', W.HWND, W.HWND, W.UINT)
api('SetForegroundWindow', W.BOOL, W.HWND)
api('IsIconic', W.BOOL, W.HWND)
api('IsWindow', W.BOOL, W.HWND)
api('GetRawInputDeviceInfoW', W.UINT, W.HANDLE, W.UINT, W.LPVOID, C.POINTER(W.UINT))
api('DestroyWindow', W.BOOL, W.HWND)
api('UnregisterClassW', W.BOOL, W.LPCWSTR, W.HINSTANCE)
P.kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
P.kernel32.GetModuleHandleW.restype = W.HINSTANCE

# Digits are only ours while the candidate bar is fresh. Any printable key
# means the user went back to typing, and Microsoft Pinyin's own 1-5
# candidate keys must never be swallowed by our hook.
TYPING_KEYS = frozenset(
    (0x08, 0x0D, 0x20)                    # backspace, enter, space
    + tuple(range(0x30, 0x3A))            # digits (outside our limit)
    + tuple(range(0x41, 0x5B))            # letters
    + tuple(range(0xBA, 0xC1))            # ; = , - . / `
    + tuple(range(0xDB, 0xDF))            # [ \ ] '
    + (0xE2,))                            # OEM_102
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift/Ctrl/Alt/Win
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
RIGHT_BUTTON_MESSAGES = (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK)
SELECT_SECONDS = 30.0
GCS_COMPSTR = 0x0008
INPUT_LOG_PATH = os.path.join(str(app_paths.data_dir()), 'ime_input.log')


def input_log(text):
    """Append one diagnostic line to data/ime_input.log (never raises)."""
    try:
        os.makedirs(os.path.dirname(INPUT_LOG_PATH), exist_ok=True)
        with open(INPUT_LOG_PATH, 'a', encoding='utf-8') as handle:
            handle.write('%s %s\n'
                         % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except BaseException:
        pass

try:
    imm32 = C.WinDLL('imm32')
    imm32.ImmGetContext.restype = C.c_void_p
    imm32.ImmGetContext.argtypes = [W.HWND]
    imm32.ImmGetCompositionStringW.restype = C.c_long
    imm32.ImmGetCompositionStringW.argtypes = [
        C.c_void_p, W.DWORD, C.c_void_p, W.DWORD]
    imm32.ImmReleaseContext.restype = W.BOOL
    imm32.ImmReleaseContext.argtypes = [W.HWND, C.c_void_p]
except BaseException:  # tests and non-IME Windows still run
    imm32 = None


def ime_composing(window=None):
    """True while ``window``'s IME has a composition string (拼音候选)."""
    if imm32 is None:
        return False
    try:
        window = window or U.GetForegroundWindow()
        if not window:
            return False
        context = imm32.ImmGetContext(window)
        if not context:
            return False
        try:
            return imm32.ImmGetCompositionStringW(
                context, GCS_COMPSTR, None, 0) > 0
        finally:
            imm32.ImmReleaseContext(window, context)
    except BaseException:
        return False


class RAWHEADER(C.Structure):
    _fields_ = [('type', W.DWORD), ('size', W.DWORD),
                ('device', W.HANDLE), ('wparam', W.WPARAM)]


class MSLLHOOKSTRUCT(C.Structure):
    _fields_ = [('pt', W.POINT), ('mouseData', W.DWORD), ('flags', W.DWORD),
                ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


LLMHF_INJECTED = 0x00000001


def parse_report(report):
    """Legacy decoder for the verified Synaptics 30-byte report 3.

    Runtime capture uses :mod:`ptp_layout` and the pad's own descriptor; this
    function only keeps old recordings/tests reproducible.  HidP_GetUsages
    verified bit0 = Confidence (0x47), bit1 = Tip (0x42). Legacy JSONL called
    these bits 'tip' and 'inrange' respectively. Do not rewrite old recordings
    or silently treat legacy names as correct.
    """
    if len(report) != 30 or report[0] != 3 or report[28] > 5:
        raise ValueError('Unsupported PTP report; expected 30-byte report 3, <=5 contacts')
    contacts = []
    for slot in range(report[28]):
        base = 1 + slot * 5
        flags = report[base]
        contacts.append(dict(slot=slot, id=(flags >> 2) & 7,
                             x=int.from_bytes(report[base+1:base+3], 'little'),
                             y=int.from_bytes(report[base+3:base+5], 'little'),
                             tip=bool(flags & 2), confidence=bool(flags & 1), flags=flags))
    return dict(schema_version=2, scan=int.from_bytes(report[26:28], 'little'),
                count=report[28], buttons=report[29], contacts=contacts,
                hex=report.hex())


class StrokeTracker:
    """Unsmoothed display segments; raw recordings remain unfiltered.

    ``grace`` keeps a contact alive when its report goes missing for a frame
    or two, so a pad dropout does not lift an injected button mid-stroke. A
    contact reported with ``tip`` false is a real finger lift, though, and
    ends the stroke immediately: fast handwriting lifts and re-touches
    within one report interval, and merging that with grace would draw a
    long connector between two separate strokes.
    """
    def __init__(self, gap=0.15, grace=0.0):
        self.gap = gap
        self.grace = grace
        self.active = {}
        self.strokes = 0

    def update(self, frame):
        segments, present, lifted = [], set(), set()
        for c in frame['contacts']:
            cid = c['id']
            if not c['tip'] or not c['confidence']:
                if not c['tip']:
                    lifted.add(cid)
                continue
            if not (0 <= c['x'] <= pad_info.max_x()
                    and 0 <= c['y'] <= pad_info.max_y()):
                continue
            present.add(cid)
            point = (c['x'], c['y'])
            previous = self.active.get(cid)
            if previous is None or frame['t'] - previous[0] > self.gap:
                self.strokes += 1
                old_point = None
            else:
                old_point = previous[1]
            segments.append((cid, old_point, point))
            self.active[cid] = (frame['t'], point)
        for cid in list(self.active):
            if cid in lifted:
                del self.active[cid]
                continue
            if cid not in present:
                if self.grace and frame['t'] - self.active[cid][0] <= self.grace:
                    continue
                del self.active[cid]
        return segments


class CaptureGuard:
    """Testable fail-open state. No native side effects."""
    def __init__(self):
        self.active = False
        self.heartbeat = time.monotonic()
        self.started = self.last_input = 0.0

    def start(self, now):
        self.started = self.last_input = self.heartbeat = now
        self.active = True

    def release(self):
        self.active = False

    def reason(self, now, focused=True, alt=False, escape=False):
        if not self.active:
            return None
        if alt:
            return 'Alt：鼠标已释放'
        if escape:
            return 'Esc：鼠标已释放'
        if not focused:
            return '窗口失去焦点：鼠标已释放'
        if now - self.heartbeat > 2:
            return '界面响应超时：鼠标已释放'
        if now - self.last_input > 90:
            return '90 秒无触控输入：鼠标已释放'
        if now - self.started > 600:
            return '单次达到 10 分钟：已停止并释放'
        return None


class LiveInput(threading.Thread):
    def __init__(self, events, owner, allow_capture=True, fallback_hotkey=False,
                 start_hotkey=True, alt_releases=True, alt_stops=True,
                 alt_hold=False):
        super().__init__(name='PTP raw-input and release guard', daemon=True)
        self.events = events
        self.commands = queue.Queue()
        self.owner = owner
        self.target = None  # focused input window for the caret overlay mode
        self.allow_capture = allow_capture
        self.fallback_hotkey = fallback_hotkey
        self.start_hotkey = start_hotkey  # F8 mode; the overlay uses Alt hold
        self.alt_releases = alt_releases  # False while Alt is the write trigger
        self.alt_stops = alt_stops  # False when Alt only arms (PPT ink tap)
        self.alt_hold = alt_hold  # capture requires Alt to be physically down
        # Continuous (连写) sessions stay armed without Alt. While suppressed,
        # the physical pad is swallowed; a view may briefly lift suppression
        # (PPT continuous move) so the pad moves the pointer again.
        self.continuous = False
        self.suppress = True
        self.free_hold = False
        # A right click in 连写 belongs to the app (context menu) and ends
        # continuous writing. Remember its press so the matching release is
        # delivered even if the view drops `continuous` in between.
        self.right_pass = False
        self.guard = CaptureGuard()
        self.stopping = threading.Event()
        self.hwnd = self.mouse_hook = self.key_hook = None
        self.hotkey = 0
        self.hotkey_label = 'F8'
        self.hotkey_quit = 0
        self.hotkey_quit_label = 'Ctrl+F8'
        self.selecting = False
        self.select_limit = 0
        self.select_deadline = 0.0
        self.select_blocked = False
        self.swallowed = set()
        self.path = ''
        self.pad = None
        self.reader = None
        self.devices = {}
        self.hinst = P.kernel32.GetModuleHandleW(None)
        self.mouse_callback = HOOKPROC(self._mouse)
        self.key_callback = HOOKPROC(self._keyboard)
        self.window_callback = R.WNDPROC(self._window)
        self.class_name = 'TouchpadLive_' + uuid.uuid4().hex

    def emit(self, kind, payload=None):
        self.events.put((kind, payload))

    def request(self, command):
        self.commands.put(command)

    def register_hotkey(self, register=None):
        """Prefer F8; overlay mode may fall back if another app owns it."""
        register = register or U.RegisterHotKey
        options = list(HOTKEY_START)
        if self.fallback_hotkey:
            options += list(HOTKEY_FALLBACK)
        for ident, label, modifiers in options:
            if register(self.hwnd, ident, modifiers, 0x77):
                return ident, label
        return 0, 'F8'

    def register_quit_hotkey(self, register=None):
        """Ctrl+F8 normally; the overlay mode may fall back to a free combo."""
        register = register or U.RegisterHotKey
        options = list(HOTKEY_QUIT)
        if self.fallback_hotkey:
            options += list(HOTKEY_QUIT_FALLBACK)
        for ident, label, modifiers in options:
            if register(self.hwnd, ident, modifiers, 0x77):
                return ident, label
        return 0, 'Ctrl+F8'

    def focused(self):
        window = self.target or self.owner
        return bool(window) and U.GetForegroundWindow() == window and not U.IsIconic(window)

    def safety(self):
        alt = (self.alt_releases and not self.continuous
               and bool(U.GetAsyncKeyState(0x12) & 0x8000))
        reason = self.guard.reason(time.monotonic(), self.focused(), alt,
                                   bool(U.GetAsyncKeyState(0x1B) & 0x8000))
        if reason:
            self.release(reason)

    def release(self, reason='鼠标已释放'):
        was_active = self.guard.active
        self.guard.release()  # fail open BEFORE UI, I/O or unhook calls
        self.selecting = False
        self.select_limit = 0
        self.select_deadline = 0.0
        self.select_blocked = False
        self.swallowed.clear()
        self.continuous = False
        self.suppress = True
        self.free_hold = False
        self.right_pass = False
        for name in ('mouse_hook', 'key_hook'):
            handle = getattr(self, name)
            if handle:
                U.UnhookWindowsHookEx(handle)
                setattr(self, name, None)
        if was_active:
            self.emit('stopped', reason)

    def _select_on(self, limit):
        """Observe digits 1..limit while the candidate bar is visible."""
        limit = max(0, min(int(limit), 9))
        composing = bool(limit) and ime_composing(self.target)
        if self.stopping.is_set() or not limit or composing:
            if composing and not self.select_blocked:
                # Tell the view once per episode: otherwise the bar looks
                # ready while every digit still belongs to the pinyin IME.
                self.select_blocked = True
                input_log('select blocked: IME composing target=%s'
                          % self.target)
                self.emit('select_blocked', '输入法组词中')
            self._select_off()
            return
        self.select_blocked = False
        if not self.key_hook:
            self.key_hook = U.SetWindowsHookExW(13, self.key_callback, self.hinst, 0)
            if not self.key_hook:
                self.selecting = False
                self.emit('select_failed', '无法安装数字选字键盘钩子')
                return
        if not self.selecting and self.target:
            # Reaching here already proved the IME is not composing. A
            # self-check replay has no target and should not touch the log.
            input_log('select_on limit=%d composing=False target=%s'
                      % (limit, self.target))
        self.select_limit = limit
        self.selecting = True
        self.select_deadline = time.monotonic() + SELECT_SECONDS
        self.swallowed.clear()

    def _select_off(self):
        self.selecting = False
        self.select_limit = 0
        self.select_deadline = 0.0
        # Keep swallowed keys so their key-up is still eaten after a choice.
        if not self.guard.active and self.key_hook:
            U.UnhookWindowsHookEx(self.key_hook)
            self.key_hook = None

    def _mouse(self, code, message, data):
        try:
            if code >= 0 and self.guard.active:
                self.safety()
                if self.guard.active:
                    if data:
                        info = C.cast(data, C.POINTER(MSLLHOOKSTRUCT)).contents
                        # Precision touchpads turn a two-finger pinch into
                        # Ctrl+wheel, often delivered as injected input. The
                        # wheel belongs to our region zoom while capturing;
                        # letting it through zooms PowerPoint underneath.
                        if message == 0x020A:  # WM_MOUSEWHEEL
                            if info.flags & LLMHF_INJECTED:
                                return 1
                            # A pinch arrives as Ctrl+wheel (sometimes as a
                            # physical event). In 连写 move mode suppression is
                            # briefly lifted for the pointer, but a pinch must
                            # still never zoom the slide: swallow it and let
                            # the raw contacts zoom our region instead.
                            ctrl = bool(U.GetAsyncKeyState(0x11) & 0x8000)
                            if ctrl and not self.suppress:
                                return 1
                            if self.suppress:
                                raw = info.mouseData >> 16
                                delta = ((raw & 0xFFFF) ^ 0x8000) - 0x8000
                                self.emit('wheel', delta)
                                return 1
                        # Let our own SendInput strokes through (PPT ink mode);
                        # only the physical pad/mouse is swallowed.
                        if info.flags & LLMHF_INJECTED:
                            return U.CallNextHookEx(None, code, message, data)
                        # 连写 owns the pad, but a right click is not ink: it
                        # goes to the app (context menu) and ends 连写. Deliver
                        # the matching release too, even if the view has
                        # already dropped `continuous` by then.
                        if self.continuous and message in RIGHT_BUTTON_MESSAGES:
                            if message == WM_RBUTTONUP:
                                self.right_pass = False
                            else:
                                self.right_pass = True
                                self.emit('exit_continuous', '右键退出连写')
                            return U.CallNextHookEx(None, code, message, data)
                        if self.right_pass and message in RIGHT_BUTTON_MESSAGES:
                            if message == WM_RBUTTONUP:
                                self.right_pass = False
                            return U.CallNextHookEx(None, code, message, data)
                    if not self.suppress:
                        # PPT continuous move: the physical pad drives the
                        # pointer, but the raw-input capture stays armed.
                        return U.CallNextHookEx(None, code, message, data)
                    return 1  # movement, clicks and wheel; no pointer relocation
        except BaseException:
            self.release('鼠标处理异常：已释放')
        return U.CallNextHookEx(None, code, message, data)

    def _modified(self):
        """Ctrl/Shift/Alt/Win held: digits are app shortcuts, not candidates."""
        return any(U.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_KEYS)

    def _composing(self):
        """True while the target IME owns the digits (拼音组词中)."""
        return ime_composing(self.target)

    def _keyboard(self, code, message, data):
        try:
            if code >= 0 and message in (0x100, 0x101, 0x104, 0x105):
                vk = C.cast(data, C.POINTER(W.DWORD)).contents.value
                modified = self._modified() if message == 0x100 else False
                if message == 0x101 and vk in self.swallowed:
                    self.swallowed.discard(vk)
                    return 1  # never leak the second half of a chosen digit
                if message == 0x100 and vk in self.swallowed:
                    return 1  # auto-repeat of a swallowed digit
                if (message == 0x100 and self.selecting
                        and self.select_deadline
                        and time.monotonic() > self.select_deadline):
                    self._select_off()
                    self.emit('typing', None)  # stale candidates: hand keys back
                if (message == 0x100 and self.selecting
                        and 0x31 <= vk <= 0x39
                        and vk - 0x30 <= self.select_limit
                        and modified):
                    # Alt/Ctrl/Shift still down: the digit is an app shortcut
                    # (or Alt is still held from writing), not a candidate.
                    input_log('digit %d passed: modifier held' % (vk - 0x30))
                if (message == 0x100 and self.selecting
                        and 0x31 <= vk <= 0x39
                        and vk - 0x30 <= self.select_limit
                        and not modified):
                    if self._composing():
                        # A pinyin composition already owns the digit keys.
                        input_log('digit %d passed: IME composing'
                                  % (vk - 0x30))
                        self._select_off()
                        self.emit('typing', None)
                    else:
                        input_log('digit %d selected' % (vk - 0x30))
                        self.swallowed.add(vk)
                        self.emit('select', vk - 0x30)
                        return 1  # swallow the down; its up is eaten below
                if (self.selecting and message == 0x100
                        and vk in TYPING_KEYS and not modified):
                    # Pinyin/ordinary typing resumed: our digits are stale,
                    # so the IME gets every future key untouched.
                    self._select_off()
                    self.emit('typing', None)
                if message in (0x100, 0x104):
                    if self.alt_releases and vk in (0x12, 0xA4, 0xA5):
                        self.release('Alt：鼠标已释放')
                    elif vk == 0x1B:
                        self.release('Esc：取消并释放')
                    elif vk in (0x5B, 0x5C):
                        self.release('Windows 键：鼠标已释放')
                elif message in (0x101, 0x105) and vk in (0x12, 0xA4, 0xA5):
                    # A continuous session stays armed across Alt; the view
                    # polls the tap and decides what it means.
                    if (self.alt_stops and self.guard.active
                            and not self.continuous):
                        self.release('Alt：松开并识别')
        except BaseException:
            self.release('释放键处理异常：已释放')
        # Never swallow Alt, Esc, Windows keys or ordinary typing.
        return U.CallNextHookEx(None, code, message, data)

    def begin(self):
        if self.stopping.is_set():
            return
        if not self.allow_capture:
            self.emit('notice', '安全预览模式：不会捕获鼠标')
            return
        phrase = f'按 {self.hotkey_label}' if self.start_hotkey else '按住 Alt'
        if self.guard.active:
            self.release(f'{self.hotkey_label}：已停止并释放')
            return
        if self.target:
            if not U.IsWindow(self.target) or U.IsIconic(self.target):
                self.emit('notice', '目标输入窗口已关闭或最小化')
                return
            if U.GetForegroundWindow() != self.target:
                self.emit('notice', f'目标输入框已失焦，请重新{phrase}')
                return
        elif not self.focused():
            self.emit('notice', f'请先选中轨迹窗口，再{phrase}')
            return
        if (self.alt_hold and not self.free_hold
                and not (U.GetAsyncKeyState(0x12) & 0x8000)):
            # Alt was released between the trigger and this request; an
            # armed session without the key would never see its key-up.
            self.emit('notice', '请按住 Alt 再写')
            return
        # Do not strand a pre-existing mouse drag, Alt+Tab or Esc.
        blocked = (1, 2, 4, 5, 6, 0x1B) + ((0x12,) if self.alt_releases else ())
        if any(U.GetAsyncKeyState(k) & 0x8000 for k in blocked):
            keys = '鼠标按钮、Alt 和 Esc' if self.alt_releases else '鼠标按钮和 Esc'
            self.emit('notice', f'请松开{keys}，再{phrase}')
            return
        try:
            self.selecting = False
            self.select_limit = 0
            self.suppress = True
            if not self.key_hook:
                self.key_hook = U.SetWindowsHookExW(13, self.key_callback, self.hinst, 0)
                if not self.key_hook:
                    raise C.WinError(C.get_last_error())
            self.mouse_hook = U.SetWindowsHookExW(14, self.mouse_callback, self.hinst, 0)
            if not self.mouse_hook:
                raise C.WinError(C.get_last_error())
            if self.stopping.is_set():
                self.release()
                return
            self.guard.start(time.monotonic())
            self.emit('started', {'path': self.path, 'start': self.guard.started})
        except BaseException as exc:
            self.release()
            self.emit('notice', '无法捕获，鼠标未锁定：' + str(exc))

    def _device_matches(self, handle):
        if handle not in self.devices:
            n = W.UINT()
            U.GetRawInputDeviceInfoW(handle, 0x20000007, None, C.byref(n))
            if not n.value:
                return False
            name = C.create_unicode_buffer(n.value + 1)
            if U.GetRawInputDeviceInfoW(handle, 0x20000007, name, C.byref(n)) == 0xFFFFFFFF:
                return False
            self.devices[handle] = name.value.casefold() == self.path.casefold()
        return self.devices[handle]

    def _input(self, lparam):
        if not self.guard.active:
            return  # no background recording while released
        size = W.UINT()
        hs = C.sizeof(RAWHEADER)
        if U.GetRawInputData(lparam, R.RID_INPUT, None, C.byref(size), hs) == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())
        if not hs + 8 <= size.value <= 65536:
            raise ValueError('Unexpected RAWINPUT size')
        raw = C.create_string_buffer(size.value)
        got = U.GetRawInputData(lparam, R.RID_INPUT, raw, C.byref(size), hs)
        if got == 0xFFFFFFFF or got < hs + 8:
            raise ValueError('GetRawInputData failed')
        data = raw.raw[:got]
        header = RAWHEADER.from_buffer_copy(data)
        if header.type != R.RIM_TYPEHID or not self._device_matches(header.device):
            return
        size_hid, count = R.u32(data, hs), R.u32(data, hs + 4)
        expected = self.pad.layout.input_length if self.pad else 0
        if size_hid != expected or hs + 8 + size_hid * count > got:
            raise ValueError('Unexpected HID packet layout')
        now = time.monotonic()
        self.guard.last_input = now
        if self.events.qsize() > 4096:
            self.release('界面处理积压：已释放；本段数据可能不完整')
            return
        for i in range(count):
            off = hs + 8 + i * size_hid
            frame = self.reader.read(data[off:off+size_hid])
            if frame is None:
                continue  # other report IDs / a layout this pad does not use
            frame['t'] = round(now - self.guard.started, 6)
            self.emit('frame', frame)

    def _window(self, hwnd, message, wparam, lparam):
        try:
            if message == R.WM_INPUT:
                self._input(lparam)
            elif message == 0x312:
                if wparam in (1, 3, 4):  # F8, Shift+F8, Ctrl+Alt+F8
                    self.emit('toggle')
                elif wparam in HOTKEY_QUIT_IDS:
                    self.emit('quit')
        except BaseException as exc:
            self.release('输入异常：鼠标已释放')
            self.emit('notice', str(exc))
        # DefWindowProc is required to clean up foreground WM_INPUT.
        return U.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        registered = False
        try:
            self.pad = pad_info.device() or pad_info.configure()
            if self.pad is None:
                raise RuntimeError(pad_info.failure())
            self.path = self.pad.path
            self.reader = ptp_layout.HidReader(self.pad)
            wc = R.WNDCLASSEXW()
            wc.cbSize, wc.lpfnWndProc = C.sizeof(wc), self.window_callback
            wc.hInstance, wc.lpszClassName = self.hinst, self.class_name
            if not U.RegisterClassExW(C.byref(wc)):
                raise C.WinError(C.get_last_error())
            registered = True
            self.hwnd = U.CreateWindowExW(0, self.class_name, 'PTP raw input', 0,
                                         0, 0, 1, 1, None, None, self.hinst, None)
            if not self.hwnd:
                raise C.WinError(C.get_last_error())
            rid = R.RAWINPUTDEVICE(13, 5, R.RIDEV_INPUTSINK, self.hwnd)
            if not U.RegisterRawInputDevices(C.byref(rid), 1, C.sizeof(rid)):
                raise C.WinError(C.get_last_error())
            if self.start_hotkey:
                self.hotkey, self.hotkey_label = self.register_hotkey()
            else:
                self.hotkey, self.hotkey_label = 0, 'Alt'
            self.hotkey_quit, self.hotkey_quit_label = self.register_quit_hotkey()
            self.emit('ready', {'global_f8': self.hotkey == 1,
                                'global_quit': self.hotkey_quit == 2,
                                'start_hotkey': self.start_hotkey,
                                'hotkey': self.hotkey_label,
                                'quit': bool(self.hotkey_quit),
                                'quit_key': self.hotkey_quit_label,
                                'path': self.path})
            msg = W.MSG()
            # raw_probe declares its own MSG; cast for the shared function signatures.
            msgptr = C.cast(C.byref(msg), C.POINTER(R.MSG))
            while not self.stopping.is_set():
                self.safety()
                for _ in range(256):
                    if not U.PeekMessageW(msgptr, None, 0, 0, R.PM_REMOVE):
                        break
                    U.TranslateMessage(msgptr)
                    U.DispatchMessageW(msgptr)
                for _ in range(32):
                    if self.stopping.is_set():
                        break
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == 'start':
                        self.begin()
                    elif command == 'release':
                        self.release()
                    elif isinstance(command, tuple):
                        if command[0] == 'select_on':
                            self._select_on(command[1])
                        elif command[0] == 'select_off':
                            self._select_off()
                self.stopping.wait(0.003)
        except BaseException as exc:
            self.emit('error', str(exc))
        finally:
            self.release('窗口关闭：鼠标已释放')
            if self.hotkey:
                U.UnregisterHotKey(self.hwnd, self.hotkey)
            if self.hotkey_quit:
                U.UnregisterHotKey(self.hwnd, self.hotkey_quit)
            if self.hwnd:
                rid = R.RAWINPUTDEVICE(13, 5, 1, None)  # RIDEV_REMOVE
                U.RegisterRawInputDevices(C.byref(rid), 1, C.sizeof(rid))
                U.DestroyWindow(self.hwnd)
            if registered:
                U.UnregisterClassW(self.class_name, self.hinst)
            if self.reader is not None:
                self.reader.close()
                self.reader = None
            self.emit('closed')
