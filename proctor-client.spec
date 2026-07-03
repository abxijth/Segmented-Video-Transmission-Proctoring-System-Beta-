# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the proctoring client.

Bundles the Python runtime and every dependency (OpenCV, NumPy, requests, plus
vendored ffmpeg). Output per OS:
  * Windows / Linux -> a single self-contained executable (ProctorClient[.exe])
  * macOS           -> a onedir ProctorClient.app bundle (NOT onefile — see the
                       macOS section below for why: onefile breaks Apple-Silicon
                       library validation)
The SAME spec works on all three — run it on each OS. PyInstaller does NOT
cross-compile.

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

if sys.platform == "darwin":
    # --- macOS: onedir + .app bundle -----------------------------------------
    # A onefile build seals Python.framework (signed by python.org) inside the
    # archive; at launch it's extracted and loaded into our ad-hoc-signed
    # bootloader, and Apple-Silicon *library validation* refuses the cross-Team
    # load ("different Team IDs"). onedir puts every dylib/framework on disk as a
    # real file, so a single deep ad-hoc re-sign (see the CI / build_macos.sh
    # `codesign --deep` step) gives the whole bundle ONE consistent signature and
    # the mismatch disappears. Do NOT switch this back to onefile.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,          # onedir: binaries go into COLLECT below
        name="ProctorClient",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        console=True,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity="-",          # ad-hoc; CI re-signs the .app --deep
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="ProctorClient",
    )
    # The Info.plist camera/mic keys are REQUIRED or macOS TCC denies capture.
    app = BUNDLE(
        coll,
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
else:
    # --- Windows / Linux: single self-contained executable -------------------
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
        console=True,      # show the status console; students see it working
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
