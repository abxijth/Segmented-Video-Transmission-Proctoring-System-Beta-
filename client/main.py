"""Client entry point — wires the pipeline together and runs it.

    camera ──► recorder ──► disk queue ──► uploader ──► server

The recorder and uploader each run on their own thread; the main thread just
waits for Ctrl+C, then finalizes (drains the queue) before exiting.

Usage:
    python -m client.main --server http://192.168.1.50:8000 \
                          --exam exam2026 --student student001
"""

from __future__ import annotations

import argparse
import sys
import time

from client.camera import CameraManager
from client.config import ClientConfig
from client.disk_queue import DiskQueue
from client.recorder import ChunkRecorder
from client.uploader import UploadManager


def parse_args() -> ClientConfig:
    parser = argparse.ArgumentParser(description="SEB webcam proctoring client")
    parser.add_argument("--server", required=True,
                        help="server base URL, e.g. http://192.168.1.50:8000")
    parser.add_argument("--exam", required=True, help="exam id")
    parser.add_argument("--student", required=True, help="student id")
    parser.add_argument("--camera", type=int, default=0, help="camera index")
    parser.add_argument("--chunk-seconds", type=float, default=5.0)
    args = parser.parse_args()

    return ClientConfig(
        server_url=args.server,
        exam_id=args.exam,
        student_id=args.student,
        camera_index=args.camera,
        chunk_seconds=args.chunk_seconds,
    )


def main() -> int:
    config = parse_args()
    queue = DiskQueue(config.session_queue_dir)

    with CameraManager(config) as camera:
        width, height = camera.actual_resolution
        print(f"[client] camera open @ {width}x{height}, "
              f"chunking every {config.chunk_seconds:g}s "
              f"({config.codec_name})")
        print(f"[client] uploading to {config.server_url} "
              f"as {config.exam_id}/{config.student_id}")

        recorder = ChunkRecorder(config, camera, queue)
        uploader = UploadManager(config, queue)
        recorder.start()
        uploader.start()

        try:
            while True:
                time.sleep(2)
                print(f"[client] recorded={recorder.chunks_recorded} "
                      f"uploaded={uploader.chunks_uploaded} "
                      f"queued={len(queue.pending())}", end="\r")
        except KeyboardInterrupt:
            print("\n[client] stopping; finalizing recording...")

        recorder.stop()
        drained = uploader.drain_and_stop(timeout=120)

    if drained:
        print(f"[client] done. uploaded {uploader.chunks_uploaded} chunks.")
    else:
        print(f"[client] timed out draining queue; "
              f"{len(queue.pending())} chunk(s) remain in "
              f"{config.session_queue_dir} and will upload on next run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
