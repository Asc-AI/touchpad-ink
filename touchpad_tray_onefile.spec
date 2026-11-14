# -*- mode: python ; coding: utf-8 -*-
"""Single-file variant: dist/TouchpadInk.exe.

Build from the project root:
    python -m PyInstaller --noconfirm touchpad_tray_onefile.spec

Startup is slower than the folder build because Windows extracts to a temp
directory, and unsigned one-file executables are more likely to trigger an
antivirus / SmartScreen prompt.  The folder build is the safer default.
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
    a.binaries,
    a.datas,
    [],
    name='TouchpadInk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON) if ICON.exists() else None,
)
