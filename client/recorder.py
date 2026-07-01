"""Chunk Recorder — capture frames and write them as rotating H.264 chunks.

Runs in its own thread. Every `chunk_seconds` it closes the current MP4 and
opens the next one, so the disk queue fills with independently-playable
~5-second clips numbered in sequence.

Frames are captured with OpenCV but encoded to H.264 by piping them into an
ffmpeg subprocess (see client/media.py) — H.264 is required by the server's
scalable MPEG-TS merge, and OpenCV cannot encode it.

Write-then-rename: each chunk is written to a staging name (`.chunk_NNNNNN.mp4`)
and renamed to its final `chunk_NNNNNN.mp4` only when complete, so the uploader
can never pick up a half-written file.
"""

from __future__ import annotations

import os
import threading
import time

from client.camera import CameraManager
from client.config import ClientConfig
from client.disk_queue import DiskQueue
from client.media import (
    FfmpegChunkWriter,
    detect_h264_encoder,
    find_ffmpeg,
)


class ChunkRecorder:
    def __init__(self, config: ClientConfig, camera: CameraManager,
                 queue: DiskQueue):
        self._config = config
        self._camera = camera
        self._queue = queue

        self._ffmpeg_bin = find_ffmpeg(config.ffmpeg_bin)
        if self._ffmpeg_bin is None:
            raise RuntimeError(
                "ffmpeg was not found. It is required to encode H.264 chunks. "
                "Install ffmpeg and put it on PATH, set PROCTOR_FFMPEG to its "
                "path, or place ffmpeg next to the executable.")

        # Pick whichever H.264 encoder this ffmpeg build actually ships. Doing
        # it once here fails fast (before recording) on builds that have no
        # H.264 encoder at all, instead of on the first chunk.
        self._encoder = detect_h264_encoder(self._ffmpeg_bin)
        if self._encoder is None:
            raise RuntimeError(
                "this ffmpeg has no H.264 encoder (looked for libx264, "
                "libopenh264, and hardware encoders). On Fedora install the "
                "full build: `sudo dnf install openh264 ffmpeg-free` (or swap "
                "to RPM Fusion's ffmpeg). Elsewhere install an ffmpeg with "
                "libx264.")
        print(f"[recorder] using H.264 encoder: {self._encoder}")

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recorder",
                                         daemon=True)
        # Resume after any chunks a previous (possibly crashed) session left in
        # the queue, so we never overwrite not-yet-uploaded chunks.
        leftover = queue.pending()
        self._next_sequence = (leftover[-1][0] + 1) if leftover else 0

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    @property
    def chunks_recorded(self) -> int:
        return self._next_sequence

    # --- capture loop -------------------------------------------------------
    def _run(self) -> None:
        width, height = self._camera.actual_resolution

        while not self._stop.is_set():
            sequence = self._next_sequence
            self._record_one_chunk(sequence, width, height)
            self._next_sequence += 1

    def _record_one_chunk(self, sequence: int, width: int, height: int) -> None:
        """Record one chunk at a constant, wall-clock-accurate frame rate.

        Real-time correctness: a chunk is tagged at `config.fps`, so to play
        back at normal speed it must contain exactly `fps * chunk_seconds`
        frames. Webcams rarely deliver frames at a perfectly steady rate, so we
        pace by the wall clock — at any moment we have written
        `floor(elapsed * fps)` frames, duplicating the most recent frame when
        the camera is running behind. This keeps playback duration equal to
        real elapsed time instead of speeding it up.
        """
        final_path = self._queue.path_for(sequence)
        part_path = self._queue.staging_path_for(sequence)
        fps = self._config.fps

        writer = FfmpegChunkWriter(part_path, width, height, fps,
                                   self._ffmpeg_bin, self._encoder)

        target_total = int(round(self._config.chunk_seconds * fps))
        start = time.monotonic()
        frames_written = 0
        latest_frame = None
        try:
            while frames_written < target_total and not self._stop.is_set():
                frame = self._camera.read_frame()
                if frame is not None:
                    latest_frame = frame
                if latest_frame is None:
                    continue  # no frame yet; wait for the first one

                # Catch up to where the wall clock says we should be, capped at
                # this chunk's total so we never overrun into the next second.
                elapsed = time.monotonic() - start
                due = min(int(elapsed * fps), target_total)
                while frames_written < due:
                    writer.write(latest_frame)
                    frames_written += 1

                time.sleep(0.002)  # yield; avoids a busy-spin between frames
        finally:
            writer.close()

        # Publish the chunk to the queue only if it actually has content.
        if frames_written > 0:
            os.replace(part_path, final_path)
        else:
            _silently_remove(part_path)


def _silently_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
