"""Chunk Recorder — capture frames and write them as rotating MP4 chunks.

Runs in its own thread. Every `chunk_seconds` it closes the current MP4 and
opens the next one, so the disk queue fills with independently-playable
~5-second clips numbered in sequence.

Write-then-rename: each chunk is written to a staging name (`.chunk_NNNNNN.mp4`)
and renamed to its final `chunk_NNNNNN.mp4` only when complete, so the uploader
can never pick up a half-written file. The staging name still ends in `.mp4` so
OpenCV's VideoWriter selects the MP4 container correctly.
"""

from __future__ import annotations

import os
import threading
import time

import cv2

from client.camera import CameraManager
from client.config import ClientConfig
from client.disk_queue import DiskQueue


class ChunkRecorder:
    def __init__(self, config: ClientConfig, camera: CameraManager,
                 queue: DiskQueue):
        self._config = config
        self._camera = camera
        self._queue = queue

        self._fourcc = cv2.VideoWriter_fourcc(*config.chunk_fourcc)
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
        frame_interval = 1.0 / self._config.fps

        while not self._stop.is_set():
            sequence = self._next_sequence
            self._record_one_chunk(sequence, width, height, frame_interval)
            self._next_sequence += 1

    def _record_one_chunk(self, sequence: int, width: int, height: int,
                          frame_interval: float) -> None:
        final_path = self._queue.path_for(sequence)
        part_path = self._queue.staging_path_for(sequence)

        writer = cv2.VideoWriter(part_path, self._fourcc,
                                 self._config.fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError(
                f"VideoWriter could not open {part_path!r} with fourcc "
                f"{self._config.chunk_fourcc!r}. Your OpenCV build may not "
                f"support this codec — try 'mp4v' (default) or 'avc1' in "
                f"client/config.py.")

        deadline = time.monotonic() + self._config.chunk_seconds
        frames_written = 0
        try:
            while time.monotonic() < deadline and not self._stop.is_set():
                frame = self._camera.read_frame()
                if frame is None:
                    continue  # transient camera glitch; keep trying
                writer.write(frame)
                frames_written += 1
                time.sleep(frame_interval)
        finally:
            writer.release()

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
