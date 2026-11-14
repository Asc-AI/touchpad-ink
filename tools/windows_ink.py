"""Windows Ink handwriting recognition adapter.

Uses the WinRT InkRecognizerContainer directly. No handwriting panel, no
synthetic mouse/pen input, and no screen-coordinate conversion: strokes stay
in touchpad coordinates.

Recognitions run on one long-lived thread that keeps one
``InkRecognizerContainer``. Creating a container per call is not safe here:
on Windows 11 / Python 3.13 / winrt 3.x it segfaults the whole process after
about ten recognitions (verified 2026-11-13), which is the "crash after a
lot of writing" report. One cached recognizer survived a 200-call stress.
"""
from __future__ import annotations

import asyncio
import queue
import threading

DEFAULT_MAX_CANDIDATES = 5
WORKER_TIMEOUT = 20.0


class RecognitionUnavailable(RuntimeError):
    pass


def is_available():
    try:
        import winrt.windows.ui.input.inking  # noqa: F401
    except Exception:
        return False
    return True


def recognize(strokes, max_candidates=DEFAULT_MAX_CANDIDATES):
    """Recognize one or more strokes.

    strokes: iterable of strokes, each an iterable of (x, y) touchpad points.
    Returns:
      {'results': [{'candidates': [...], 'top': '...'}],
       'text': '...', 'candidates': [...]}
    'candidates' belongs to the first result; 'text' concatenates the top
    candidate of every result returned by the engine.
    """
    clean = []
    for stroke in strokes:
        points = [(float(p[0]), float(p[1])) for p in stroke]
        if points:  # a single-point dot is valid ink
            clean.append(points)
    if not clean:
        raise ValueError('empty stroke set')
    worker = _worker_instance()
    reply = queue.Queue(1)
    worker.requests.put((clean, max_candidates, reply))
    try:
        ok, value = reply.get(timeout=WORKER_TIMEOUT)
    except queue.Empty:
        raise RecognitionUnavailable('Windows Ink 识别超时') from None
    if not ok:
        if isinstance(value, RecognitionUnavailable):
            raise value
        raise RecognitionUnavailable('Windows Ink 识别失败：' + str(value))
    return value


class _Worker(threading.Thread):
    """Serializes recognition and owns the event loop and recognizer."""

    def __init__(self):
        super().__init__(name='Windows Ink recognition', daemon=True)
        self.requests = queue.Queue()
        self.startup = threading.Event()
        self.error = None

    def run(self):
        loop = None
        recognizer = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from winrt.windows.ui.input.inking import InkRecognizerContainer
            recognizer = InkRecognizerContainer()
        except BaseException as exc:
            self.error = exc
        finally:
            self.startup.set()
        while True:
            item = self.requests.get()
            if item is None:
                break
            strokes, max_candidates, reply = item
            try:
                if self.error is not None:
                    raise self.error
                result = loop.run_until_complete(
                    _recognize(strokes, max_candidates, recognizer))
                reply.put((True, result))
            except BaseException as exc:
                reply.put((False, exc))
        if loop is not None:
            try:
                loop.close()
            except BaseException:
                pass


_worker = None
_worker_lock = threading.Lock()


def _worker_instance():
    """Start the single recognition thread on first use."""
    global _worker
    with _worker_lock:
        if _worker is None:
            worker = _Worker()
            worker.start()
            worker.startup.wait(10)
            _worker = worker
        return _worker


def _stop_worker():
    """Stop and forget the worker; tests only (the app keeps it for life)."""
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.requests.put(None)
        worker.join(timeout=2)


async def _recognize(strokes, max_candidates, recognizer=None):
    from winrt.windows.foundation import Point
    from winrt.windows.ui.input.inking import (
        InkRecognitionTarget,
        InkRecognizerContainer,
        InkStrokeBuilder,
        InkStrokeContainer,
    )

    builder = InkStrokeBuilder()
    container = InkStrokeContainer()
    for stroke in strokes:
        container.add_stroke(
            builder.create_stroke([Point(x, y) for x, y in stroke])
        )

    if recognizer is None:  # direct/self-check use; the app always caches one
        recognizer = InkRecognizerContainer()
    try:
        operation = recognizer.recognize_async(container, InkRecognitionTarget.ALL)
        raw_results = await operation
    except Exception as exc:
        raise RecognitionUnavailable('Windows Ink 识别失败：' + str(exc)) from exc

    results = []
    for raw in raw_results:
        candidates = list(raw.get_text_candidates())[:max_candidates]
        if candidates:
            results.append({'candidates': candidates, 'top': candidates[0]})
    if not results:
        return {'results': [], 'text': '', 'candidates': []}
    return {
        'results': results,
        'text': ''.join(result['top'] for result in results),
        'candidates': results[0]['candidates'],
    }
