"""Disk Queue — the durable buffer between recording and uploading.

The recorder writes finished chunks here; the uploader drains them. Because
the queue *is* the filesystem, the design is crash- and outage-resilient for
free: anything not yet ACKed is still on disk after a restart.

Chunk filenames encode the sequence number, so ordering is just a sorted
directory listing.
"""

from __future__ import annotations

import os

from shared.protocol import chunk_filename, sequence_from_filename


class DiskQueue:
    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(self._dir, exist_ok=True)

    def path_for(self, sequence: int) -> str:
        return os.path.join(self._dir, chunk_filename(sequence))

    def staging_path_for(self, sequence: int) -> str:
        """Path the recorder writes to while a chunk is still in progress.

        It ends in `.mp4` so OpenCV's VideoWriter picks the MP4 container, but
        the leading dot means it does NOT start with `chunk_`, so `pending()`
        skips it and the uploader never grabs a half-written file. On
        completion the recorder renames it to `path_for(sequence)`.
        """
        return os.path.join(self._dir, "." + chunk_filename(sequence))

    def pending(self) -> list[tuple[int, str]]:
        """All queued chunks as (sequence, path), oldest sequence first.

        Files still being written by the recorder use a `.part` suffix and are
        ignored here, so the uploader never grabs a half-written chunk.
        """
        items: list[tuple[int, str]] = []
        for name in os.listdir(self._dir):
            try:
                seq = sequence_from_filename(name)
            except ValueError:
                continue  # .part files, stray files, etc.
            items.append((seq, os.path.join(self._dir, name)))
        items.sort(key=lambda pair: pair[0])
        return items

    def remove(self, path: str) -> None:
        """Delete a chunk after the server ACKs it. Idempotent."""
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def is_empty(self) -> bool:
        return not self.pending()
