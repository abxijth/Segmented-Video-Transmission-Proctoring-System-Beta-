"""Constants and helpers shared by the client and the server.

Keeping the wire format in one place means the client and server can never
drift apart on chunk naming, header names, or the metadata schema.
"""

from __future__ import annotations

# --- Chunk naming -----------------------------------------------------------
# Chunks are numbered from 0 and zero-padded so a plain lexicographic sort
# (on disk, in listings) is also the correct playback order.
CHUNK_PREFIX = "chunk_"
CHUNK_SUFFIX = ".mp4"
CHUNK_SEQ_WIDTH = 6  # supports ~1M chunks => ~58 days at 5s/chunk


def chunk_filename(sequence: int) -> str:
    """`5` -> `chunk_000005.mp4`."""
    return f"{CHUNK_PREFIX}{sequence:0{CHUNK_SEQ_WIDTH}d}{CHUNK_SUFFIX}"


def sequence_from_filename(name: str) -> int:
    """`chunk_000005.mp4` -> `5`. Raises ValueError on a malformed name."""
    if not (name.startswith(CHUNK_PREFIX) and name.endswith(CHUNK_SUFFIX)):
        raise ValueError(f"not a chunk filename: {name!r}")
    return int(name[len(CHUNK_PREFIX): -len(CHUNK_SUFFIX)])


# --- HTTP contract ----------------------------------------------------------
# Upload endpoint: POST /exams/{exam_id}/{student_id}/chunks
#   - multipart file field name below
#   - sequence + session metadata sent as headers (kept off the body so the
#     server can validate before reading the upload stream)
UPLOAD_FILE_FIELD = "chunk"
HEADER_SEQUENCE = "X-Chunk-Sequence"
HEADER_AUTH = "Authorization"          # value: "Bearer <token>"
AUTH_SCHEME = "Bearer"

# Shared secret for the prototype. In production this becomes a per-exam /
# per-student token issued at exam start. Override via env on both sides.
DEFAULT_AUTH_TOKEN = "prototype-shared-secret"


# --- Metadata schema (metadata.json) ---------------------------------------
STATUS_RECORDING = "Recording"
STATUS_FINALIZED = "Finalized"


def new_metadata(student_id: str, exam_id: str, codec: str,
                 resolution: str, fps: int) -> dict:
    """The initial metadata.json written when a student's first chunk lands."""
    return {
        "studentId": student_id,
        "examId": exam_id,
        "expectedChunk": 0,    # next contiguous sequence the server wants
        "lastReceived": -1,    # highest sequence stored (may be ahead of merge)
        "lastMerged": -1,      # highest sequence folded into recording.mp4
        "lastChunkAt": 0.0,    # epoch seconds of the last upload (staleness)
        "status": STATUS_RECORDING,
        "codec": codec,
        "resolution": resolution,
        "fps": fps,
    }
