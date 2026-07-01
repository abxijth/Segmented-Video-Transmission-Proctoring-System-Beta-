"""Background Merge Worker — server-side, client-independent.

A daemon thread that, on a fixed interval, walks every session on disk and:

  1. appends each session's next contiguous batch of chunks into recording.mp4,
  2. deletes those chunks once they are safely merged, and
  3. auto-finalizes a session whose client has stopped uploading (disconnected),
     flushing any remaining chunks first.

This means a recording is produced and chunks are reclaimed even if the client
never calls /finalize — a crash or a dropped network on the student side no
longer leaves chunks stranded on the server.

The merge logic (`merge_pending`, `finalize_session`) is also called directly
by the /finalize endpoint. All of it assumes the caller holds the session lock.
"""

from __future__ import annotations

import threading
import time

from server.config import ServerConfig
from server.locks import session_lock
from server.merger import append_chunks_to_ts
from server.storage import SessionStore, list_sessions
from shared.protocol import STATUS_FINALIZED, STATUS_RECORDING


# --- merge primitives (caller must hold the session lock) -------------------
def merge_pending(store: SessionStore) -> dict | None:
    """Merge the next contiguous batch of chunks and delete them.

    Returns the updated metadata, or None if the session has no metadata yet.
    """
    meta = store.load_metadata()
    if meta is None:
        return None

    batch = store.contiguous_after(meta.get("lastMerged", -1))
    if batch:
        append_chunks_to_ts(store, batch)
        store.delete_chunks(batch)
        meta["lastMerged"] = batch[-1]
        meta["expectedChunk"] = batch[-1] + 1
        store.save_metadata(meta)
    return meta


def finalize_session(store: SessionStore) -> dict | None:
    """Flush every remaining chunk into recording.mp4 and mark Finalized.

    Used on graceful finalize and on stale auto-finalize. Unlike the periodic
    pass this merges across gaps (skipping chunks that never arrived), because a
    disconnected client will never fill them — better a continuous recording
    than chunks stranded forever.
    """
    meta = store.load_metadata()
    if meta is None:
        return None

    remaining = store.stored_sequences()
    if remaining:
        append_chunks_to_ts(store, remaining)
        store.delete_chunks(remaining)
        meta["lastMerged"] = max(meta.get("lastMerged", -1), remaining[-1])
        meta["lastReceived"] = max(meta.get("lastReceived", -1), remaining[-1])
        meta["expectedChunk"] = meta["lastMerged"] + 1
    meta["status"] = STATUS_FINALIZED
    store.save_metadata(meta)
    return meta


# --- the worker thread ------------------------------------------------------
class MergeWorker:
    def __init__(self, config: ServerConfig):
        self._config = config
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="merge-worker",
                                         daemon=True)

    def start(self) -> None:
        self._thread.start()
        print(f"[worker] started: merge every {self._config.merge_interval:g}s, "
              f"auto-finalize after {self._config.stale_after:g}s idle")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # stop.wait() doubles as the interval sleep; returns True when stopped.
        while not self._stop.wait(self._config.merge_interval):
            try:
                self._tick()
            except Exception as exc:  # never let one bad session kill the loop
                print(f"[worker] tick error: {exc}")

    def _tick(self) -> None:
        now = time.time()
        for exam_id, student_id in list_sessions(self._config.storage_root):
            with session_lock(exam_id, student_id):
                store = SessionStore(self._config.storage_root,
                                     exam_id, student_id)
                meta = merge_pending(store)
                if meta is None or meta["status"] != STATUS_RECORDING:
                    continue

                last_at = meta.get("lastChunkAt", 0.0)
                if last_at and (now - last_at) > self._config.stale_after:
                    finalize_session(store)
                    print(f"[worker] auto-finalized {exam_id}/{student_id} "
                          f"(idle {now - last_at:.0f}s)")
