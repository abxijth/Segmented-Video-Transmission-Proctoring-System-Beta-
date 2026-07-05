"""FastAPI server — receive chunks; a background worker merges & cleans up.

Endpoints:
    POST /exams/{exam}/{student}/chunks          upload one chunk
    POST /exams/{exam}/{student}/finalize        flush + mark complete
    GET  /exams/{exam}/{student}/status          metadata + counts
    GET  /exams/{exam}/{student}/recording.mp4   download merged recording
    GET  /exams                                  list all sessions
    GET  /health

Uploads just store the chunk and return fast. The MergeWorker
(see server/worker.py) merges chunks into recording.mp4, deletes the merged
chunks, and auto-finalizes sessions whose client has disconnected — so the
server reaches a complete recording even if the client never finalizes.

Run:
    python -m server.main
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, File, Header, HTTPException, Path, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import os

from server.config import ServerConfig
from server.locks import session_lock
from server.merger import build_mp4_from_ts
from server.storage import SessionStore, list_sessions
from server.worker import MergeWorker, finalize_session
from shared.protocol import (
    AUTH_SCHEME,
    HEADER_SEQUENCE,
    STATUS_RECORDING,
    UPLOAD_FILE_FIELD,
)

config = ServerConfig()
worker = MergeWorker(config)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="SEB Proctoring Server", version="0.2", lifespan=lifespan)


# --- auth -------------------------------------------------------------------
def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"{AUTH_SCHEME} {config.auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")


def _store(exam_id: str, student_id: str) -> SessionStore:
    return SessionStore(config.storage_root, exam_id, student_id)


# --- upload -----------------------------------------------------------------
# Sync handler: FastAPI runs it in a threadpool, so the blocking disk write and
# the brief lock wait never stall the event loop or other students' uploads.
@app.post("/exams/{exam_id}/{student_id}/chunks",
          dependencies=[Depends(require_auth)])
def upload_chunk(
    exam_id: str = Path(...),
    student_id: str = Path(...),
    chunk: UploadFile = File(..., alias=UPLOAD_FILE_FIELD),
    x_chunk_sequence: int = Header(..., alias=HEADER_SEQUENCE),
    x_session_metadata: str | None = Header(default=None, alias="X-Session-Metadata"),
):
    if x_chunk_sequence < 0:
        raise HTTPException(status_code=400, detail="sequence must be >= 0")

    data = chunk.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty chunk")

    store = _store(exam_id, student_id)

    with session_lock(exam_id, student_id):
        meta = store.init_metadata_if_absent(
            codec="H264", resolution="unknown", fps=0)

        # Already merged (and deleted), or still pending: idempotent retry.
        # 409 tells the client to drop its local copy without resending.
        if x_chunk_sequence <= meta["lastMerged"] or \
                store.has_chunk(x_chunk_sequence):
            return JSONResponse(status_code=409,
                                content={"status": "duplicate",
                                         "sequence": x_chunk_sequence})

        # Merge custom metadata if provided
        if x_session_metadata:
            try:
                import json
                custom_meta = json.loads(x_session_metadata)
                if isinstance(custom_meta, dict):
                    meta.setdefault("custom", {}).update(custom_meta)
            except Exception as e:
                print(f"[server] Error parsing custom metadata header: {e}")

        store.save_chunk(x_chunk_sequence, data)
        meta["lastReceived"] = max(meta["lastReceived"], x_chunk_sequence)
        meta["lastChunkAt"] = time.time()
        meta["status"] = STATUS_RECORDING  # (re)activate; worker may finalize
        store.save_metadata(meta)

    return {"status": "stored", "sequence": x_chunk_sequence}


# --- finalize ---------------------------------------------------------------
@app.post("/exams/{exam_id}/{student_id}/finalize",
          dependencies=[Depends(require_auth)])
def finalize(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    if store.load_metadata() is None:
        raise HTTPException(status_code=404, detail="no such session")

    with session_lock(exam_id, student_id):
        meta = finalize_session(store)

    return {"status": meta["status"], "lastMerged": meta["lastMerged"]}


# --- read endpoints ---------------------------------------------------------
@app.get("/exams/{exam_id}/{student_id}/status")
def status(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    meta = store.load_metadata()
    if meta is None:
        raise HTTPException(status_code=404, detail="no such session")
    return {
        **meta,
        "pendingChunks": len(store.stored_sequences()),  # not yet merged
        "hasRecording": store.recording_ts_exists(),
    }


@app.get("/exams/{exam_id}/{student_id}/recording.mp4")
def download_recording(exam_id: str, student_id: str):
    store = _store(exam_id, student_id)
    if not store.recording_ts_exists():
        raise HTTPException(status_code=404, detail="recording not ready")

    # Build (or rebuild) the MP4 from the TS accumulator only if stale. This
    # lazy, cached conversion is the one O(total) step — done at review time,
    # not when the exam ends, so it never spikes.
    with session_lock(exam_id, student_id):
        if _mp4_is_stale(store):
            build_mp4_from_ts(store)

    return FileResponse(store.recording_path, media_type="video/mp4",
                        filename=f"{exam_id}_{student_id}.mp4")


def _mp4_is_stale(store: SessionStore) -> bool:
    if not store.recording_exists():
        return True
    # Rebuild if more footage has been appended to the TS since the last build.
    return os.path.getmtime(store.recording_ts_path) > \
        os.path.getmtime(store.recording_path)


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


def main() -> None:
    print(f"[server] storage at {config.storage_root}")
    print(f"[server] listening on http://{config.host}:{config.port}")
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
