"""H.264 encoding via an ffmpeg subprocess.

OpenCV's bundled FFmpeg cannot *encode* H.264 (no libx264 for licensing
reasons), and the server's scalable MPEG-TS merge requires H.264. So we capture
frames with OpenCV but encode each chunk by piping raw BGR frames into ffmpeg,
which produces a proper H.264 MP4.

Not every ffmpeg build ships `libx264` — Fedora's default `ffmpeg-free`, for
example, omits it for patent reasons but includes Cisco's `libopenh264`. So we
detect at startup whichever H.264 encoder the local ffmpeg actually has, in
preference order, and build the encode command to match. Any of them produces
H.264 the server can stream-copy into its TS accumulator.

`ffmpeg` is located from (in order): an explicit override, a copy bundled next
to / inside the packaged executable, or the system PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache


def find_ffmpeg(override: str | None = None) -> str | None:
    """Return a usable ffmpeg path, or None if none is found."""
    candidates: list[str] = []
    if override:
        candidates.append(override)

    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    # Bundled inside a PyInstaller onefile build (extracted to _MEIPASS)...
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, exe))
    # ...or sitting next to the executable / this script.
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(base, exe))

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Finally, whatever is on PATH.
    return shutil.which("ffmpeg")


# H.264 encoders we know how to drive, best first. libx264 gives the best
# quality/CPU trade-off; libopenh264 is the common fallback on Fedora and other
# builds that omit libx264; the hardware encoders are last-resort.
_H264_ENCODERS = ("libx264", "libopenh264", "h264_v4l2m2m",
                  "h264_vaapi", "h264_nvenc", "h264_qsv")


@lru_cache(maxsize=8)
def detect_h264_encoder(ffmpeg_bin: str) -> str | None:
    """Return the best available H.264 encoder for this ffmpeg, or None.

    Cached per ffmpeg path so we only shell out to `ffmpeg -encoders` once.
    """
    try:
        out = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for encoder in _H264_ENCODERS:
        if re.search(rf"\b{re.escape(encoder)}\b", out):
            return encoder
    return None


def _encoder_output_args(encoder: str) -> list[str]:
    """ffmpeg output flags tuned per encoder (they take different options).

    Tuned for small files at 720p proctoring quality. libx264 uses CRF (quality-
    targeted, variable bitrate) which compresses far better than the old
    `ultrafast` default; `veryfast` still keeps CPU low on student laptops.
    """
    if encoder == "libx264":
        # CRF 28 = visually fine for a webcam feed, much smaller than CRF 23.
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p"]
    # libopenh264 / hardware encoders don't accept -preset/-crf; use a capped
    # average bitrate that's sane for 720p (well below the old 2500k).
    return ["-c:v", encoder, "-b:v", "1200k", "-maxrate", "1500k",
            "-bufsize", "3000k", "-pix_fmt", "yuv420p"]


class FfmpegChunkWriter:
    """Encodes raw BGR frames to one H.264 MP4 chunk via ffmpeg's stdin."""

    def __init__(self, path: str, width: int, height: int, fps: int,
                 ffmpeg_bin: str, encoder: str = "libx264"):
        self._path = path
        self._proc = subprocess.Popen(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                # raw input coming in on stdin
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                # H.264 output; encoder + its flags chosen for this ffmpeg build
                *_encoder_output_args(encoder),
                "-an", "-movflags", "+faststart",
                path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def write(self, frame_bgr) -> None:
        """Write one frame. Raises BrokenPipeError if ffmpeg has exited."""
        assert self._proc.stdin is not None
        self._proc.stdin.write(frame_bgr.tobytes())

    def close(self) -> None:
        """Flush and finish encoding. Raises RuntimeError if ffmpeg failed."""
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
        code = self._proc.wait()
        if code != 0:
            err = (self._proc.stderr.read().decode(errors="replace")
                   if self._proc.stderr else "")
            raise RuntimeError(f"ffmpeg encode failed (code {code}): {err.strip()}")
