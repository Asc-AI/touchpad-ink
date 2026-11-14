"""Frozen entry point for the tray build.

A ``--windowed`` PyInstaller executable has no console: ``sys.stdout`` and
``sys.stderr`` are ``None`` and ``print()`` raises.  Point both at
``data/tray_console.log`` before importing the app, so the existing status
messages still reach a file next to the executable.
"""
import sys


def _redirect_missing_streams():
    if sys.stdout is not None and sys.stderr is not None:
        return None
    from app_paths import data_dir
    handle = open(data_dir() / 'tray_console.log', 'a', encoding='utf-8',
                  buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle
    return handle


_LOG_HANDLE = _redirect_missing_streams()

if '--ink-check' in sys.argv:
    # Prove the bundled WinRT projection and recognizer work in the frozen app.
    import windows_ink
    try:
        outcome = windows_ink.recognize([[(10, 10), (60, 20), (100, 10)]])
        print('ink check: candidates=%s' % outcome['candidates'][:5])
        code = 0
    except BaseException as exc:
        print('ink check failed: %s' % exc)
        code = 1
    sys.exit(code)

import tray_app  # noqa: E402  (after the stream redirection)


if __name__ == '__main__':
    sys.exit(tray_app.main())
