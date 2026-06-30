"""Client configuration.

All tunables live here so the rest of the client reads cleanly and there are
no magic numbers buried in the logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.protocol import DEFAULT_AUTH_TOKEN


@dataclass
class ClientConfig:
    # --- Session identity ---
    server_url: str                 # e.g. "http://192.168.1.50:8000"
    exam_id: str
    student_id: str
    auth_token: str = os.environ.get("PROCTOR_TOKEN", DEFAULT_AUTH_TOKEN)

    # --- Camera ---
    camera_index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    fps: int = 30

    # --- Chunking ---
    chunk_seconds: float = 5.0
    # FourCC for OpenCV's MP4 writer:
    #   "mp4v" -> MPEG-4 Part 2: most portable, works everywhere out of the box.
    #   "avc1" -> H.264: requires an OpenCV build with H.264 support.
    # The roadmap targets H.264; flip this once your environment supports it.
    chunk_fourcc: str = os.environ.get("PROCTOR_FOURCC", "mp4v")

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
        return "H264" if self.chunk_fourcc in ("avc1", "h264", "H264") else "MPEG4"

    @property
    def session_queue_dir(self) -> str:
        """Per-session subfolder so concurrent sessions never collide."""
        return os.path.join(self.queue_dir, self.exam_id, self.student_id)
