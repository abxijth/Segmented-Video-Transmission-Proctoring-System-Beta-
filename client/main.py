"""Client entry point — wires the pipeline together and runs it.

    camera ──► recorder ──► disk queue ──► uploader ──► server

The recorder and uploader each run on their own thread; the main thread just
waits for Ctrl+C, then finalizes (drains the queue) before exiting.

Settings are resolved from, in priority order:
  1. command-line flags         (--server / --exam / --student / ...)
  2. environment variables      (PROCTOR_SERVER / PROCTOR_EXAM / PROCTOR_STUDENT)
  3. a `proctor.ini` file        next to the executable (or current dir)
  4. interactive prompts         (so the packaged .exe works on double-click)

Usage (from source):
    python -m client.main --server http://192.168.1.50:8000 \
                          --exam exam2026 --student student001
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
import time

from client.camera import CameraManager
from client.config import DEFAULT_SERVER_URL, ClientConfig
from client.disk_queue import DiskQueue
from client.recorder import ChunkRecorder
from client.uploader import UploadManager


# --- settings resolution ----------------------------------------------------
def _app_dir() -> str:
    """Directory to look beside for proctor.ini / write the queue into.

    When frozen by PyInstaller this is the folder containing the .exe; from
    source it is the current working directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _load_ini() -> dict:
    path = os.path.join(_app_dir(), "proctor.ini")
    if not os.path.exists(path):
        return {}
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section("client"):
        return {}
    return dict(parser["client"])


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default


def resolve_config(argv: list[str] | None = None) -> ClientConfig:
    parser = argparse.ArgumentParser(description="SEB webcam proctoring client")
    parser.add_argument("--server", help="server base URL, e.g. http://192.168.1.50:8000")
    parser.add_argument("--exam", help="exam id")
    parser.add_argument("--student", help="student id")
    parser.add_argument("--camera", type=int, help="camera index (default 0)")
    parser.add_argument("--chunk-seconds", type=float, help="chunk length (default 5)")
    args = parser.parse_args(argv)

    ini = _load_ini()

    def pick(cli, env, key):
        return cli or os.environ.get(env) or ini.get(key)

    server = pick(args.server, "PROCTOR_SERVER", "server")
    exam = pick(args.exam, "PROCTOR_EXAM", "exam")
    student = pick(args.student, "PROCTOR_STUDENT", "student")

    # Server has a baked-in default (see client/config.py), so it never needs a
    # prompt. Exam/student fall back to prompts only if still unset.
    if not server:
        server = DEFAULT_SERVER_URL
    if not exam:
        exam = _prompt("Exam ID", "exam2026")
    if not student:
        student = _prompt("Student ID")

    camera = args.camera if args.camera is not None else int(ini.get("camera", 0))
    chunk_seconds = (args.chunk_seconds if args.chunk_seconds is not None
                     else float(ini.get("chunk_seconds", 5.0)))

    return ClientConfig(
        server_url=server,
        exam_id=exam,
        student_id=student,
        camera_index=camera,
        chunk_seconds=chunk_seconds,
        # Keep the queue beside the app so a packaged exe writes somewhere sane.
        queue_dir=os.environ.get(
            "PROCTOR_QUEUE", os.path.join(_app_dir(), "client_queue")),
    )


# --- run loop ---------------------------------------------------------------
def run(config: ClientConfig) -> int:
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


def main() -> int:
    try:
        config = resolve_config()
    except (KeyboardInterrupt, EOFError):
        print("\n[client] cancelled.")
        return 1

    try:
        return run(config)
    except RuntimeError as exc:
        # e.g. camera could not be opened — show it and (for double-click) hold.
        print(f"\n[client] error: {exc}")
        if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
            input("Press Enter to close...")
        return 1


if __name__ == "__main__":
    sys.exit(main())
