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
import threading
import time
from collections import deque
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


def _parse_dshow_audio_names(stderr: str) -> list[str]:
    """DirectShow audio device names from `ffmpeg -list_devices` stderr.

    Handles both output formats ffmpeg has shipped, because a student laptop may
    have either:

      ffmpeg >= 5.0   each device line is tagged inline:
                        [dshow @ ..] "Microphone (Realtek)" (audio)
      ffmpeg 4.x      devices are grouped under section headers, untagged:
                        [dshow @ ..] DirectShow audio devices
                        [dshow @ ..]  "Microphone (Realtek)"

    The 4.x format has no "(audio)" suffix, so the old suffix-only regex found
    nothing there and audio was silently dropped. We now track the section too.
    Returns friendly names in listing order (first = default).
    """
    names: list[str] = []
    section: str | None = None  # "audio" | "video" | None
    for raw in stderr.splitlines():
        # Strip the "[dshow @ 0x..] " log prefix so matching is simpler.
        line = re.sub(r"^\[dshow @ [^\]]*\]\s?", "", raw).rstrip()
        low = line.lower()

        # Section headers (ffmpeg 4.x groups devices under these).
        if "directshow audio devices" in low:
            section = "audio"
            continue
        if "directshow video devices" in low:
            section = "video"
            continue

        # Inline-tagged device line (ffmpeg >= 5.0).
        tagged = re.search(r'"([^"]+)"\s*\((audio|video)\)', line)
        if tagged:
            if tagged.group(2) == "audio":
                names.append(tagged.group(1))
            continue

        # "Alternative name" lines are @device_... aliases; ignore for naming.
        if "alternative name" in low:
            continue

        # Bare quoted name (ffmpeg 4.x) — classify by the current section.
        bare = re.match(r'\s*"([^"]+)"\s*$', line)
        if bare and section == "audio":
            names.append(bare.group(1))

    return names


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
    names = _parse_dshow_audio_names(proc.stderr)
    return names[0] if names else None


def _avfoundation_audio_devices(ffmpeg_bin: str) -> list[tuple[str, str]]:
    """All AVFoundation audio devices on macOS as (index, name) pairs."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[tuple[str, str]] = []
    in_audio_section = False
    for raw in proc.stderr.splitlines():
        # Strip the "[AVFoundation indev @ 0x..] " log prefix.
        line = re.sub(r"^\[[^\]]*\]\s?", "", raw).rstrip()
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if "AVFoundation video devices" in line:
            in_audio_section = False
            continue
        if in_audio_section:
            match = re.match(r"\s*\[(\d+)\]\s*(.+)$", line)
            if match:
                devices.append((match.group(1), match.group(2).strip()))
    return devices


# Devices that OPEN fine but capture pure digital silence. Virtual/loopback
# audio drivers (screen-share and routing tools) register as microphones and
# often grab index 0, which is exactly how a recording ends up with a silent
# AAC track while the "real" mic sits unused at index 1.
_VIRTUAL_MIC_HINTS = ("blackhole", "soundflower", "loopback", "aggregate",
                      "zoomaudiodevice", "teams", "vb-audio", "vb-cable",
                      "virtual", "ndi", "obs")
# The Mac's own microphone — the safest default when nothing has signal yet.
_BUILTIN_MIC_HINTS = ("macbook", "built-in", "builtin", "internal", "imac",
                      "mac mini", "mac studio")


def _rank_avfoundation_device(name: str) -> int:
    """Sort key: built-in mic first, known-silent virtual devices last."""
    low = name.lower()
    if any(h in low for h in _VIRTUAL_MIC_HINTS):
        return 3
    if "iphone" in low or "continuity" in low:
        return 2  # a real mic, but silent whenever the phone isn't active
    if any(h in low for h in _BUILTIN_MIC_HINTS):
        return 0
    return 1


def _default_avfoundation_audio_index(ffmpeg_bin: str) -> str | None:
    """Best-guess AVFoundation audio device index on macOS, or None.

    Previously this returned the FIRST listed device, which on many Macs is a
    virtual/loopback driver or an idle iPhone Continuity mic — both open
    successfully and record pure silence. Rank by name instead.
    """
    devices = _avfoundation_audio_devices(ffmpeg_bin)
    if not devices:
        return None
    return min(devices, key=lambda d: _rank_avfoundation_device(d[1]))[0]


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
                        seconds: float = 0.7) -> tuple[float | None, str]:
    """Measure the peak dBFS captured from `input_args` over `seconds`.

    Returns (level, detail). `level` is the peak in dBFS, or None if the device
    could not be opened / produced no measurable audio; `detail` is a short
    human reason (the tail of ffmpeg's error) used for diagnostics when it fails.
    """
    try:
        # No `-loglevel error` here: volumedetect reports at info level.
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", *input_args,
             "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run ffmpeg: {exc}"
    if proc.returncode != 0:
        # Surface the last meaningful ffmpeg error line (e.g. permission,
        # "I/O error", "Could not open device", "device busy").
        tail = next((ln.strip() for ln in reversed(proc.stderr.splitlines())
                     if ln.strip()), "")
        return None, tail or f"ffmpeg exited {proc.returncode}"
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    if match:
        return float(match.group(1)), ""
    return None, "opened but reported no audio level"


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


def _first_avfoundation_with_signal(ffmpeg_bin: str,
                                    skip_device: str) -> tuple[str, ...] | None:
    """First macOS audio device that measurably captures sound, or None.

    Devices are tried best-first (built-in mic before virtual drivers), the one
    already probed (`skip_device`, e.g. ':0') is skipped.
    """
    devices = sorted(_avfoundation_audio_devices(ffmpeg_bin),
                     key=lambda d: _rank_avfoundation_device(d[1]))
    for idx, name in devices:
        if f":{idx}" == skip_device:
            continue
        alt = ["-f", "avfoundation", "-thread_queue_size", "1024",
               "-i", f":{idx}"]
        level, _ = _measure_audio_dbfs(ffmpeg_bin, alt)
        if level is not None and level > _SILENCE_DBFS:
            print(f"[audio] device {skip_device!r} was silent; using "
                  f"[{idx}] {name} instead (peak {level:.0f} dBFS)")
            return tuple(alt)
    return None


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
        # No capture device at all for this OS.
        if os.name == "nt":
            print("[audio] no DirectShow microphone was found. Plug in / "
                  "enable a mic, or pass --audio-device \"<name>\" (list them "
                  "with: ffmpeg -list_devices true -f dshow -i dummy).")
        else:
            print("[audio] no microphone device found; recording video only")
        return None

    level, detail = _measure_audio_dbfs(ffmpeg_bin, candidate)
    if level is None:
        # The short probe couldn't get a reading. On Windows/macOS a *named*
        # device was still found, and the probe is easily defeated by things
        # that do NOT stop real capture: the device is slow to initialize, the
        # macOS permission prompt is still pending, or volumedetect dislikes the
        # default sample format. Disabling audio here is exactly the bug that
        # produced silent-video recordings, so instead we ATTACH the device and
        # let the actual per-chunk capture decide. The recorder self-heals to
        # video-only if the real capture also fails, so nothing is ever lost.
        if os.name == "nt" or sys.platform == "darwin":
            # On macOS, first check whether ANOTHER device has actual signal
            # (the picked one may be a virtual driver that can't be measured).
            if sys.platform == "darwin" and not override:
                alt = _first_avfoundation_with_signal(
                    ffmpeg_bin, skip_device=candidate[-1])
                if alt is not None:
                    return alt
            print(f"[audio] level probe inconclusive for "
                  f"{_describe_audio_input(candidate)} ({detail}); will still "
                  f"capture from it.")
            if sys.platform == "darwin":
                print("[audio] If the recording ends up silent, grant mic "
                      "access: System Settings > Privacy & Security > "
                      "Microphone > enable this app, then relaunch.")
            else:
                print("[audio] If it ends up silent, enable Settings > Privacy "
                      "> Microphone > 'Let desktop apps access your microphone', "
                      "or pass --audio-device \"<name>\".")
            return tuple(candidate)
        # Linux/other: pulse "default" is reported even when there is no real
        # mic, so a failed probe here genuinely means no usable input.
        print(f"[audio] microphone {_describe_audio_input(candidate)} could not "
              f"be captured: {detail}; recording video only.")
        return None

    if level > _SILENCE_DBFS:
        print(f"[audio] microphone OK — {_describe_audio_input(candidate)} "
              f"(peak {level:.0f} dBFS)")
        return tuple(candidate)

    # Opened but silent. On macOS the culprit is usually the wrong DEVICE:
    # virtual/loopback drivers and idle iPhone Continuity mics open fine but
    # deliver pure digital silence (-91 dB). Probe every other AVFoundation
    # audio device and keep the first one with real signal.
    if not override and sys.platform == "darwin":
        alt = _first_avfoundation_with_signal(ffmpeg_bin,
                                              skip_device=candidate[-1])
        if alt is not None:
            return alt

    # Opened but silent. On Linux the culprit is usually the wrong default
    # source; try each real input and keep the first one that has signal.
    if not override and sys.platform != "darwin" and os.name != "nt":
        for src in _pulse_input_sources():
            if src == candidate[-1]:
                continue
            alt = ["-f", "pulse", "-thread_queue_size", "1024", "-i", src]
            alt_level, _ = _measure_audio_dbfs(ffmpeg_bin, alt)
            if alt_level is not None and alt_level > _SILENCE_DBFS:
                print(f"[audio] default source was silent; using '{src}' "
                      f"(peak {alt_level:.0f} dBFS)")
                return tuple(alt)

    # No source with signal found — warn loudly but keep recording, so if the
    # user unmutes / grants permission mid-session sound starts flowing.
    _warn_silent_mic(candidate, level)
    return tuple(candidate)


# --- continuous audio capture + finalize-time muxing --------------------------
#
# Muxing the live mic directly into each chunk's encode process proved lossy on
# macOS: while ffmpeg drains the huge rawvideo stdin pipe (~80 MB/s) it services
# the AVFoundation reader too slowly, and the DEVICE silently discards mic
# frames it couldn't hand over. Real recordings came out with sound only in the
# first and last fraction of a second of every chunk — audio flowed only while
# the video pipe was idle. Re-opening the mic every 5 s for each chunk process
# made it worse.
#
# So audio is captured ONCE per session by a dedicated ffmpeg whose only job is
# to write raw PCM to a pipe. A pipe, unlike a capture device, never drops data
# (worst case it buffers), and nothing in that process can starve the reader.
# The recorder slices the continuous stream into exact per-chunk windows and
# the finalizer muxes each slice in as AAC after the video is encoded.

PCM_RATE = 48000          # samples/s, mono s16le => 96 KB/s
_PCM_FRAME = 2            # bytes per sample (s16le mono)


class AudioStreamCapture:
    """Continuous, lossless microphone capture as a raw s16le PCM stream.

    A reader thread drains the ffmpeg pipe into an in-memory buffer. The
    consumer (the chunk finalizer — single, sequential) calls `align_to(t)` to
    position the stream at a wall-clock instant and `read_seconds(d)` to take
    the next `d` seconds, zero-padded on underrun so a chunk ALWAYS gets a
    full-length audio slice (constant stream layout even if the mic dies).
    """

    _MAX_BUFFER_SECONDS = 300  # safety cap; the finalizer normally keeps up
    _MAX_RESTARTS = 3

    def __init__(self, ffmpeg_bin: str, input_args: tuple[str, ...]):
        self._ffmpeg_bin = ffmpeg_bin
        self._input_args = list(input_args)
        self._lock = threading.Condition()
        self._chunks: deque[bytes] = deque()
        self._buffered = 0            # bytes currently in _chunks
        self._consumed = 0            # bytes handed out / skipped so far
        self._received = 0            # total bytes ever received from ffmpeg
        self._epoch: float | None = None  # monotonic time of stream sample 0
        self._closing = False
        self._dead = False
        self._restarts = 0
        self._proc: subprocess.Popen | None = None
        self._start_proc()
        self._thread = threading.Thread(target=self._reader, name="audio-pcm",
                                        daemon=True)
        self._thread.start()

    # -- internals -------------------------------------------------------
    def _start_proc(self) -> None:
        cmd = [self._ffmpeg_bin, "-hide_banner", "-loglevel", "error",
               *self._input_args,
               "-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "pipe:1"]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

    def _reader(self) -> None:
        while True:
            proc = self._proc
            assert proc is not None and proc.stdout is not None
            data = proc.stdout.read(4096)
            now = time.monotonic()
            if data:
                with self._lock:
                    if self._epoch is None:
                        # First bytes: sample 0 was captured read-size ago.
                        self._epoch = now - len(data) / (PCM_RATE * _PCM_FRAME)
                    if (self._buffered
                            > self._MAX_BUFFER_SECONDS * PCM_RATE * _PCM_FRAME):
                        # Consumer is stuck; drop this block. It still counts
                        # toward _received so the timeline stays honest (the
                        # gap is skipped over by align_to, not replayed).
                        self._received += len(data)
                    else:
                        self._chunks.append(data)
                        self._buffered += len(data)
                        self._received += len(data)
                    self._lock.notify_all()
                continue
            # EOF: ffmpeg exited (device lost, killed, ...)
            with self._lock:
                if self._closing:
                    return
                self._restarts += 1
                if self._restarts > self._MAX_RESTARTS:
                    self._dead = True
                    self._lock.notify_all()
                    print("[audio] capture process died repeatedly; the rest "
                          "of the session will have SILENT audio (constant "
                          "layout is kept so chunks stay mergeable).")
                    return
            print("[audio] capture process exited; restarting mic capture "
                  f"({self._restarts}/{self._MAX_RESTARTS})")
            time.sleep(1.0)
            with self._lock:
                if self._closing:
                    return
                # Keep the timeline sample-continuous across the outage by
                # inserting silence for the time the process was down.
                if self._epoch is not None:
                    expect = int((time.monotonic() - self._epoch)
                                 * PCM_RATE) * _PCM_FRAME
                    gap = expect - self._received
                    if gap > 0:
                        pad = b"\x00" * gap
                        self._chunks.append(pad)
                        self._buffered += gap
                        self._received += gap
                        self._lock.notify_all()
            try:
                self._start_proc()
            except OSError:
                with self._lock:
                    self._dead = True
                    self._lock.notify_all()
                return

    def _take_locked(self, n: int) -> bytes:
        """Remove up to n buffered bytes (lock must be held)."""
        out = bytearray()
        while n > 0 and self._chunks:
            block = self._chunks.popleft()
            if len(block) > n:
                out += block[:n]
                self._chunks.appendleft(block[n:])
                self._buffered -= n
                n = 0
            else:
                out += block
                self._buffered -= len(block)
                n -= len(block)
        self._consumed += len(out)
        return bytes(out)

    # -- consumer API ------------------------------------------------------
    def align_to(self, t_monotonic: float, timeout: float = 2.0) -> None:
        """Advance the stream so the next byte corresponds to wall time `t`.

        Called by the finalizer at each chunk boundary; it skips the few ms of
        audio that fall between chunks (loop overhead) so audio can never
        drift behind video over a long session. Forward-only; a no-op if the
        stream is already at/past `t` or hasn't produced data yet.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._epoch is None and not self._dead and not self._closing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._lock.wait(left)
            if self._epoch is None:
                return
            target = int((t_monotonic - self._epoch) * PCM_RATE) * _PCM_FRAME
            skip = target - self._consumed
            while skip > 0:
                if self._buffered > 0:
                    skip -= len(self._take_locked(min(skip, self._buffered)))
                    continue
                if self._dead or self._closing:
                    self._consumed = target  # count it as skipped silence
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    self._consumed = target
                    return
                self._lock.wait(left)

    def read_seconds(self, seconds: float, timeout: float = 3.0) -> bytes:
        """Next `seconds` of PCM, exactly sized, zero-padded on underrun."""
        want = int(round(seconds * PCM_RATE)) * _PCM_FRAME
        deadline = time.monotonic() + timeout
        out = bytearray()
        with self._lock:
            while len(out) < want:
                if self._buffered > 0:
                    out += self._take_locked(want - len(out))
                    continue
                if self._dead or self._closing:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._lock.wait(left)
            short = want - len(out)
            if short > 0:
                self._consumed += short  # padded time counts as consumed
        if short > 0:
            out += b"\x00" * short
        return bytes(out)

    @property
    def healthy(self) -> bool:
        with self._lock:
            return not self._dead

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._lock.notify_all()
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self._thread.join(timeout=5)


def mux_pcm_into_chunk(ffmpeg_bin: str, video_path: str, pcm: bytes,
                       out_path: str) -> None:
    """Mux a raw PCM slice (s16le mono PCM_RATE) into a video chunk as AAC.

    Stream-copies the already-encoded H.264 video, so this is fast. Raises
    RuntimeError on failure (caller falls back to publishing video-only).
    """
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
           "-i", video_path,
           "-f", "s16le", "-ar", str(PCM_RATE), "-ac", "1", "-i", "pipe:0",
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", out_path]
    try:
        proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"audio mux failed to run: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"audio mux failed (code {proc.returncode}): {err}")


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
