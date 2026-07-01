"""Client configuration.

All tunables live here so the rest of the client reads cleanly and there are
no magic numbers buried in the logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.protocol import DEFAULT_AUTH_TOKEN

# Baked-in default server. Change this one line and rebuild to ship an exe that
# already points at your machine — no flags, env, or proctor.ini needed.
# Can still be overridden at runtime (CLI > env PROCTOR_SERVER > proctor.ini).
DEFAULT_SERVER_URL = os.environ.get(
    "PROCTOR_SERVER", "http://10.47.244.1:8000")


@dataclass
class ClientConfig:
    # --- Session identity ---
    server_url: str = DEFAULT_SERVER_URL   # e.g. "http://192.168.1.50:8000"
    exam_id: str = "exam2026"
    student_id: str = "student001"
    auth_token: str = os.environ.get("PROCTOR_TOKEN", DEFAULT_AUTH_TOKEN)

    # --- Camera ---
    camera_index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    fps: int = 30

    # --- Chunking / encoding ---
    chunk_seconds: float = 5.0
    # Chunks are H.264 (required by the server's scalable MPEG-TS merge),
    # encoded via ffmpeg. Leave ffmpeg_bin empty to auto-discover ffmpeg
    # (bundled next to the exe, or on PATH); set to force a specific binary.
    ffmpeg_bin: str = os.environ.get("PROCTOR_FFMPEG", "")

    # --- Local disk queue ---
    # Chunks are written here and only deleted after the server ACKs them, so
    # a network outage just means the queue grows until connectivity returns.
    queue_dir: str = os.environ.get("PROCTOR_QUEUE", "./client_queue")

    # --- Upload / retry ---
    request_timeout: float = 30.0
    retry_base_delay: float = 1.0    # seconds; exponential backoff base
    retry_max_delay: float = 30.0    # cap between retries

    @property
    def resolution(self) -> str:
        return f"{self.frame_width}x{self.frame_height}"

    @property
    def codec_name(self) -> str:
        return "H264"

    @property
    def session_queue_dir(self) -> str:
        """Per-session subfolder so concurrent sessions never collide."""
        return os.path.join(self.queue_dir, self.exam_id, self.student_id)
