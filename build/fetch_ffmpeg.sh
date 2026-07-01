#!/usr/bin/env bash
# Downloads a static ffmpeg and places it in vendor/ so the PyInstaller build
# bundles it *inside* the ProctorClient binary — no external dependency to
# install on the machines that run it. Idempotent.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p vendor

if [ -f vendor/ffmpeg ]; then
    echo "[ffmpeg] already present at vendor/ffmpeg - skipping download"
    exit 0
fi

os="$(uname -s)"
tmp="$(mktemp -d)"

if [ "$os" = "Linux" ]; then
    echo "[ffmpeg] downloading static Linux ffmpeg..."
    curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
        -o "$tmp/ffmpeg.tar.xz"
    tar -xf "$tmp/ffmpeg.tar.xz" -C "$tmp"
    cp "$tmp"/ffmpeg-*-static/ffmpeg vendor/ffmpeg
elif [ "$os" = "Darwin" ]; then
    echo "[ffmpeg] downloading static macOS ffmpeg..."
    curl -L https://evermeet.cx/ffmpeg/getrelease/zip -o "$tmp/ffmpeg.zip"
    unzip -o "$tmp/ffmpeg.zip" -d "$tmp"
    cp "$tmp/ffmpeg" vendor/ffmpeg
else
    echo "[ffmpeg] unsupported OS: $os" >&2
    exit 1
fi

chmod +x vendor/ffmpeg
rm -rf "$tmp"
echo "[ffmpeg] placed -> vendor/ffmpeg"
