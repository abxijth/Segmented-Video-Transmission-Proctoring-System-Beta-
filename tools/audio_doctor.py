"""audio_doctor — diagnose why microphone capture isn't working.

Run this on the STUDENT machine (the one recording) and share the output:

    python -m tools.audio_doctor
    # or:  python tools/audio_doctor.py

It reports the ffmpeg it will use, the microphones it can see, which device the
client would auto-pick, and whether a real capture produces sound — so a
"recording has no audio" problem can be pinned to a device, a permission, or the
code without guessing. It changes nothing and records nothing permanent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

# Make the client package importable whether run as -m tools.audio_doctor or as
# a plain script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.config import ClientConfig
from client.media import (
    _avfoundation_audio_devices,
    _candidate_audio_input,
    _measure_audio_dbfs,
    _parse_dshow_audio_names,
    _rank_avfoundation_device,
    detect_audio_input,
    find_ffmpeg,
)


def _hr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<failed to run {cmd[0]}: {exc}>"


def main() -> int:
    cfg = ClientConfig()

    _hr("1. Environment")
    print(f"platform      : {sys.platform}  (os.name={os.name})")
    ffmpeg = find_ffmpeg(cfg.ffmpeg_bin)
    print(f"ffmpeg path   : {ffmpeg or 'NOT FOUND'}")
    if not ffmpeg:
        print("\nffmpeg is required. Install it / put it on PATH, then re-run.")
        return 1
    ver = _run([ffmpeg, "-hide_banner", "-version"]).splitlines()
    print(f"ffmpeg version: {ver[0] if ver else '?'}")
    print(f"audio_enabled : {cfg.audio_enabled}   "
          f"audio_device override: {cfg.audio_device!r}")

    _hr("2. Microphones ffmpeg can see")
    if os.name == "nt":
        raw = _run([ffmpeg, "-hide_banner", "-list_devices", "true",
                    "-f", "dshow", "-i", "dummy"])
        print(raw.strip() or "<no output>")
        names = _parse_dshow_audio_names(raw)
        print(f"\nparsed audio device names: {names or 'NONE (this is the bug)'}")
    elif sys.platform == "darwin":
        raw = _run([ffmpeg, "-hide_banner", "-f", "avfoundation",
                    "-list_devices", "true", "-i", ""])
        print(raw.strip() or "<no output>")
        devices = _avfoundation_audio_devices(ffmpeg)
        print(f"\nparsed audio devices: {devices or 'NONE (this is the bug)'}")
        if devices:
            print("\nper-device live level (talk / play music while this runs):")
            for idx, name in devices:
                args = ["-f", "avfoundation", "-i", f":{idx}"]
                lvl, why = _measure_audio_dbfs(ffmpeg, args, seconds=1.5)
                rank = _rank_avfoundation_device(name)
                tag = ("likely-virtual" if rank == 3 else
                       "continuity" if rank == 2 else
                       "built-in" if rank == 0 else "external")
                if lvl is None:
                    print(f"  [{idx}] {name:<40} ({tag}) -> no reading ({why})")
                else:
                    verdict = "SOUND" if lvl > -80 else "SILENT"
                    print(f"  [{idx}] {name:<40} ({tag}) -> "
                          f"peak {lvl:.1f} dBFS  {verdict}")
    else:
        print(_run(["pactl", "list", "sources", "short"]).strip()
              or "<pactl not available; PulseAudio/PipeWire may be off>")

    _hr("3. Device the client would auto-pick")
    candidate = _candidate_audio_input(ffmpeg, cfg.audio_device or None)
    print(f"candidate input args : {candidate}")
    if candidate is None:
        print("=> No capture device -> client records VIDEO ONLY.")
        print("   Plug in/enable a mic, or pass --audio-device.")
        return 2

    _hr("4. Live capture test (does the mic actually produce sound?)")
    level, detail = _measure_audio_dbfs(ffmpeg, list(candidate), seconds=1.5)
    if level is None:
        print(f"could not capture: {detail}")
        print("=> The device is listed but capture failed. Usually a PERMISSION")
        if os.name == "nt":
            print("   issue: Settings > Privacy > Microphone > 'Let desktop")
            print("   apps access your microphone' = ON; unmute the input.")
        elif sys.platform == "darwin":
            print("   issue: System Settings > Privacy & Security > Microphone")
            print("   > enable your terminal / the app, then re-run.")
    else:
        verdict = "SOUND DETECTED" if level > -80 else "SILENT (near -inf dB)"
        print(f"measured peak level  : {level:.1f} dBFS  -> {verdict}")
        if level <= -80:
            print("=> Mic opens but captures silence (muted, wrong input, or a")
            print("   monitor source). Speak during the test, unmute, or pick a")
            print("   different --audio-device.")

    _hr("5. Full end-to-end: encode a 2s mic+tone clip and inspect it")
    out = os.path.join(tempfile.gettempdir(), "audio_doctor_test.mp4")
    # video = test pattern, audio = the real mic; mirrors the chunk writer.
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
           *candidate, "-t", "2",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0",
           "-shortest", out]
    err = _run(cmd)
    if not os.path.exists(out):
        print("encode FAILED:\n" + err.strip())
        return 3
    probe = _run([_probe_bin(ffmpeg), "-hide_banner", "-i", out])
    streams = [l.strip() for l in probe.splitlines() if "Stream #" in l]
    print("test clip:", out)
    for s in streams:
        print("  " + s)
    has_audio = any("Audio:" in s for s in streams)
    print(f"\n=> {'AUDIO TRACK PRESENT' if has_audio else 'NO AUDIO TRACK'} "
          f"in the test clip.")

    _hr("6. What detect_audio_input() decides (what the client uses)")
    result = detect_audio_input(ffmpeg, cfg.audio_device)
    print(f"\nRESULT: {'audio ENABLED -> ' + str(result) if result else 'VIDEO ONLY'}")
    return 0


def _probe_bin(ffmpeg: str) -> str:
    cand = ffmpeg.replace("ffmpeg", "ffprobe")
    return cand if os.path.isfile(cand) else "ffprobe"


if __name__ == "__main__":
    raise SystemExit(main())
