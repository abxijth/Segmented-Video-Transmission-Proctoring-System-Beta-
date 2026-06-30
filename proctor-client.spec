# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the proctoring client.

Builds a single self-contained executable (Windows: ProctorClient.exe) that
bundles the Python runtime and every dependency (OpenCV, NumPy, requests).
The SAME spec works on Windows, Linux, and macOS — run it on each OS to get
that platform's executable. PyInstaller does NOT cross-compile.

    pyinstaller proctor-client.spec
"""

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

# OpenCV ships compiled libraries and submodules PyInstaller must be told about.
hiddenimports = collect_submodules("cv2") + ["numpy"]
binaries = collect_dynamic_libs("cv2")

a = Analysis(
    ["proctor_client.py"],
    pathex=["."],          # so `client` and `shared` packages are importable
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2"],  # trim size
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ProctorClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # show the status console; students see it working
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
