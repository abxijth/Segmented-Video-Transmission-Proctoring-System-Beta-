"""FastAPI server — receive chunks, store them, merge into recording.mp4.

Endpoints:
    POST /exams/{exam}/{student}/chunks          upload one chunk
    POST /exams/{exam}/{student}/finalize        mark recording complete
    GET  /exams/{exam}/{student}/status          metadata + counts
    GET  /exams/{exam}/{student}/recording.mp4   download merged recording
    GET  /exams                                  list all sessions
    GET  /health

Run:
    python -m server.main
"""

from __future__ import annotations

import threading
from collections import defaultdict

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    UploadFile,
    File,
)
from fastapi.responses import FileResponse, JSONResponse

from server.config import ServerConfig
from server.merger import MergeError, merge_recording
from server.storage import SessionStore, list_sessions
from shared.protocol import (
    AUTH_SCHEME,
    HEADER_SEQUENCE,
    STATUS_FINALIZED,
    UPLOAD_FILE_FIELD,
)

config = ServerConfig()
app = FastAPI(title="SEB Proctoring Server", version="0.1")

# One lock per (exam, student) so chunk-store + merge for a session is
# serialized, while different students upload fully in parallel.
_session_locks: defaultdict[tuple[str, str], threading.Lock] = defaultdict(
    threading.Lock)


# --- auth -------------------------------------------------------------------
def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"{AUTH_SCHEME} {config.auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")


def _store(exam_id: str, student_id: str) -> SessionStore:
    return SessionStore(config.storage_root, exam_id, student_id)


# --- upload -----------------------------------------------------------------
@app.post("/exams/{exam_id}/{student_id}/chunks",
          dependencies=[Depends(require_auth)])
async def upload_chunk(
    exam_id: str = Path(...),
    student_id: str = Path(...),
    chunk: UploadFile = File(..., alias=UPLOAD_FILE_FIELD),
    x_chunk_sequence: int = Header(..., alias=HEADER_SEQUENCE),
):
    if x_chunk_sequence < 0:
        raise HTTPException(status_code=400, detail="sequence must be >= 0")

    data = await chunk.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty chunk")

    store = _store(exam_id, student_id)

    # Serialize per session: store the chunk, then re-merge the contiguous run.
    with _session_locks[(exam_id, student_id)]:
        if store.has_chunk(x_chunk_sequence):
            # Idempotent retry — already have it. 409 tells the client to drop
            # its local copy without resending.
            return JSONResponse(status_code=409,
                                content={"status": "duplicate",
                                         "sequence": x_chunk_sequence})

        store.save_chunk(x_chunk_sequence, data)

        meta = store.init_metadata_if_absent(
            codec="H264", resolution="unknown", fps=0)
        stored = store.stored_sequences()
        meta["lastReceived"] = stored[-1] if stored else -1

        try:
            last_merged = merge_recording(store)
        except MergeError as exc:
            raise HTTPException(status_code=500,
                                detail=f"merge failed: {exc}")

        meta["lastMerged"] = last_merged
        meta["expectedChunk"] = last_merged + 1
        store.save_metadata(meta)

    return {"status": "stored", "sequence": x_chunk_sequence,
            "lastMerged": last_merged}


# --- finalize ---------------------------------------------------------------
@app.post("/exams/{exam_id}/{student_id}/finalize",
          dependencies=[Depends(require_auth)])
def finalize(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    meta = store.load_metadata()
    if meta is None:
        raise HTTPException(status_code=404, detail="no such session")

    with _session_locks[(exam_id, student_id)]:
        try:
            last_merged = merge_recording(store)
        except MergeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        meta["lastMerged"] = last_merged
        meta["expectedChunk"] = last_merged + 1
        meta["status"] = STATUS_FINALIZED
        store.save_metadata(meta)

    return {"status": STATUS_FINALIZED, "lastMerged": last_merged}


# --- read endpoints ---------------------------------------------------------
@app.get("/exams/{exam_id}/{student_id}/status")
def status(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    meta = store.load_metadata()
    if meta is None:
        raise HTTPException(status_code=404, detail="no such session")
    stored = store.stored_sequences()
    return {
        **meta,
        "storedChunks": len(stored),
        "contiguousChunks": len(store.contiguous_sequences()),
        "hasRecording": _recording_exists(store),
    }


@app.get("/exams/{exam_id}/{student_id}/recording.mp4")
def download_recording(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    if not _recording_exists(store):
        raise HTTPException(status_code=404, detail="recording not ready")
    return FileResponse(store.recording_path, media_type="video/mp4",
                        filename=f"{exam_id}_{student_id}.mp4")


@app.get("/exams")
def all_sessions():
    return {
        "sessions": [
            {"examId": exam, "studentId": student}
            for exam, student in list_sessions(config.storage_root)
        ]
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def _recording_exists(store: SessionStore) -> bool:
    import os
    return os.path.exists(store.recording_path)


def main() -> None:
    print(f"[server] storage at {config.storage_root}")
    print(f"[server] listening on http://{config.host}:{config.port}")
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
