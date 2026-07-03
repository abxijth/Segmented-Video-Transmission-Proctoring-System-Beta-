#!/usr/bin/env bash
# Build the ProctorClient binary on Linux (or macOS).
# Run from the repo root:  bash build/build_linux.sh
# Output: dist/ProctorClient  (single self-contained file)
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[build] creating virtual environment..."
python3 -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate

echo "[build] installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements-build.txt

echo "[build] fetching ffmpeg to bundle inside the binary..."
bash build/fetch_ffmpeg.sh

echo "[build] running PyInstaller..."
pyinstaller --clean --noconfirm proctor-client.spec

if [ "$(uname -s)" = "Darwin" ]; then
    # macOS builds a onedir .app. Deep ad-hoc re-sign so the bootloader and the
    # bundled python.org framework share ONE signature (fixes the Apple-Silicon
    # "different Team IDs" crash), then package with ditto (preserves framework
    # symlinks + exec bits). Ship/AirDrop the .zip; users unzip and run the .app.
    echo "[build] signing macOS .app (deep ad-hoc)..."
    codesign --force --deep --sign - dist/ProctorClient.app
    codesign --verify --deep --strict dist/ProctorClient.app
    ditto -c -k --keepParent dist/ProctorClient.app dist/ProctorClient-macos.zip
    echo
    echo "[build] DONE -> dist/ProctorClient.app  (+ dist/ProctorClient-macos.zip to distribute)"
else
    echo
    echo "[build] DONE -> dist/ProctorClient  (ffmpeg is bundled inside; no extra setup)"
fi
echo "Copy proctor.ini.example to proctor.ini next to the binary/app to preset settings."
