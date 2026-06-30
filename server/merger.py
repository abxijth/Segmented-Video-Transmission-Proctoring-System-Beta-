"""Merge Service — folds stored chunks into a single recording.mp4.

Uses ffmpeg's concat demuxer with stream copy (`-c copy`): no re-encoding, so
it is fast and lossless. The server never decodes H.264 during normal
operation, exactly as the roadmap specifies — it just stitches containers.

Strategy (prototype): rebuild recording.mp4 from the contiguous run of chunks
0..N each time. Rebuilding is idempotent and robust; an incremental append is
a later optimization. Original chunks are kept until you choose to archive
them.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from server.storage import SessionStore


class MergeError(RuntimeError):
    pass


def merge_recording(store: SessionStore) -> int:
    """(Re)build recording.mp4 from contiguous chunks.

    Returns the highest sequence number now included (-1 if nothing to merge).
    """
    sequences = store.contiguous_sequences()
    if not sequences:
        return -1

    concat_list = _write_concat_list(store, sequences)
    try:
        _run_ffmpeg_concat(concat_list, store.recording_path)
    finally:
        os.remove(concat_list)

    return sequences[-1]


def _write_concat_list(store: SessionStore, sequences: list[int]) -> str:
    """Write the ffmpeg concat demuxer input file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        for seq in sequences:
            # ffmpeg concat needs absolute, single-quote-escaped paths.
            abs_path = os.path.abspath(store.chunk_path(seq))
            safe = abs_path.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    return path


def _run_ffmpeg_concat(concat_list: str, output_path: str) -> None:
    # Write to a temp file in the same dir, then rename, so a reader never sees
    # a half-rebuilt recording.mp4.
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
