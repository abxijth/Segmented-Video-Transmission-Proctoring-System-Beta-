"""Upload Manager — drains the disk queue to the server, with retries.

Runs in its own thread. Chunks are uploaded strictly in sequence order; a
chunk is deleted from disk only after the server ACKs it (HTTP 200/201).
Failures back off exponentially and retry forever, which gives the offline
recovery described in the roadmap: lose the network and chunks simply pile up
until it returns.
"""

from __future__ import annotations

import threading
import time

import requests

from client.config import ClientConfig
from client.disk_queue import DiskQueue
from shared.protocol import (
    AUTH_SCHEME,
    HEADER_AUTH,
    HEADER_SEQUENCE,
    UPLOAD_FILE_FIELD,
)


class UploadManager:
    def __init__(self, config: ClientConfig, queue: DiskQueue):
        self._config = config
        self._queue = queue
        self._endpoint = (
            f"{config.server_url.rstrip('/')}"
            f"/exams/{config.exam_id}/{config.student_id}/chunks"
        )
        self._headers = {HEADER_AUTH: f"{AUTH_SCHEME} {config.auth_token}"}
        if getattr(config, "custom_metadata", None):
            import json
            self._headers["X-Session-Metadata"] = json.dumps(config.custom_metadata)


        self._stop = threading.Event()
        self._drained = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uploader",
                                         daemon=True)
        self._uploaded = 0

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown but keep draining; see drain_and_stop()."""
        self._stop.set()

    def drain_and_stop(self, timeout: float | None = None) -> bool:
        """Block until the queue is empty (best-effort), then stop.

        Returns True if the queue fully drained, False on timeout.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._queue.is_empty():
            if deadline is not None and time.monotonic() > deadline:
                self._stop.set()
                self._thread.join(timeout=5.0)
                return False
            time.sleep(0.5)
        self._stop.set()
        self._thread.join(timeout=10.0)
        return True

    @property
    def chunks_uploaded(self) -> int:
        return self._uploaded

    # --- worker loop --------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.is_empty():
            pending = self._queue.pending()
            if not pending:
                time.sleep(0.5)
                continue

            for sequence, path in pending:
                if self._stop.is_set() and self._queue.is_empty():
                    return
                self._upload_with_retry(sequence, path)

    def _upload_with_retry(self, sequence: int, path: str) -> None:
        delay = self._config.retry_base_delay
        while not self._stop_drained():
            try:
                if self._upload_once(sequence, path):
                    self._queue.remove(path)
                    self._uploaded += 1
                    return
            except (requests.RequestException, OSError) as exc:
                print(f"[uploader] chunk {sequence} failed: {exc}; "
                      f"retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, self._config.retry_max_delay)

    def _upload_once(self, sequence: int, path: str) -> bool:
        with open(path, "rb") as fh:
            files = {UPLOAD_FILE_FIELD: (path, fh, "video/mp4")}
            headers = dict(self._headers)
            headers[HEADER_SEQUENCE] = str(sequence)
            resp = requests.post(self._endpoint, files=files, headers=headers,
                                 timeout=self._config.request_timeout)

        if resp.status_code in (200, 201):
            return True
        # 409 = server already has this chunk (e.g. duplicate after a retry);
        # treat as success so we stop resending it.
        if resp.status_code == 409:
            return True
        print(f"[uploader] chunk {sequence} rejected: HTTP {resp.status_code}")
        return False

    def _stop_drained(self) -> bool:
        return self._stop.is_set() and self._queue.is_empty()
