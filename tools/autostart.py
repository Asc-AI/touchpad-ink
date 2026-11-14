"""Opt-in start at sign-in through the per-user Run key.

Everything lives under ``HKEY_CURRENT_USER``, so no administrator rights and no
scheduled task are needed. The stored command points at the executable that is
running right now: the frozen ``TouchpadInk.exe``, or ``pythonw.exe`` plus
``tools/tray_app.py`` when running from source. ``refresh()`` rewrites the value
when the program was moved or updated, so an old path never breaks sign-in.

All helpers are quiet on failure: a locked-down registry must not stop the tray
from starting. Callers only see False / None and can log it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import app_paths

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'TouchpadInk'


def _reg():
    """Import winreg lazily so tests can substitute a fake module."""
    import winreg
    return winreg


def command():
    """Command line Windows should run at sign-in."""
    if getattr(sys, 'frozen', False):
        return '"%s"' % Path(sys.executable).resolve()
    exe = Path(sys.executable).resolve()
    windowless = exe.with_name('pythonw.exe')
    if windowless.exists():
        exe = windowless
    script = app_paths.app_dir() / 'tools' / 'tray_app.py'
    return '"%s" "%s"' % (exe, script)


def _query():
    """Stored command, or None when the value is absent."""
    winreg = _reg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    text = str(value or '').strip()
    return text or None


def enabled():
    return _query() is not None


def stored_command():
    return _query()


def enable():
    winreg = _reg()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        return True
    except OSError:
        return False


def disable():
    winreg = _reg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def toggle():
    """Flip the setting; the new state, or None when the registry refused."""
    if enabled():
        return False if disable() else None
    return True if enable() else None


def refresh():
    """Repoint an enabled entry at this build; True when it had to change."""
    if not enabled():
        return False
    wanted = command()
    if stored_command() == wanted:
        return False
    return enable()
