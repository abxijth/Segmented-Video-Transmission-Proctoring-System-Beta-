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


# --- microphone / audio capture ---------------------------------------------
#
# We let ffmpeg pull audio straight from the OS capture API (no extra Python
# dependency), in the same subprocess that encodes the video chunk, and mux the
# two into one MP4. Each OS exposes the mic through a different ffmpeg input
# format:
#   Windows -> dshow        (needs the device *name*)
#   macOS   -> avfoundation (needs the device *index*, ":<n>" = audio only)
#   Linux   -> pulse        ("default" source, or a real input we pick)
# Detection is done once at startup and the captured LEVEL is measured (below),
# so a machine with no mic records video only, and a device that opens but
# yields digital silence (denied permission / muted / a monitor source) is
# caught instead of quietly recording a silent track.


def _default_dshow_audio_name(ffmpeg_bin: str) -> str | None:
    """First DirectShow audio device name on Windows, or None."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # ffmpeg prints the device list to stderr; audio lines end in "(audio)".
    for line in proc.stderr.splitlines():
        match = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if match:
            return match.group(1)
    return None


def _default_avfoundation_audio_index(ffmpeg_bin: str) -> str | None:
    """First AVFoundation audio device index on macOS, or None."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    in_audio_section = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if in_audio_section:
            match = re.search(r"\[(\d+)\]", line)
            if match:
                return match.group(1)
    return None


def _candidate_audio_input(ffmpeg_bin: str,
                           override: str | None) -> list[str] | None:
    """ffmpeg input args (format + device) for this OS's default mic, or None.

    `override` forces a specific device (name/index/source per OS). A large
    `-thread_queue_size` keeps the live capture from stalling while the video
    pipe is being written.
    """
    tq = ["-thread_queue_size", "1024"]
    if sys.platform == "darwin":
        index = override or _default_avfoundation_audio_index(ffmpeg_bin)
        if index is None:
            index = "0"  # the built-in mic is device 0 on most Macs
        return ["-f", "avfoundation", *tq, "-i", f":{index}"]
    if os.name == "nt":
        name = override or _default_dshow_audio_name(ffmpeg_bin)
        if not name:
            return None  # no dshow audio device -> video only
        return ["-f", "dshow", *tq, "-i", f"audio={name}"]
    # Linux / other: try PulseAudio's default source.
    return ["-f", "pulse", *tq, "-i", override or "default"]


# Peak level (dBFS) below this is treated as digital silence — i.e. the device
# opened but is capturing nothing (denied mic permission, a muted input, or the
# "default" source pointing at an output monitor). A live mic, even in a quiet
# room, sits well above this; pure silence reads around -84..-91 dB.
_SILENCE_DBFS = -80.0


def _measure_audio_dbfs(ffmpeg_bin: str, input_args: list[str],
                        seconds: float = 0.7) -> float | None:
    """Peak dBFS captured from `input_args` over `seconds`, or None if the
    device could not be opened / captured at all."""
    try:
        # No `-loglevel error` here: volumedetect reports at info level.
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", *input_args,
             "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    return float(match.group(1)) if match else None


def _pulse_input_sources() -> list[str]:
    """Real (non-monitor) PulseAudio/PipeWire capture sources, best-effort."""
    try:
        proc = subprocess.run(["pactl", "list", "sources", "short"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        # columns: index, name, driver, ... — skip .monitor (loopback of output)
        if len(parts) >= 2 and not parts[1].endswith(".monitor"):
            names.append(parts[1])
    return names


def _describe_audio_input(input_args: tuple[str, ...] | list[str]) -> str:
    """Human label for a mic-input arg list, e.g. 'pulse:default'."""
    fmt = input_args[input_args.index("-f") + 1] if "-f" in input_args else "?"
    dev = input_args[-1]
    return f"{fmt}:{dev}"


def _warn_silent_mic(input_args: list[str], level: float | None) -> None:
    lvl = "no signal" if level is None else f"peak {level:.0f} dBFS"
    print("\n" + "!" * 70)
    print(f"[audio] WARNING: microphone opened but is SILENT ({lvl}) — "
          f"{_describe_audio_input(input_args)}")
    if sys.platform == "darwin":
        print("[audio] Grant mic access: System Settings > Privacy & Security >")
        print("[audio] Microphone > enable this app, then relaunch. (Or the")
        print("[audio] wrong input is selected.)")
    elif os.name == "nt":
        print("[audio] Check: Settings > Privacy > Microphone > allow desktop")
        print("[audio] apps; unmute the mic; or pass --audio-device \"<name>\".")
    else:
        print("[audio] The default source may be a monitor or muted. Unmute it,")
        print("[audio] or pass --audio-device <source> (list: "
              "`pactl list sources short`).")
    print("[audio] Recording continues; fix and it starts capturing sound.")
    print("!" * 70 + "\n")


@lru_cache(maxsize=8)
def detect_audio_input(ffmpeg_bin: str,
                       override: str = "") -> tuple[str, ...] | None:
    """Return ffmpeg mic-input args if audio can be captured, else None.

    Beyond "does the device open", this MEASURES the captured level so a device
    that opens but yields digital silence (denied permission, muted input, or a
    default source pointing at an output monitor) is caught — the earlier
    open-only probe accepted those and recorded a silent track. On macOS the
    probe also triggers the one-time mic-permission prompt early. Cached per
    (ffmpeg, override).
    """
    candidate = _candidate_audio_input(ffmpeg_bin, override or None)
    if candidate is None:
        return None

    level = _measure_audio_dbfs(ffmpeg_bin, candidate)
    if level is None:
        return None  # could not open/capture at all -> record video only

    if level > _SILENCE_DBFS:
        print(f"[audio] microphone OK — {_describe_audio_input(candidate)} "
              f"(peak {level:.0f} dBFS)")
        return tuple(candidate)

    # Opened but silent. On Linux the culprit is usually the wrong default
    # source; try each real input and keep the first one that has signal.
    if not override and sys.platform != "darwin" and os.name != "nt":
        for src in _pulse_input_sources():
            if src == candidate[-1]:
                continue
            alt = ["-f", "pulse", "-thread_queue_size", "1024", "-i", src]
            alt_level = _measure_audio_dbfs(ffmpeg_bin, alt)
            if alt_level is not None and alt_level > _SILENCE_DBFS:
                print(f"[audio] default source was silent; using '{src}' "
                      f"(peak {alt_level:.0f} dBFS)")
                return tuple(alt)

    # No source with signal found — warn loudly but keep recording, so if the
    # user unmutes / grants permission mid-session sound starts flowing.
    _warn_silent_mic(candidate, level)
    return tuple(candidate)


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
    """Encodes raw BGR frames to one H.264 MP4 chunk via ffmpeg's stdin.

    If `audio_input` is given (ffmpeg mic-input args from `detect_audio_input`),
    the mic is opened as a second input and muxed as AAC into the same chunk;
    `-shortest` ends the chunk when the video (stdin) does. Without it the chunk
    is video-only (`-an`).
    """

    def __init__(self, path: str, width: int, height: int, fps: int,
                 ffmpeg_bin: str, encoder: str = "libx264",
                 audio_input: tuple[str, ...] | None = None):
        self._path = path
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y"]
        # Input 0: raw BGR video on stdin. A thread queue keeps it from stalling
        # the live audio capture while we write frames.
        if audio_input:
            cmd += ["-thread_queue_size", "1024"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", str(fps), "-i", "-"]
        # Input 1 (optional): the microphone.
        if audio_input:
            cmd += list(audio_input)
        # Video encode (encoder + flags chosen for this ffmpeg build).
        cmd += _encoder_output_args(encoder)
        # Audio: native AAC (always available, even on ffmpeg-free), or none.
        if audio_input:
            cmd += ["-c:a", "aac", "-b:a", "128k",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", path]
        self._proc = subprocess.Popen(
            cmd,
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
