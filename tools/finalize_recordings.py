#!/usr/bin/env python3
"""Post-exam finalizer — turn every recording.ts into a clean, playable MP4.

Run this once **after the exam**, on the server (or any machine with the
`storage/` folder), to produce a smooth `recording_final.mp4` per student.

Why re-encode instead of the fast `-c copy` the live server uses:

    recording.ts is built by byte-appending independently-encoded ~5s chunks,
    and every chunk starts at presentation timestamp (PTS) 0. So the timeline
    RESETS at each batch boundary. A stream-copy (`-c copy`) faithfully keeps
    those non-monotonic timestamps, which is what makes some players — phones
    especially — stutter or play too fast. Re-encoding with a REGENERATED,
    constant-frame-rate timeline (the setpts filter below) throws the broken
    input timestamps away and lays down a correct one, so the result plays at
    the right speed everywhere. Audio is re-timed with aresample to stay in sync.

Usage (from the repo root):

    python tools/finalize_recordings.py                 # whole ./storage tree
    python tools/finalize_recordings.py --storage /srv/storage --jobs 4
    python tools/finalize_recordings.py --exam exam2026 --student student001
    python tools/finalize_recordings.py --overwrite     # replace recording.mp4

Requires ffmpeg (auto-detected, same as the client) with an H.264 encoder.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys

# Make the repo's packages importable no matter where this is launched from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from client.media import detect_h264_encoder, find_ffmpeg  # noqa: E402
from server.storage import SessionStore, list_sessions      # noqa: E402


def _ffprobe_streams(ffprobe: str, path: str) -> list[dict]:
    """Return the stream list ffprobe reports for `path` (empty on error)."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        ).stdout
        return json.loads(out).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _duration(ffprobe: str, path: str) -> float:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, timeout=60,
        ).stdout
        return float(out.strip() or 0.0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def _video_output_args(encoder: str, crf: int, preset: str) -> list[str]:
    """Encoder-specific H.264 flags tuned for broad (incl. mobile) playback."""
    if encoder == "libx264":
        # high/4.0 + yuv420p is the most universally decodable H.264 profile.
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p"]
    # libopenh264 / hardware encoders take a bitrate, not CRF/preset.
    return ["-c:v", encoder, "-b:v", "2000k", "-pix_fmt", "yuv420p"]


def _build_cmd(ffmpeg: str, encoder: str, src: str, dst: str, *,
               fps: int, crf: int, preset: str, has_audio: bool) -> list[str]:
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        # Be tolerant of the discontinuous TS and fill any missing PTS.
        "-fflags", "+genpts", "-analyzeduration", "100M", "-probesize", "100M",
        "-i", src,
        # THE FIX: rewrite every frame's PTS from its frame index at a constant
        # rate, ignoring the broken input timestamps -> monotonic timeline.
        "-vf", f"setpts=N/({fps}*TB)",
        "-r", str(fps), "-fps_mode", "cfr",
        "-map", "0:v:0",
        *_video_output_args(encoder, crf, preset),
    ]
    if has_audio:
        cmd += [
            "-map", "0:a:0",
            # aresample keeps audio locked to the timeline across the resets.
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        ]
    cmd += ["-movflags", "+faststart", "-max_muxing_queue_size", "1024", dst]
    return cmd


def _finalize_one(ffmpeg: str, ffprobe: str, encoder: str,
                  store: SessionStore, out_name: str, *,
                  fps: int, crf: int, preset: str,
                  overwrite: bool, dry_run: bool) -> tuple[str, bool, str]:
    tag = f"{store.exam_id}/{store.student_id}"
    src = store.recording_ts_path
    if not os.path.exists(src):
        return tag, False, "no recording.ts"

    dst = (store.recording_path if overwrite
           else os.path.join(os.path.dirname(src), out_name))

    streams = _ffprobe_streams(ffprobe, src)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    cmd = _build_cmd(ffmpeg, encoder, src, dst, fps=fps, crf=crf,
                     preset=preset, has_audio=has_audio)
    if dry_run:
        return tag, True, "dry-run: " + " ".join(cmd)

    tmp = dst + ".finalizing.mp4"
    cmd[-1] = tmp
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return tag, False, f"ffmpeg failed: {result.stderr.strip()[:300]}"
    os.replace(tmp, dst)

    dur = _duration(ffprobe, dst)
    kind = "video+audio" if has_audio else "video-only"
    return tag, True, f"{kind}, {dur:.1f}s -> {os.path.basename(dst)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-encode every recording.ts into a smooth, playable MP4.")
    parser.add_argument("--storage",
                        default=os.environ.get("PROCTOR_STORAGE", "./storage"),
                        help="storage root (default ./storage or PROCTOR_STORAGE)")
    parser.add_argument("--exam", help="only this exam id")
    parser.add_argument("--student", help="only this student id (needs --exam)")
    parser.add_argument("--fps", type=int, default=30,
                        help="output constant frame rate (default 30)")
    parser.add_argument("--crf", type=int, default=23,
                        help="libx264 quality, lower=better/bigger (default 23)")
    parser.add_argument("--preset", default="medium",
                        help="libx264 speed/size preset (default medium)")
    parser.add_argument("--output", default="recording_final.mp4",
                        help="output filename per session (default "
                             "recording_final.mp4)")
    parser.add_argument("--overwrite", action="store_true",
                        help="write recording.mp4 in place instead of --output")
    parser.add_argument("--jobs", type=int, default=2,
                        help="parallel re-encodes (default 2; CPU-heavy)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ffmpeg commands, encode nothing")
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg(None)
    if ffmpeg is None:
        print("error: ffmpeg not found (install it or put it on PATH)")
        return 1
    ffprobe = _ffprobe_beside(ffmpeg)
    encoder = detect_h264_encoder(ffmpeg)
    if encoder is None:
        print("error: this ffmpeg has no H.264 encoder (need libx264 / "
              "libopenh264 / a hardware encoder)")
        return 1

    if args.student and not args.exam:
        parser.error("--student requires --exam")
    if args.exam:
        sessions = [(args.exam, args.student)] if args.student else [
            (e, s) for (e, s) in list_sessions(args.storage) if e == args.exam]
    else:
        sessions = list_sessions(args.storage)

    stores = [SessionStore(args.storage, e, s) for (e, s) in sessions]
    todo = [st for st in stores if os.path.exists(st.recording_ts_path)]
    if not todo:
        print(f"no recording.ts files found under {args.storage}")
        return 0

    print(f"[finalize] ffmpeg={ffmpeg} encoder={encoder} "
          f"sessions={len(todo)} jobs={args.jobs} fps={args.fps} "
          f"crf={args.crf}{' (DRY RUN)' if args.dry_run else ''}")

    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(_finalize_one, ffmpeg, ffprobe, encoder, st,
                        args.output, fps=args.fps, crf=args.crf,
                        preset=args.preset, overwrite=args.overwrite,
                        dry_run=args.dry_run)
            for st in todo
        ]
        for fut in concurrent.futures.as_completed(futures):
            tag, success, detail = fut.result()
            mark = "OK " if success else "FAIL"
            if success:
                ok += 1
            print(f"  [{mark}] {tag}: {detail}")

    print(f"[finalize] done: {ok}/{len(todo)} succeeded")
    return 0 if ok == len(todo) else 1


def _ffprobe_beside(ffmpeg: str) -> str:
    """Prefer an ffprobe next to ffmpeg; else assume it's on PATH."""
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    cand = os.path.join(os.path.dirname(ffmpeg), exe)
    return cand if os.path.isfile(cand) else exe


if __name__ == "__main__":
    sys.exit(main())
