"""Merge Service — folds stored chunks into a single recording.mp4.

Uses ffmpeg's concat demuxer with stream copy (`-c copy`): no re-encoding, so
it is fast and lossless. The server never decodes H.264 during normal
operation, exactly as the roadmap specifies — it just stitches containers.

Strategy: **incremental append.** Each merge concatenates the existing
recording.mp4 (if any) with the next contiguous batch of chunks and replaces
recording.mp4. The merged chunks are then safe to delete, which keeps disk
usage bounded during long exams (unlike a full rebuild, which needs every
chunk to remain on disk forever).
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from server.storage import SessionStore


class MergeError(RuntimeError):
    pass


def append_to_recording(store: SessionStore, sequences: list[int]) -> None:
    """Append `sequences` (in order) to recording.mp4, creating it if needed.

    Caller must hold the session lock and pass a contiguous, in-order batch
    that immediately follows whatever is already in recording.mp4.
    """
    if not sequences:
        return

    inputs: list[str] = []
    if store.recording_exists():
        # Put the current recording first so the new chunks extend it.
        inputs.append(store.recording_path)
    inputs.extend(store.chunk_path(seq) for seq in sequences)

    concat_list = _write_concat_list(inputs)
    try:
        _run_ffmpeg_concat(concat_list, store.recording_path)
    finally:
        os.remove(concat_list)


def _write_concat_list(input_paths: list[str]) -> str:
    """Write the ffmpeg concat demuxer input file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        for input_path in input_paths:
            # ffmpeg concat needs absolute, single-quote-escaped paths.
            abs_path = os.path.abspath(input_path)
            safe = abs_path.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    return path


def _run_ffmpeg_concat(concat_list: str, output_path: str) -> None:
    # Write to a temp file in the same dir, then rename, so a reader never sees
    # a half-built recording.mp4 (and so an input == output is never clobbered).
    tmp_out = output_path + ".building.mp4"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy",
        tmp_out,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        raise MergeError(
            f"ffmpeg concat failed (code {result.returncode}): "
            f"{result.stderr.strip()}")
    os.replace(tmp_out, output_path)
