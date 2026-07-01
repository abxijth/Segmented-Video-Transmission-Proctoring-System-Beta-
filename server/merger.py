"""Merge Service — scalable, incremental, low-end-of-exam-load.

The problem with rebuilding recording.mp4 from all chunks (or re-muxing the
whole growing file each cycle) is O(n^2) I/O over a long exam: unusable at 300
students x 3 hours.

Strategy here: **append to an MPEG-TS accumulator.**

  During the exam   each new chunk is remuxed to MPEG-TS (stream copy, O(chunk))
                    and its bytes are appended to recording.ts. TS is designed
                    to concatenate by raw byte-append, so extending the
                    recording never re-copies what is already there. Work is
                    tiny and spread evenly across the whole exam.
  At finalize       nothing heavy — recording.ts is already complete. No spike
                    when 300 students stop at once.
  At download       recording.mp4 is produced on demand from recording.ts
                    (one stream-copy pass) and cached. This naturally staggers
                    the only O(total) step across whenever proctors review.

Requires H.264 chunks: MPEG-TS cannot carry MPEG-4 Part 2 (OpenCV 'mp4v').
The client encodes H.264 via ffmpeg for exactly this reason.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from server.storage import SessionStore


class MergeError(RuntimeError):
    pass


def append_chunks_to_ts(store: SessionStore, sequences: list[int]) -> None:
    """Append `sequences` (in order) to recording.ts. O(size of the batch).

    Caller holds the session lock and passes chunks in ascending order.
    """
    if not sequences:
        return

    concat_list = _write_concat_list(
        [store.chunk_path(seq) for seq in sequences])
    fd, batch_ts = tempfile.mkstemp(suffix=".ts")
    os.close(fd)
    try:
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", "-f", "mpegts", batch_ts,
        ], "remux batch to TS")
        # Byte-append the batch onto the accumulator — no re-copy of the whole
        # recording, which is what keeps this O(chunk) instead of O(total).
        with open(store.recording_ts_path, "ab") as dst, \
                open(batch_ts, "rb") as src:
            _copy_stream(src, dst)
    finally:
        os.remove(concat_list)
        if os.path.exists(batch_ts):
            os.remove(batch_ts)


def build_mp4_from_ts(store: SessionStore) -> None:
    """Produce recording.mp4 from recording.ts (one stream-copy pass).

    Called lazily on download, not during the exam, so its O(total) cost is
    spread across review time rather than spiking when everyone finishes.
    """
    if not store.recording_ts_exists():
        raise MergeError("no recording.ts to build from")

    tmp_out = store.recording_path + ".building.mp4"
    _run_ffmpeg([
        "-i", store.recording_ts_path,
        "-c", "copy", "-movflags", "+faststart", tmp_out,
    ], "build mp4 from TS")
    os.replace(tmp_out, store.recording_path)


# --- helpers ----------------------------------------------------------------
def _write_concat_list(input_paths: list[str]) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        for input_path in input_paths:
            safe = os.path.abspath(input_path).replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    return path


def _copy_stream(src, dst, bufsize: int = 1 << 20) -> None:
    while True:
        block = src.read(bufsize)
        if not block:
            return
        dst.write(block)


def _run_ffmpeg(args: list[str], what: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MergeError(f"ffmpeg failed to {what} "
                         f"(code {result.returncode}): {result.stderr.strip()}")
