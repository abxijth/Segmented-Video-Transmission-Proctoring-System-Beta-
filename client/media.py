"""H.264 encoding via an ffmpeg subprocess.

OpenCV's bundled FFmpeg cannot *encode* H.264 (no libx264 for licensing
reasons), and the server's scalable MPEG-TS merge requires H.264. So we capture
frames with OpenCV but encode each chunk by piping raw BGR frames into ffmpeg,
which produces a proper H.264 MP4.

`ffmpeg` is located from (in order): an explicit override, a copy bundled next
to / inside the packaged executable, or the system PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


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


class FfmpegChunkWriter:
    """Encodes raw BGR frames to one H.264 MP4 chunk via ffmpeg's stdin."""

    def __init__(self, path: str, width: int, height: int, fps: int,
                 ffmpeg_bin: str):
        self._path = path
        self._proc = subprocess.Popen(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                # raw input coming in on stdin
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                # H.264 output; ultrafast keeps CPU low on student laptops
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-an",
                "-movflags", "+faststart",
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
