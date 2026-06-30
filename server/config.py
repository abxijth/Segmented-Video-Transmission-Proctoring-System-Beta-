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
