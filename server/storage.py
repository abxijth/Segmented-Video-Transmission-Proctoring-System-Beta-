"""Chunk storage + metadata — the only module that touches the filesystem.

Layout (per the roadmap):

    storage/<exam>/<student>/
        chunks/chunk_000000.mp4 ...
        recording.mp4
        metadata.json

Swap this module out (e.g. for S3) without changing the API or the client.
All metadata access goes through here so the JSON schema stays consistent.
"""

from __future__ import annotations

import json
import os
import tempfile

from shared.protocol import (
    chunk_filename,
    new_metadata,
    sequence_from_filename,
)


class SessionStore:
    """File operations scoped to a single (exam, student) recording session."""

    def __init__(self, storage_root: str, exam_id: str, student_id: str):
        self._base = os.path.join(storage_root, exam_id, student_id)
        self._chunks_dir = os.path.join(self._base, "chunks")
        self._metadata_path = os.path.join(self._base, "metadata.json")
        self.exam_id = exam_id
        self.student_id = student_id
        os.makedirs(self._chunks_dir, exist_ok=True)

    # --- chunks -------------------------------------------------------------
    @property
    def chunks_dir(self) -> str:
        return self._chunks_dir

    @property
    def recording_path(self) -> str:
        return os.path.join(self._base, "recording.mp4")

    def chunk_path(self, sequence: int) -> str:
        return os.path.join(self._chunks_dir, chunk_filename(sequence))

    def has_chunk(self, sequence: int) -> bool:
        return os.path.exists(self.chunk_path(sequence))

    def save_chunk(self, sequence: int, data: bytes) -> None:
        """Atomically write a chunk (temp file + rename)."""
        final = self.chunk_path(sequence)
        fd, tmp = tempfile.mkstemp(dir=self._chunks_dir, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def stored_sequences(self) -> list[int]:
        """All stored chunk sequence numbers, ascending."""
        seqs: list[int] = []
        for name in os.listdir(self._chunks_dir):
            try:
                seqs.append(sequence_from_filename(name))
            except ValueError:
                continue
        seqs.sort()
        return seqs

    def contiguous_after(self, last_merged: int) -> list[int]:
        """Stored chunks forming an unbroken run starting at last_merged + 1.

        Chunks already folded into recording.mp4 are deleted, so the merge
        worker only ever needs the next contiguous batch. If the chunk right
        after last_merged is missing (a gap not yet filled), returns [].
        """
        stored = set(self.stored_sequences())
        run: list[int] = []
        seq = last_merged + 1
        while seq in stored:
            run.append(seq)
            seq += 1
        return run

    def delete_chunks(self, sequences: list[int]) -> None:
        """Remove chunk files after they have been merged into recording.mp4."""
        for seq in sequences:
            try:
                os.remove(self.chunk_path(seq))
            except FileNotFoundError:
                pass

    def recording_exists(self) -> bool:
        return os.path.exists(self.recording_path)

    # --- metadata -----------------------------------------------------------
    def load_metadata(self) -> dict | None:
        if not os.path.exists(self._metadata_path):
            return None
        with open(self._metadata_path) as fh:
            return json.load(fh)

    def init_metadata_if_absent(self, codec: str, resolution: str,
                                fps: int) -> dict:
        meta = self.load_metadata()
        if meta is None:
            meta = new_metadata(self.student_id, self.exam_id, codec,
                                resolution, fps)
            self.save_metadata(meta)
        return meta

    def save_metadata(self, meta: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._base, suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(meta, fh, indent=2)
            os.replace(tmp, self._metadata_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def list_sessions(storage_root: str) -> list[tuple[str, str]]:
    """Every (exam_id, student_id) currently on disk."""
    sessions: list[tuple[str, str]] = []
    if not os.path.isdir(storage_root):
        return sessions
    for exam in sorted(os.listdir(storage_root)):
        exam_dir = os.path.join(storage_root, exam)
        if not os.path.isdir(exam_dir):
            continue
        for student in sorted(os.listdir(exam_dir)):
            if os.path.isdir(os.path.join(exam_dir, student)):
                sessions.append((exam, student))
    return sessions
