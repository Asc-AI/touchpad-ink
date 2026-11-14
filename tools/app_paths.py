"""Directory layout that also works from a PyInstaller build.

Source runs write next to the project (``touchpad-ink/data``).  A frozen
executable must instead write next to itself: in one-file builds ``__file__``
points into the temporary extraction directory, which disappears on exit.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir():
    path = app_dir() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path
