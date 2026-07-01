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

echo
echo "[build] DONE -> dist/ProctorClient  (ffmpeg is bundled inside; no extra setup)"
echo "Copy proctor.ini.example to proctor.ini next to the binary to preset settings."
