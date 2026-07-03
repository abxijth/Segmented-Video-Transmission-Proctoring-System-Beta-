# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the proctoring client.

Builds a single self-contained executable (Windows: ProctorClient.exe) that
bundles the Python runtime and every dependency (OpenCV, NumPy, requests).
The SAME spec works on Windows, Linux, and macOS — run it on each OS to get
that platform's executable. PyInstaller does NOT cross-compile.

    pyinstaller proctor-client.spec
"""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

# OpenCV ships compiled libraries and submodules PyInstaller must be told about.
hiddenimports = collect_submodules("cv2") + ["numpy"]
binaries = collect_dynamic_libs("cv2")

# Bundle ffmpeg (required to encode H.264 chunks) from vendor/. On Windows put
# vendor/ffmpeg.exe; on Linux/macOS vendor/ffmpeg. It lands at the bundle root,
# where client/media.py looks for it, so the shipped exe has NO external deps.
# The build scripts (build/fetch_ffmpeg.ps1 etc.) download it automatically.
_ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
_ffmpeg_vendor = os.path.join("vendor", _ffmpeg_name)
if os.path.isfile(_ffmpeg_vendor):
    binaries += [(_ffmpeg_vendor, ".")]
    print(f"[spec] bundling ffmpeg from {_ffmpeg_vendor}")
else:
    print("\n" + "!" * 70)
    print(f"[spec] WARNING: {_ffmpeg_vendor} not found - ffmpeg will NOT be")
    print("[spec] bundled. The exe will only work where ffmpeg is on PATH.")
    print("[spec] Run the build script (build/fetch_ffmpeg.ps1) to fetch it,")
    print("[spec] or place ffmpeg in vendor/ before building.")
    print("!" * 70 + "\n")

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
    # Ad-hoc sign on macOS ("-"). Apple Silicon KILLS unsigned binaries outright,
    # so this is required for the arm64 build to run at all. It is NOT Apple
    # notarization — see build/build_macos.sh / PACKAGING.md for the quarantine
    # story students still hit on first launch.
    codesign_identity="-" if sys.platform == "darwin" else None,
    entitlements_file=None,
)

# On macOS, wrap the executable in a proper .app bundle. The Info.plist below
# carries NSCameraUsageDescription — without it macOS's TCC denies camera access
# and every frame comes back empty. The bundle is what we ship inside the .dmg.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="ProctorClient.app",
        icon=None,
        bundle_identifier="org.amfoss.proctorclient",
        info_plist={
            "NSCameraUsageDescription":
                "Records webcam video for exam proctoring.",
            "NSMicrophoneUsageDescription":
                "Records microphone audio for exam proctoring.",
            "CFBundleShortVersionString": "0.1.1",
            "CFBundleVersion": "0.1.1",
            "LSMinimumSystemVersion": "11.0",
            # Keep it out of the Dock/App Switcher — it runs in the background
            # under Safe Exam Browser, not as a foreground app.
            "LSUIElement": True,
        },
    )
