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
from queue import Queue

from client.camera import CameraManager
from client.config import ClientConfig
from client.disk_queue import DiskQueue
from client.media import (
    AudioStreamCapture,
    FfmpegChunkWriter,
    detect_audio_input,
    detect_h264_encoder,
    find_ffmpeg,
    mux_pcm_into_chunk,
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

        # Resolve the microphone once, up front, so every chunk has the SAME
        # stream layout (the server's TS concat requires that). If audio is off,
        # there is no working mic, or permission is denied, we record video
        # only — recording is never blocked by audio.
        #
        # Audio is NOT muxed live into the chunk encoder anymore: on macOS the
        # rawvideo stdin pipe starved the AVFoundation reader and the mic
        # DROPPED most frames (chunks had sound only in their first/last
        # instants). Instead one dedicated ffmpeg captures a continuous PCM
        # stream for the whole session (lossless — a pipe can't drop), and the
        # finalizer muxes each chunk's exact slice in as AAC afterwards.
        self._audio: AudioStreamCapture | None = None
        if config.audio_enabled:
            # detect_audio_input() prints the authoritative [audio] status
            # (which device, measured level, or a silent-mic warning).
            audio_input = detect_audio_input(self._ffmpeg_bin,
                                             config.audio_device)
            if audio_input is None:
                print("[recorder] no usable microphone; recording video only")
            else:
                try:
                    self._audio = AudioStreamCapture(self._ffmpeg_bin,
                                                     audio_input)
                except OSError as exc:
                    print(f"[recorder] could not start audio capture ({exc}); "
                          "recording video only")

        # Self-heal: if muxing the audio slice into finished chunks keeps
        # failing, give up on audio and continue publishing video-only chunks.
        # (When only the MIC dies, AudioStreamCapture itself degrades to
        # silence and chunks keep their audio track / constant layout.)
        self._audio_failures = 0
        self._audio_failure_limit = 2  # ~two chunks (~10 s) before downgrading

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recorder",
                                         daemon=True)
        # Chunk finalization (ffmpeg flush + publish) runs on its own thread so
        # the capture loop never blocks between chunks — that gap is what made
        # playback jump at every chunk boundary once the mic was added, since
        # closing an audio+video ffmpeg (and its mic) takes noticeably longer.
        self._finalize_q: Queue = Queue()
        self._finalize_thread = threading.Thread(
            target=self._finalize_loop, name="finalizer", daemon=True)
        # Resume after any chunks a previous (possibly crashed) session left in
        # the queue, so we never overwrite not-yet-uploaded chunks.
        leftover = queue.pending()
        self._next_sequence = (leftover[-1][0] + 1) if leftover else 0

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._finalize_thread.start()
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        # Drain any chunks still being finalized before returning, so none is
        # lost — then let the finalizer thread exit.
        self._finalize_q.put(None)
        self._finalize_thread.join(timeout=timeout)
        if self._audio is not None:
            self._audio.close()

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

        # The live encode is now always video-only; the finalizer muxes this
        # chunk's audio slice in afterwards (see __init__ for why).
        writer = FfmpegChunkWriter(part_path, width, height, fps,
                                   self._ffmpeg_bin, self._encoder,
                                   audio_input=None)

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
            # Hand the finished writer to the finalizer thread and return
            # immediately, so the NEXT chunk starts capturing without waiting
            # for this ffmpeg (and its mic) to flush and close. That wait was
            # the dead time that made playback jump at each chunk boundary.
            self._finalize_q.put(
                (writer, part_path, final_path, frames_written, start))

    # --- background finalization --------------------------------------------
    def _finalize_loop(self) -> None:
        """Flush, add audio, and publish finished chunks in order.

        Runs off the capture path. Chunks arrive strictly in sequence, so
        consuming the continuous PCM stream here is naturally ordered: each
        chunk aligns the stream to its own start time (skipping the few ms
        lost between chunks) and takes exactly its video duration of audio.
        """
        while True:
            item = self._finalize_q.get()
            try:
                if item is None:
                    return
                writer, part_path, final_path, frames_written, start = item
                try:
                    writer.close()
                except RuntimeError as exc:
                    print(f"[recorder] chunk encode failed: {exc}")
                    _silently_remove(part_path)
                    continue
                if frames_written <= 0:
                    _silently_remove(part_path)
                    continue
                if self._audio is not None:
                    duration = frames_written / self._config.fps
                    self._audio.align_to(start)
                    pcm = self._audio.read_seconds(duration)
                    mux_path = part_path + ".audio.mp4"  # leading dot kept
                    try:
                        mux_pcm_into_chunk(self._ffmpeg_bin, part_path, pcm,
                                           mux_path)
                    except RuntimeError as exc:
                        print(f"[recorder] audio mux failed: {exc}")
                        _silently_remove(mux_path)
                        self._audio_failures += 1
                        if self._audio_failures >= self._audio_failure_limit:
                            self._audio.close()
                            self._audio = None
                            print("[recorder] audio muxing keeps failing; "
                                  "continuing with VIDEO ONLY for the rest "
                                  "of this session.")
                        # Publish the chunk video-only rather than lose it.
                        os.replace(part_path, final_path)
                        continue
                    self._audio_failures = 0
                    os.replace(mux_path, final_path)  # publish first...
                    _silently_remove(part_path)       # ...then drop staging
                else:
                    os.replace(part_path, final_path)
            finally:
                self._finalize_q.task_done()


def _silently_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
