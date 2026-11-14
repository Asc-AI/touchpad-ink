# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the resident tray app.

Build from the project root:
    python -m PyInstaller --noconfirm touchpad_tray.spec

Output: dist/TouchpadInk/TouchpadInk.exe plus its _internal folder.  Writable
state (logs, recordings, tray.ico) is created in dist/TouchpadInk/data at run
time, next to the executable, so copying the whole folder is enough.
"""
from pathlib import Path

ROOT = Path(SPECPATH)
ENTRY = ROOT / 'tools' / 'tray_exe.py'
ICON = ROOT / 'data' / 'tray.ico'

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT / 'tools')],
    binaries=[],
    datas=[],
    hiddenimports=[
        'winrt.windows.foundation',
        'winrt.windows.foundation.collections',
        'winrt.windows.storage.streams',
        'winrt.windows.ui.input.inking',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'PIL', 'unittest', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TouchpadInk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON) if ICON.exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='TouchpadInk',
)
