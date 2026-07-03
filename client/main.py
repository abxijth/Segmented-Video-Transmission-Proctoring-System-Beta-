"""Client entry point — wires the pipeline together and runs it.

    camera ──► recorder ──► disk queue ──► uploader ──► server

The recorder and uploader each run on their own thread; the main thread just
waits for Ctrl+C, then finalizes (drains the queue) before exiting.

Settings are resolved from, in priority order (nothing ever prompts, so the
packaged exe runs fully hands-off on a double-click or when SEB launches it):
  1. command-line flags         (--server / --exam / --student / ...)
  2. environment variables      (PROCTOR_SERVER / PROCTOR_EXAM / PROCTOR_STUDENT)
  3. a `proctor.ini` file        next to the executable (or current dir)
  4. baked defaults              (server in client/config.py; exam "exam2026";
                                  student = the machine hostname)

Usage (from source):
    python -m client.main --server http://192.168.1.50:8000 \
                          --exam exam2026 --student student001
"""

from __future__ import annotations

import argparse
import configparser
import os
import socket
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

    When frozen by PyInstaller this is the folder containing the executable;
    from source it is the current working directory. On macOS the executable
    lives deep inside `ProctorClient.app/Contents/MacOS/ProctorClient`, so we
    climb out of the bundle to the folder that CONTAINS the .app — that's where
    a `proctor.ini` sits beside the app icon and where the queue can actually be
    written (writing inside the bundle is hidden and often read-only).
    """
    if not getattr(sys, "frozen", False):
        return os.getcwd()
    exe_dir = os.path.dirname(sys.executable)
    if sys.platform == "darwin" and ".app/Contents/MacOS" in sys.executable:
        # .../X.app/Contents/MacOS -> .../  (the folder holding X.app)
        return os.path.dirname(os.path.dirname(os.path.dirname(exe_dir)))
    return exe_dir


def _load_ini() -> dict:
    path = os.path.join(_app_dir(), "proctor.ini")
    if not os.path.exists(path):
        return {}
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section("client"):
        return {}
    return dict(parser["client"])


def _default_student_id() -> str:
    """Fallback identity when none is configured: the machine's hostname.

    Keeps double-click / SEB-launched runs completely hands-off — the client
    never has to stop and ask, and never crashes on a missing student id.
    Sanitized so it is safe inside a URL path and a folder name.
    """
    host = socket.gethostname() or "student"
    safe = "".join(c for c in host if c.isalnum() or c in "-_")
    return safe or "student"


def resolve_config(argv: list[str] | None = None) -> ClientConfig:
    parser = argparse.ArgumentParser(description="SEB webcam proctoring client")
    parser.add_argument("--server", help="server base URL, e.g. http://192.168.1.50:8000")
    parser.add_argument("--exam", help="exam id")
    parser.add_argument("--student", help="student id")
    parser.add_argument("--camera", type=int, help="camera index (default 0)")
    parser.add_argument("--chunk-seconds", type=float, help="chunk length (default 5)")
    parser.add_argument("--no-audio", action="store_true",
                        help="record video only (skip microphone capture)")
    parser.add_argument("--audio-device",
                        help="mic device override (dshow name / avfoundation "
                             "index / pulse source)")
    args = parser.parse_args(argv)

    ini = _load_ini()

    def pick(cli, env, key):
        return cli or os.environ.get(env) or ini.get(key)

    # Fully hands-off resolution so the exe "just runs" on double-click:
    #   CLI flag > env var > proctor.ini (next to the exe) > baked default.
    # Nothing here ever prompts or blocks.
    server = pick(args.server, "PROCTOR_SERVER", "server") or DEFAULT_SERVER_URL
    exam = pick(args.exam, "PROCTOR_EXAM", "exam") or "exam2026"
    student = (pick(args.student, "PROCTOR_STUDENT", "student")
               or _default_student_id())

    camera = args.camera if args.camera is not None else int(ini.get("camera", 0))
    chunk_seconds = (args.chunk_seconds if args.chunk_seconds is not None
                     else float(ini.get("chunk_seconds", 5.0)))

    # Audio on by default; --no-audio, PROCTOR_AUDIO=0, or `audio = 0` in the
    # ini turns it off. Same CLI > env > ini > default precedence as the rest.
    if args.no_audio:
        audio_enabled = False
    else:
        env_audio = os.environ.get("PROCTOR_AUDIO")
        ini_audio = ini.get("audio")
        audio_enabled = (env_audio if env_audio is not None
                         else ini_audio if ini_audio is not None
                         else "1") not in ("0", "false", "no", "off")
    audio_device = (args.audio_device
                    or os.environ.get("PROCTOR_AUDIO_DEVICE")
                    or ini.get("audio_device", ""))

    return ClientConfig(
        server_url=server,
        exam_id=exam,
        student_id=student,
        camera_index=camera,
        chunk_seconds=chunk_seconds,
        audio_enabled=audio_enabled,
        audio_device=audio_device,
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
