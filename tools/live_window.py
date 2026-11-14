"""Fixed-size touchpad motion window. F8 captures, Alt/Esc releases.

Double-click live_window.bat. Starts idle; no mouse capture or recording until F8.
--self-check renders a safe non-activating replay; it cannot install capture hooks.
"""
import argparse
import csv
import ctypes as C
import datetime as dt
import json
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import traceback

import pad_info
import windows_ink
import app_paths
from live_input import LiveInput, StrokeTracker, U

BASE = app_paths.app_dir()
BG, PANEL, TEXT, MUTED, ACCENT = '#101820', '#17232e', '#e7eef5', '#94a8b9', '#52dac1'
COLORS = ('#52dac1', '#79aefb', '#f7c577', '#d5a0fa', '#fc8e9b')


class Session:
    def __init__(self, device, directory=None, prefix='live'):
        directory = Path(directory or BASE / 'data')
        directory.mkdir(parents=True, exist_ok=True)
        # Windows clock resolution can repeat a microsecond stamp when two
        # views start capture at the same moment; bump the suffix instead of
        # failing the whole start with FileExistsError.
        stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        index = 0
        while True:
            name = prefix + '_' + stamp + ('' if not index else '_%d' % index)
            self.base = directory / name
            try:
                self.file = self.base.with_suffix('.jsonl').open('x', encoding='utf-8')
            except FileExistsError:
                index += 1
                continue
            try:
                self.csv_file = self.base.with_suffix('.contacts.csv').open('x', newline='', encoding='utf-8')
                break
            except FileExistsError:
                self.file.close()
                try:
                    self.base.with_suffix('.jsonl').unlink()
                except OSError:
                    pass
                index += 1
            except BaseException:
                self.file.close()
                raise
        self.csv = csv.writer(self.csv_file)
        self.csv.writerow(['frame', 't', 'scan', 'slot', 'contact_id', 'x', 'y', 'tip', 'confidence', 'flags'])
        self.frames = self.points = 0
        self.device = device
        self.created = dt.datetime.now().isoformat(timespec='milliseconds')
        self.last_flush = time.monotonic()
        self.closed = False
        self.finalized = False
        self.stop_reason = None

    def write(self, frame):
        if self.closed:
            raise RuntimeError('Recording streams already closed')
        self.file.write(json.dumps(frame, ensure_ascii=False) + '\n')
        for c in frame['contacts']:
            self.csv.writerow([self.frames, frame['t'], frame['scan'], c['slot'], c['id'],
                               c['x'], c['y'], int(c['tip']), int(c['confidence']), c['flags']])
        self.frames += 1
        self.points += len(frame['contacts'])
        if time.monotonic() - self.last_flush > 0.25:
            self.file.flush()
            self.csv_file.flush()
            self.last_flush = time.monotonic()

    def close(self, reason):
        if self.finalized:
            return
        self.closed = True
        self.stop_reason = self.stop_reason or reason
        try:
            self.file.close()
        finally:
            self.csv_file.close()
        meta = dict(schema_version=2, device=self.device, created_at=self.created,
                    frames=self.frames, contact_rows=self.points, stop_reason=self.stop_reason,
                    x_range=[0, pad_info.max_x()], y_range=[0, pad_info.max_y()],
                    pressure_available=False,
                    confidence_usage='0x0D/0x47', tip_usage='0x0D/0x42',
                    raw_data=str(self.base.with_suffix('.jsonl')),
                    contacts_csv=str(self.base.with_suffix('.contacts.csv')))
        temporary = self.base.with_suffix('.meta.tmp')
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.base.with_suffix('.meta.json'))
        self.finalized = True


class MotionWindow:
    def __init__(self, root, safe_preview=False):
        self.root = root
        self.safe_preview = safe_preview
        self.events = queue.Queue()
        self.worker = None
        self.ready = self.active = self.pending = self.closing = False
        self.global_f8 = False
        self.session = None
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active = {}
        self.last_capture_strokes = []
        self.recognition_token = 0
        self.frame_count = 0
        self.last_ui_update = 0
        self.last_path = ''
        root.withdraw()
        root.title('触控板 · 运动窗口')
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.on_error
        root.bind('<F8>', self.local_toggle)
        root.bind('<F9>', lambda e: self.clear())
        root.bind('<Escape>', lambda e: self.release())
        root.bind('<Alt_L>', lambda e: self.release())
        root.bind('<Alt_R>', lambda e: self.release())
        root.bind('<FocusOut>', lambda e: root.after(20, self.check_focus))
        root.bind('<Unmap>', lambda e: self.release() if e.widget == root else None)

        outer = tk.Frame(root, bg=BG)
        outer.pack(padx=24, pady=20)
        top = tk.Frame(outer, bg=BG)
        top.pack(fill='x')
        tk.Label(top, text='触控板 / 运动窗口', font=('Microsoft YaHei UI', 19, 'bold'),
                 bg=BG, fg=TEXT).pack(side='left')
        self.badge = tk.Label(top, text='待机 · 鼠标自由', font=('Microsoft YaHei UI', 10),
                              bg=PANEL, fg=ACCENT, padx=14, pady=8)
        self.badge.pack(side='right')
        tk.Label(outer, text='F8 开始 / 停止     Alt 或 Esc 立即释放     F9 清空画面',
                 bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(8, 15))

        # Fixed aspect ratio; device coordinates are not screen cursor coordinates.
        self.cw, self.ch = 800, 510
        self.pad_w, self.pad_h = pad_info.max_x(), pad_info.max_y()
        self.scale = min((self.cw - 40) / self.pad_w,
                         (self.ch - 40) / self.pad_h)
        self.ox = (self.cw - self.scale * self.pad_w) / 2
        self.oy = (self.ch - self.scale * self.pad_h) / 2
        self.canvas = tk.Canvas(outer, width=self.cw, height=self.ch, bg=PANEL,
                                highlightthickness=1, highlightbackground='#2c3e4d')
        self.canvas.pack()
        self.grid()
        self.hint = self.canvas.create_text(self.cw/2, self.ch/2, text='按 F8 后，在触控板上移动或书写\n无需按住触控板',
                                             justify='center', fill=MUTED,
                                             font=('Microsoft YaHei UI', 13))
        row = tk.Frame(outer, bg=BG)
        row.pack(fill='x', pady=(12, 0))
        self.stats = tk.Label(row, text='X —   Y —     触点 0     帧 0     笔画 0',
                              bg=BG, fg=TEXT, font=('Consolas', 10))
        self.stats.pack(side='left')
        self.start_btn = tk.Button(row, text='开始捕获  F8', command=self.toggle, bg=ACCENT,
                                   fg=BG, activebackground='#7ee7d4', relief='flat',
                                   font=('Microsoft YaHei UI', 10), padx=12, pady=6, state='disabled')
        self.start_btn.pack(side='right')
        tk.Button(row, text='清空 F9', command=self.clear, bg=PANEL, fg=TEXT,
                  activebackground='#2c3e4d', relief='flat', padx=10, pady=6).pack(side='right', padx=10)

        recog = tk.Frame(outer, bg=BG)
        recog.pack(fill='x', pady=(10, 0))
        self.recog_label = tk.Label(recog, text='识别：写完按 F8 停止后自动识别',
                                    bg=BG, fg=TEXT, font=('Microsoft YaHei UI', 10),
                                    anchor='w', justify='left')
        self.recog_label.pack(side='left')
        self.retry_btn = tk.Button(recog, text='重新识别', command=self.recognize_last,
                                   bg=PANEL, fg=TEXT, activebackground='#2c3e4d',
                                   relief='flat', padx=10, pady=5,
                                   font=('Microsoft YaHei UI', 9), state='disabled')
        self.retry_btn.pack(side='right')
        self.candidate_bar = tk.Frame(outer, bg=BG, height=38)
        self.candidate_bar.pack(fill='x', pady=(5, 0))
        self.candidate_bar.pack_propagate(False)

        self.status = tk.Label(outer, text='正在检测触控板…', wraplength=800, anchor='w', justify='left',
                               bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9))
        self.status.pack(fill='x', pady=(12, 4))
        tk.Label(outer, text='捕获时暂停所有鼠标的移动 / 点击 / 滚轮；Alt+Tab 仍可用。失焦自动释放。',
                 bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w')
        root.update_idletasks()
        self.owner = U.GetAncestor(root.winfo_id(), 2)
        self.worker = LiveInput(self.events, self.owner, allow_capture=not safe_preview)
        self.worker.start()
        if not safe_preview:
            root.deiconify()
        root.after(16, self.tick)

    def grid(self):
        for x in range(0, 1333, 222):
            px, _ = self.map(x, 0)
            self.canvas.create_line(px, self.oy, px, self.ch-self.oy, fill='#253543', tags='grid')
        for y in range(0, 829, 138):
            _, py = self.map(0, y)
            self.canvas.create_line(self.ox, py, self.cw-self.ox, py, fill='#253543', tags='grid')
        self.canvas.create_rectangle(self.ox, self.oy, self.cw-self.ox, self.ch-self.oy,
                                      outline='#425a6b', tags='grid')
        self.canvas.create_text(self.ox+8, self.oy+10, text='0,0', anchor='nw', fill=MUTED, tags='grid')
        self.canvas.create_text(self.cw-self.ox-8, self.ch-self.oy-10,
                                text='%d,%d' % (self.pad_w, self.pad_h),
                                anchor='se', fill=MUTED, tags='grid')

    def map(self, x, y):
        return self.ox+x*self.scale, self.oy+y*self.scale

    def local_toggle(self, event):
        if not self.global_f8:
            self.toggle()
        return 'break'

    def toggle(self):
        if not self.ready or self.pending or self.safe_preview:
            return
        if self.active or self.worker.guard.active:
            self.release()
            return
        self.root.deiconify()
        self.root.lift()
        U.SetForegroundWindow(self.owner)
        self.pending = True
        self.worker.request('start')

    def release(self):
        if self.worker:
            self.worker.request('release')

    def check_focus(self):
        if self.worker and self.worker.guard.active and not self.worker.focused():
            self.release()

    def clear(self):
        if self.active or self.pending:
            self.status.config(text='先按 Alt 释放，再按 F9 清空；采样文件不会被删除。')
            return
        self.canvas.delete('ink')
        self.canvas.delete('cursor')
        self.canvas.itemconfigure(self.hint, state='normal')
        self.tracker = StrokeTracker()
        self.recording_strokes = []
        self.recording_active.clear()
        self.last_capture_strokes = []
        self.recognition_token += 1
        self.clear_candidates()
        self.recog_label.config(text='识别：写完按 F8 停止后自动识别')
        self.retry_btn.config(state='disabled')
        self.frame_count = 0
        self.stats.config(text='X —   Y —     触点 0     帧 0     笔画 0')

    def draw(self, frame):
        self.canvas.itemconfigure(self.hint, state='hidden')
        self.canvas.delete('cursor')
        segments = self.tracker.update(frame)
        self.track_strokes(frame, segments)
        for cid, previous, point in segments:
            px, py = self.map(*point)
            color = COLORS[cid % len(COLORS)]
            if previous is None:
                self.canvas.create_oval(px-1.8, py-1.8, px+1.8, py+1.8, fill=color, outline='', tags='ink')
            else:
                x0, y0 = self.map(*previous)
                self.canvas.create_line(x0, y0, px, py, fill=color, width=2.5,
                                        capstyle='round', tags='ink')
            self.canvas.create_oval(px-6, py-6, px+6, py+6, outline=color, width=2, tags='cursor')
        self.frame_count += 1
        if time.monotonic() - self.last_ui_update > 0.08 or self.safe_preview:
            pos = 'X —   Y —' if not segments else 'X %4d   Y %4d' % segments[-1][2]
            self.stats.config(text=f'{pos}     触点 {len(segments)}     帧 {self.frame_count}     笔画 {self.tracker.strokes}')
            self.last_ui_update = time.monotonic()
        # Bound display memory; never delete recorded data or join disjoint paths.
        if self.frame_count % 1000 == 0:
            ink = self.canvas.find_withtag('ink')
            if len(ink) > 20000:
                for item in ink[:-15000]:
                    self.canvas.delete(item)

    def track_strokes(self, frame, segments):
        """Keep the same strokes that are drawn, for Windows Ink recognition."""
        if self.closing:
            return
        for cid, previous, point in segments:
            if previous is None or cid not in self.recording_active:
                stroke = {'contact': cid,
                          'points': [(frame['t'], point[0], point[1])]}
                self.recording_strokes.append(stroke)
                self.recording_active[cid] = stroke
            else:
                self.recording_active[cid]['points'].append(
                    (frame['t'], point[0], point[1]))
        for cid in list(self.recording_active):
            if cid not in self.tracker.active:
                del self.recording_active[cid]

    def clear_candidates(self):
        for widget in self.candidate_bar.winfo_children():
            widget.destroy()

    def recognize_last(self):
        if self.closing:
            return
        if not self.last_capture_strokes:
            self.recog_label.config(text='识别：没有可识别的笔画')
            self.clear_candidates()
            self.retry_btn.config(state='disabled')
            return
        self.retry_btn.config(state='normal')
        self.recognition_token += 1
        token = self.recognition_token
        self.recog_label.config(text='识别中…')
        self.clear_candidates()
        threading.Thread(target=self._recognize_worker,
                         args=(self.last_capture_strokes, token),
                         daemon=True).start()

    def _recognize_worker(self, strokes, token):
        try:
            payload = dict(windows_ink.recognize(strokes), token=token)
        except Exception as exc:
            payload = {'token': token, 'error': str(exc)}
        self.events.put(('recognized', payload))

    def show_recognition(self, data):
        self.clear_candidates()
        if data.get('error'):
            self.recog_label.config(text='识别不可用：' + data['error'])
            return
        results = data.get('results') or []
        if not results:
            self.recog_label.config(text='识别：没有得到候选字')
            return
        text = data.get('text') or results[0]['top']
        extra = ' · 共 %d 段' % len(results) if len(results) > 1 else ''
        self.recog_label.config(text='识别：%s%s    点击候选复制' % (text, extra))
        tk.Label(self.candidate_bar, text='候选', bg=BG, fg=MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
        for candidate in results[0]['candidates']:
            tk.Button(self.candidate_bar, text=candidate,
                      command=lambda value=candidate: self.copy_text(value),
                      bg=PANEL, fg=TEXT, activebackground=ACCENT,
                      activeforeground=BG, relief='flat', padx=12, pady=3,
                      font=('Microsoft YaHei UI', 12)).pack(side='left', padx=(6, 0))

    def copy_text(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            self.status.config(text='已复制到剪贴板：' + text)
        except Exception as exc:
            self.status.config(text='复制失败：' + str(exc))

    def event(self, kind, data):
        if kind == 'ready':
            self.ready = True
            self.global_f8 = data['global_f8']
            self.start_btn.config(state='normal' if not self.safe_preview else 'disabled')
            suffix = '' if self.global_f8 else '；全局 F8 被占用，请使用开始按钮或窗口内 F8'
            self.status.config(text='Synaptics 已连接 · 默认不录制 · F8 后自动保存原始 JSONL 和触点 CSV' + suffix)
        elif kind == 'toggle':
            self.toggle()
        elif kind == 'started':
            self.pending = False
            self.session = Session(data['path'])
            self.last_path = str(self.session.base.with_suffix('.jsonl'))
            if self.closing:
                return  # retain queued frames without changing window activation/UI
            self.clear()
            self.active = True
            self.badge.config(text='捕获中 · Alt 释放', fg='#101820', bg=ACCENT)
            self.start_btn.config(text='Alt 释放鼠标')
            self.status.config(text='正在录制：' + Path(self.last_path).name)
        elif kind == 'frame':
            if self.session and not self.session.closed:
                self.session.write(data)
                self.draw(data)
        elif kind == 'stopped':
            self.active = self.pending = False
            self.canvas.delete('cursor')
            self.tracker.active.clear()
            self.recording_active.clear()
            self.last_capture_strokes = [
                [(point[1], point[2]) for point in stroke['points']]
                for stroke in self.recording_strokes if stroke['points']]
            self.recording_strokes = []
            self.badge.config(text='已释放 · 鼠标自由', fg=ACCENT, bg=PANEL)
            self.start_btn.config(text='再次捕获 F8')
            if self.session:
                self.session.close(data)
                self.session = None
            self.status.config(text=data + (' · 已保存：' + Path(self.last_path).name if self.last_path else ''))
            if not self.closing:
                self.recognize_last()
        elif kind == 'recognized':
            if data.get('token') == self.recognition_token:
                self.show_recognition(data)
        elif kind in ('notice', 'error'):
            self.pending = False
            self.status.config(text=data)
            if kind == 'error':
                self.ready = False
                self.start_btn.config(state='disabled')
        elif kind == 'closed':
            self.ready = False
            self.start_btn.config(state='disabled')

    def tick(self):
        if self.closing:
            return
        self.worker.guard.heartbeat = time.monotonic()
        try:
            for _ in range(300):
                try:
                    kind, data = self.events.get_nowait()
                except queue.Empty:
                    break
                self.event(kind, data)
        except BaseException:
            self.on_error(*sys.exc_info())
        self.root.after(16, self.tick)

    def on_error(self, exc_type, exc, tb):
        self.release()  # native thread releases even if disk or rendering has failed
        traceback.print_exception(exc_type, exc, tb)
        if self.session:
            try:
                self.session.close('界面或保存失败；本段数据可能不完整：' + str(exc))
            except BaseException:
                traceback.print_exc()
        if not self.closing:
            self.status.config(text='界面 / 保存异常，已请求释放鼠标；本段数据可能不完整：' + str(exc))

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.worker:
            self.worker.stopping.set()
            self.worker.join(timeout=3)
        # Preserve even a start/frame/stop sequence still waiting in the UI queue.
        try:
            while not self.events.empty():
                kind, data = self.events.get_nowait()
                if kind in ('started', 'frame', 'stopped'):
                    try:
                        self.event(kind, data)
                    except BaseException:
                        self.on_error(*sys.exc_info())
        finally:
            try:
                if self.session:
                    self.session.close('窗口关闭')
            except BaseException:
                traceback.print_exc()
            finally:
                self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true', help='non-activating replay smoke test; no capture hooks')
    args = parser.parse_args()
    try:
        C.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    pad_info.configure()
    root = tk.Tk()
    app = MotionWindow(root, safe_preview=args.self_check)
    if args.self_check:
        # Imported only for verification; normal startup has no Pillow dependency.
        from test_live_window import visual_check
        root.after(1000, lambda: visual_check(app))
    try:
        root.mainloop()
        if getattr(app, 'self_check_failed', False):
            raise RuntimeError('Safe visual verification failed')
    finally:
        if not app.closing:
            app.close()


if __name__ == '__main__':
    main()
