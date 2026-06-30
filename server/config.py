"""Server configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.protocol import DEFAULT_AUTH_TOKEN


@dataclass
class ServerConfig:
    host: str = os.environ.get("PROCTOR_HOST", "0.0.0.0")
    port: int = int(os.environ.get("PROCTOR_PORT", "8000"))
    # Root of the on-disk layout described in the roadmap:
    #   storage/<exam>/<student>/{chunks/, recording.mp4, metadata.json}
    storage_root: str = os.environ.get("PROCTOR_STORAGE", "./storage")
    auth_token: str = os.environ.get("PROCTOR_TOKEN", DEFAULT_AUTH_TOKEN)

    # --- background merge worker ---
    # How often the worker merges new chunks into recording.mp4 and deletes
    # them. Lower = recording.mp4 stays fresher; higher = less ffmpeg churn.
    merge_interval: float = float(os.environ.get("PROCTOR_MERGE_INTERVAL", "10"))
    # If a session receives no new chunk for this long, treat the client as
    # disconnected: flush remaining chunks and mark the recording Finalized.
    stale_after: float = float(os.environ.get("PROCTOR_STALE_AFTER", "60"))
