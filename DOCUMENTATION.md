# SEB Webcam Proctoring — Technical Documentation

A complete reference for the system: what it does, how every piece works, the
wire protocol, and how to set it up, run it, and package it.

> **Status:** working prototype. The client records the webcam into short
> **H.264** MP4 chunks and uploads them to a server, which incrementally merges
> them into one recording per student using an **MPEG-TS accumulator** so the
> merge scales to many students over a multi-hour exam. Built to survive
> network drops and client disconnects.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [How it works (the big picture)](#2-how-it-works-the-big-picture)
3. [Why H.264 + MPEG-TS (the merge strategy)](#3-why-h264--mpeg-ts-the-merge-strategy)
4. [Project layout](#4-project-layout)
5. [Setup](#5-setup)
6. [Running it](#6-running-it)
7. [Code walkthrough — client](#7-code-walkthrough--client)
8. [Code walkthrough — server](#8-code-walkthrough--server)
9. [The shared protocol](#9-the-shared-protocol)
10. [HTTP API reference](#10-http-api-reference)
11. [metadata.json schema](#11-metadatajson-schema)
12. [Lifecycle scenarios](#12-lifecycle-scenarios)
13. [Configuration reference](#13-configuration-reference)
14. [Packaging the client (.exe)](#14-packaging-the-client-exe)
15. [Testing](#15-testing)
16. [Troubleshooting](#16-troubleshooting)
17. [Scaling notes](#17-scaling-notes)

---

## 1. What it does

- **Client** (student laptop): captures the webcam, encodes **H.264** MP4 chunks
  (~5 s each) via ffmpeg, and queues them on disk. A background uploader sends
  each chunk to the server over HTTP and deletes the local copy only after the
  server confirms receipt.
- **Server** (proctor laptop): authenticates each upload and stores the chunk.
  A background worker incrementally merges chunks into a per-student recording
  and deletes the merged chunks. The final `recording.mp4` is produced on
  demand when someone downloads it.

Key properties:

- **The local disk is the queue.** Network drops don't lose data — chunks
  accumulate and retry until connectivity returns.
- **The server reaches a complete recording on its own.** A background worker
  merges and cleans up even if the client crashes or never signals completion.
- **The merge scales.** Work is spread evenly across the exam (see §3), with no
  load spike when everyone finishes.

---

## 2. How it works (the big picture)

```
 STUDENT LAPTOP (client)                          PROCTOR LAPTOP (server)
┌──────────────────────────────┐                ┌───────────────────────────────────┐
│ CameraManager (OpenCV)        │                │ FastAPI (sync upload handler,      │
│      │ raw frames             │                │           threadpool)              │
│      ▼                        │  POST chunk    │   store chunk → metadata → ACK     │
│ FfmpegChunkWriter ── H.264 ──▶│ ─────────────▶ │        │ (fast, no merge here)     │
│   (ffmpeg subprocess)         │  200 / 409     │        ▼                           │
│      │ chunk_00001.mp4        │ ◀───────────── │   chunks/ on disk                  │
│      ▼                        │                │                                    │
│ DiskQueue (folder)            │                │ MergeWorker (background, ~10s)     │
│      │                        │                │   append contiguous chunks ──▶     │
│      ▼                        │                │   recording.ts  (byte-append)      │
│ UploadManager (retry+ACK)     │                │   delete merged chunks             │
└──────────────────────────────┘                │   auto-finalize idle sessions      │
                                                 │        │                           │
                                                 │        ▼  on download:             │
                                                 │   recording.ts ──▶ recording.mp4   │
                                                 └───────────────────────────────────┘
```

**Client — two threads:**
- The **recorder** thread captures frames and encodes rotating H.264 chunks,
  dropping finished files into the queue folder.
- The **uploader** thread uploads the oldest-numbered chunk first and deletes it
  on ACK.

They communicate only through the disk queue folder, so a slow/absent network
never blocks recording.

**Server — request path is thin, worker does the heavy lifting:**
- An **upload** only stores the chunk and returns immediately (the handler is
  synchronous, so FastAPI runs it in a threadpool — many students upload in
  parallel without blocking the event loop).
- The **MergeWorker** runs in the background: it appends each session's next
  contiguous batch of chunks to `recording.ts`, deletes the merged chunks, and
  auto-finalizes sessions whose client has gone silent.
- `recording.mp4` is built lazily from `recording.ts` when downloaded.

---

## 3. Why H.264 + MPEG-TS (the merge strategy)

The naïve approach — rebuild `recording.mp4` from all chunks (or re-mux the
whole growing file) on each merge — is **O(n²)** disk I/O over a long exam:
unusable at hundreds of students × hours.

The fix uses two container formats for what each is good at:

- **MPEG-TS (`.ts`)** splits media into small self-contained packets with *no
  global index*, so two `.ts` files concatenate by **raw byte-append**.
  Extending the recording is therefore **O(size of the new chunk)**, never
  O(whole recording).
- **MP4** has a `moov` index that must be rewritten to append — great for
  playback/seeking, bad for appending.

So:

| Phase | Action | Cost |
|---|---|---|
| During the exam | remux each chunk to TS (stream copy) and **byte-append** to `recording.ts`; delete the chunk | tiny, spread across the whole exam |
| At finalize | just stop — `recording.ts` is already complete | ~none (no end-of-exam spike) |
| At download | build `recording.mp4` from `recording.ts` in one stream-copy pass, cached | one O(total) pass, staggered across review time |

**H.264 is required** because MPEG-TS cannot carry MPEG-4 Part 2 (OpenCV's
`mp4v`). OpenCV also cannot *encode* H.264 in typical builds, which is why the
client encodes chunks with an ffmpeg subprocess instead of `cv2.VideoWriter`.

---

## 4. Project layout

```
ICPC/
├── README.md               quick start
├── DOCUMENTATION.md         this file
├── PACKAGING.md             build the client into a standalone exe
├── requirements.txt         runtime dependencies
├── requirements-build.txt   + PyInstaller, for packaging
├── proctor_client.py        PyInstaller entry point
├── proctor-client.spec      PyInstaller build recipe
├── proctor.ini.example      optional client config template
│
├── shared/
│   └── protocol.py          chunk naming, HTTP headers, metadata schema
│
├── client/                  runs on the student laptop
│   ├── config.py            ClientConfig (baked-in server URL, ffmpeg, etc.)
│   ├── camera.py            CameraManager — OpenCV webcam capture
│   ├── media.py             find_ffmpeg() + FfmpegChunkWriter (H.264 encode)
│   ├── recorder.py          ChunkRecorder — frames → rotating H.264 chunks
│   ├── disk_queue.py        DiskQueue — durable on-disk buffer
│   ├── uploader.py          UploadManager — upload + retry + ACK-delete
│   └── main.py              settings resolution + run loop
│
├── server/                  runs on the proctor laptop
│   ├── config.py            ServerConfig (worker interval, stale timeout)
│   ├── locks.py             per-session locks (uploads + worker coordinate)
│   ├── storage.py           SessionStore — the only file-touching module
│   ├── merger.py            TS append + on-demand mp4 build
│   ├── worker.py            MergeWorker + merge_pending/finalize_session
│   └── main.py              FastAPI app + entry point
│
├── build/                   local build scripts (build_windows.bat / _linux.sh)
└── .github/workflows/       CI to build exes for Windows/Linux/macOS
```

---

## 5. Setup

### Prerequisites

| Requirement | Where | Why |
|---|---|---|
| Python 3.10+ | client & server | runs the code |
| **ffmpeg on PATH** | **client** (encode) **and server** (merge) | H.264 encode + TS/MP4 mux |
| A webcam | client | capture |
| Two machines on one LAN | deployment | (or use one machine to test) |

> ffmpeg is now required on the **client** too (for H.264 encoding), not just
> the server. For a packaged client you can bundle ffmpeg — see §14.

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt`: `opencv-python` (capture), `requests` (upload),
`fastapi` + `uvicorn[standard]` + `python-multipart` (server).

---

## 6. Running it

### Two laptops

**Server** (proctor laptop; note its LAN IP):

```bash
python -m server.main
# [server] storage at ./storage
# [server] listening on http://0.0.0.0:8000
# [worker] started: merge every 10s, auto-finalize after 60s idle
```

**Client** (student laptop):

```bash
python -m client.main --server http://192.168.1.50:8000 \
                      --exam exam2026 --student student001
```

Live status line: `[client] recorded=7 uploaded=6 queued=1`. Stop with
`Ctrl+C` — the client drains the queue before exiting.

> The client's server URL has a **baked-in default** (see
> `DEFAULT_SERVER_URL` in `client/config.py`), so `--server` is optional once
> set. Resolution order: CLI flag > `PROCTOR_SERVER` env > `proctor.ini` >
> baked default.

### Single machine (testing)

```bash
# terminal 1
python -m server.main
# terminal 2
python -m client.main --server http://127.0.0.1:8000 --exam exam2026 --student student001
```

### Get the result

```bash
curl http://<server>:8000/exams/exam2026/student001/status
curl -O http://<server>:8000/exams/exam2026/student001/recording.mp4
```

### Client CLI flags

| Flag | Default | Meaning |
|---|---|---|
| `--server` | baked-in URL | server base URL |
| `--exam` | prompt / `exam2026` | exam id |
| `--student` | prompt | student id |
| `--camera` | `0` | webcam device index |
| `--chunk-seconds` | `5.0` | chunk length |

---

## 7. Code walkthrough — client

Pipeline: **camera → recorder (ffmpeg H.264) → disk queue → uploader → server.**

### 7.1 `client/config.py` — `ClientConfig`

Dataclass of all tunables (all have defaults, so a bare exe runs). Notable:
`server_url` defaults to the baked-in `DEFAULT_SERVER_URL`; `ffmpeg_bin`
(override the ffmpeg path, else auto-discover); `fps`, `chunk_seconds`,
`queue_dir`, retry backoff. `session_queue_dir` isolates each session's chunks.

### 7.2 `client/camera.py` — `CameraManager`

Thin wrapper over OpenCV `VideoCapture`. `open()`, `read_frame()` (returns
`None` on a transient glitch), `actual_resolution` (the size the camera actually
produced, used to size the encoder). Usable as a context manager.

### 7.3 `client/media.py` — ffmpeg encoding

- `find_ffmpeg(override)` locates ffmpeg: explicit override → bundled next
  to/inside the packaged exe → system PATH.
- `FfmpegChunkWriter` spawns one ffmpeg per chunk (`-f rawvideo` in, `libx264
  -preset ultrafast` out) and encodes raw BGR frames written to its stdin into
  an H.264 MP4. `close()` flushes and raises if ffmpeg failed.

### 7.4 `client/recorder.py` — `ChunkRecorder`

Own thread. Captures frames and writes **rotating H.264 chunks** — a new file
every `chunk_seconds`. Details:

- **Constant-rate, wall-clock-paced writing** so playback speed is correct:
  a chunk tagged at `fps` always contains exactly `fps × chunk_seconds` frames;
  if the camera runs behind, the latest frame is duplicated to keep the timeline
  real-time (this fixes the earlier "sped-up video" bug).
- **Write-then-rename**: encodes to a staging name (`.chunk_NNNNNN.mp4`) and
  renames to the final name only when complete, so the uploader never grabs a
  half-written file.
- **Resume after a crash**: numbering continues after any leftover queued
  chunks.
- Raises a clear error at startup if **ffmpeg isn't found**.

### 7.5 `client/disk_queue.py` — `DiskQueue`

The durable buffer = a folder. `pending()` lists queued chunks oldest-first
(ignoring staging files); `remove()` deletes on ACK; `path_for` /
`staging_path_for` give final vs in-progress names.

### 7.6 `client/uploader.py` — `UploadManager`

Own thread. Uploads chunks in sequence order; deletes a chunk only after the
server returns `200`/`201` (stored) or `409` (server already has it). Any error
or non-OK status retries forever with exponential backoff — this is the offline
recovery. `drain_and_stop()` flushes the queue at shutdown.

### 7.7 `client/main.py` — entry & settings

`resolve_config()` gathers settings from CLI > env > `proctor.ini` (next to the
exe) > interactive prompts (so a double-clicked exe works). `run(config)` opens
the camera, starts the recorder + uploader threads, prints a status line until
`Ctrl+C`, then finalizes.

---

## 8. Code walkthrough — server

### 8.1 `server/config.py` — `ServerConfig`

Host, port, `storage_root`, `auth_token`, plus worker tunables
`merge_interval` (default 10 s) and `stale_after` (default 60 s). All overridable
via env (`PROCTOR_*`).

### 8.2 `server/locks.py`

A registry of per-`(exam, student)` `threading.Lock`s shared by the upload
handler and the worker, so a merge never races a concurrent upload while
different students proceed in parallel.

### 8.3 `server/storage.py` — `SessionStore`

The only filesystem module. Manages:

```
storage/<exam>/<student>/
    chunks/chunk_000000.mp4 ...   (transient; deleted after merge)
    recording.ts                  (TS accumulator, grows during exam)
    recording.mp4                 (built on demand from the TS)
    metadata.json
```

Key methods: `save_chunk` (atomic), `has_chunk`, `stored_sequences`,
`contiguous_after(last_merged)` (the next gap-free batch to merge),
`delete_chunks`, `recording_ts_path` / `recording_ts_exists`, and the metadata
helpers.

### 8.4 `server/merger.py` — the scalable merge

- `append_chunks_to_ts(store, sequences)` — remux the batch to MPEG-TS
  (`-c copy`) and **byte-append** it to `recording.ts`. O(batch), no re-copy of
  existing footage.
- `build_mp4_from_ts(store)` — one stream-copy pass `recording.ts →
  recording.mp4` (`+faststart`). Called lazily on download.

Both raise `MergeError` on ffmpeg failure. The server never *decodes* video —
only remuxes containers.

### 8.5 `server/worker.py` — `MergeWorker`

A daemon thread that, every `merge_interval`, walks every session and:

1. `merge_pending` — append the next contiguous chunk batch to the TS, delete
   the merged chunks, advance `lastMerged`.
2. auto-finalize — if a session in `Recording` has received no chunk for
   `stale_after` seconds (client disconnected), `finalize_session` flushes any
   remaining chunks (across gaps) and marks it `Finalized`.

`merge_pending` / `finalize_session` are also called directly by the `/finalize`
endpoint. All assume the caller holds the session lock.

### 8.6 `server/main.py` — FastAPI app

- `lifespan` starts/stops the worker with the app.
- `require_auth` checks the bearer token.
- **Upload** (`POST .../chunks`) is a **sync** handler (threadpool): validate →
  under the session lock, reject already-merged/duplicate sequences with `409`,
  else `save_chunk`, update `lastReceived` / `lastChunkAt` / `status` → return
  fast. **No merging on the request path.**
- **Finalize** flushes + marks `Finalized`.
- **Download** builds `recording.mp4` from the TS on demand (only if the TS grew
  since the last build), then serves it.
- **Status** returns metadata + `pendingChunks` + `hasRecording`.

---

## 9. The shared protocol

`shared/protocol.py` — the contract both sides agree on.

**Chunk naming:** `chunk_filename(5) → "chunk_000005.mp4"`, zero-padded so a
sorted listing is playback order. `sequence_from_filename()` parses it back.

**HTTP contract:**

| Constant | Value | Use |
|---|---|---|
| `UPLOAD_FILE_FIELD` | `chunk` | multipart file field |
| `HEADER_SEQUENCE` | `X-Chunk-Sequence` | which chunk number |
| `HEADER_AUTH` | `Authorization` | `Bearer <token>` |
| `DEFAULT_AUTH_TOKEN` | `prototype-shared-secret` | override via `PROCTOR_TOKEN` |

**Metadata:** `new_metadata(...)` builds the initial `metadata.json`; status
constants `STATUS_RECORDING` / `STATUS_FINALIZED`.

---

## 10. HTTP API reference

Base URL: `http://<server>:8000`

### `POST /exams/{exam_id}/{student_id}/chunks` — upload one chunk (auth)

Headers: `Authorization: Bearer <token>`, `X-Chunk-Sequence: <int>`.
Body: multipart, file field `chunk`.

| Status | Meaning | Client action |
|---|---|---|
| `200` | stored | delete local copy |
| `409` | already stored or already merged | delete local copy |
| `400` | empty body / negative sequence | fix & resend |
| `401` | bad/missing token | — |

```bash
curl -X POST http://127.0.0.1:8000/exams/exam2026/student001/chunks \
  -H "Authorization: Bearer prototype-shared-secret" \
  -H "X-Chunk-Sequence: 0" \
  -F "chunk=@chunk_000000.mp4;type=video/mp4"
```

### `POST /exams/{exam_id}/{student_id}/finalize` — flush + finalize (auth)

```json
{ "status": "Finalized", "lastMerged": 41 }
```

### `GET /exams/{exam_id}/{student_id}/status`

```json
{
  "studentId": "student001", "examId": "exam2026",
  "expectedChunk": 42, "lastReceived": 41, "lastMerged": 41,
  "lastChunkAt": 1751200000.0, "status": "Recording",
  "codec": "H264", "resolution": "unknown", "fps": 0,
  "pendingChunks": 0, "hasRecording": true
}
```

### `GET /exams/{exam_id}/{student_id}/recording.mp4`

Builds the MP4 from the TS on demand (cached) and returns it; `404` if no
footage yet.

### `GET /exams` — list sessions · `GET /health`

FastAPI also serves interactive docs at `/docs`.

---

## 11. metadata.json schema

`storage/<exam>/<student>/metadata.json`:

| Field | Meaning |
|---|---|
| `studentId`, `examId` | session identity |
| `expectedChunk` | next contiguous sequence wanted (`lastMerged + 1`) |
| `lastReceived` | highest sequence stored |
| `lastMerged` | highest sequence appended to `recording.ts` |
| `lastChunkAt` | epoch seconds of the last upload (staleness detection) |
| `status` | `Recording` or `Finalized` |
| `codec`, `resolution`, `fps` | reported labels |

---

## 12. Lifecycle scenarios

**Normal chunk:** encode H.264 → stage → rename → upload → server stores → ACK →
client deletes local copy → worker later appends it to `recording.ts` and
deletes the server-side chunk.

**Network drops:** recorder keeps writing chunks to disk (queue grows); uploads
back off and retry; on reconnect the queue drains oldest-first.

**Out-of-order / lost chunk:** a chunk after a gap is stored but not merged; the
worker only appends the contiguous run, so `recording.ts` stops at the gap until
it's filled. On finalize, remaining chunks are flushed across gaps.

**Duplicate upload:** server returns `409` (already stored or already merged);
client drops its copy.

**Client crash/disconnect:** the worker keeps merging what arrived, and after
`stale_after` seconds of silence auto-finalizes the session — a complete
recording with no client action.

**Graceful stop (`Ctrl+C`):** recorder stops, uploader drains, session can be
finalized (or the worker finalizes it as stale).

---

## 13. Configuration reference

### Client (`client/config.py` / env)

| Setting | Default | Env |
|---|---|---|
| server URL | baked `DEFAULT_SERVER_URL` | `PROCTOR_SERVER` |
| chunk length | `5.0 s` | — (`--chunk-seconds`) |
| resolution | `1280x720` | — |
| fps | `30` | — |
| ffmpeg path | auto-discover | `PROCTOR_FFMPEG` |
| queue dir | beside app / `./client_queue` | `PROCTOR_QUEUE` |
| auth token | shared secret | `PROCTOR_TOKEN` |

### Server (`server/config.py` / env)

| Setting | Default | Env |
|---|---|---|
| host / port | `0.0.0.0` / `8000` | `PROCTOR_HOST` / `PROCTOR_PORT` |
| storage root | `./storage` | `PROCTOR_STORAGE` |
| auth token | shared secret | `PROCTOR_TOKEN` |
| merge interval | `10 s` | `PROCTOR_MERGE_INTERVAL` |
| stale timeout | `60 s` | `PROCTOR_STALE_AFTER` |

> Client and server **must share the same token**.

---

## 14. Packaging the client (.exe)

Full guide in **`PACKAGING.md`**. In short:

- `pyinstaller proctor-client.spec` builds a single self-contained executable
  (Windows `ProctorClient.exe`, Linux/macOS `ProctorClient`). **PyInstaller does
  not cross-compile** — build on each target OS, or use the included GitHub
  Actions workflow.
- **ffmpeg** (needed for H.264 encoding) is bundled if you drop a static binary
  in `vendor/` (`vendor/ffmpeg.exe` on Windows), else the client uses ffmpeg on
  PATH.
- Settings resolve from CLI > env > `proctor.ini` (next to the exe) > prompts.
  The server URL is baked in, so a bare exe already points at your server.

---

## 15. Testing

Verified end-to-end (with ffmpeg-generated chunks and a fake camera, so no
physical webcam needed):

- scalable merge: TS byte-append, chunk deletion after merge, on-demand MP4
  build (mid-exam and final), correct **H.264** duration, light finalize;
- auto-finalize on client disconnect (stale detection);
- real-time chunk duration despite a slow camera (speed fix);
- full pipeline: camera → H.264 chunks → upload → TS merge → downloaded MP4;
- duplicate → `409`, gap handling, `401` on bad token.

---

## 16. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg was not found` (client) | no ffmpeg | install ffmpeg on PATH, set `PROCTOR_FFMPEG`, or bundle it (§14) |
| `could not open camera index 0` | wrong index / in use | `--camera 1`; close other apps |
| client retries forever | server unreachable / wrong IP / firewall | check URL; `curl http://<ip>:8000/health`; open the port |
| `401` on upload | token mismatch | same `PROCTOR_TOKEN` on both sides |
| `recording not ready` (404) | nothing merged yet | wait for the worker; check `/status` `hasRecording` |
| recording shorter than expected | a gap (missing chunk) | compare `lastReceived` vs `lastMerged` |
| merge/ffmpeg errors on server | server ffmpeg missing/old | install ffmpeg, ensure on PATH |

---

## 17. Scaling notes

The merge now scales (§3), and uploads run in a threadpool, so a single server
handles many students. For a large exam (e.g. 300 students × 3 hours):

- **Bandwidth:** ~60 MB/s (~480 Mbit/s) → **wired gigabit** to the server, not
  WiFi.
- **Storage:** ~1–2 GB/student → plan **hundreds of GB** free per exam.
- **Static server IP:** reserve one before baking it into the client.
- **Multiple workers / redundancy:** run several uvicorn workers and, ideally, a
  second server with shared storage; the in-process locks would then move to a
  shared store, and chunks/TS to shared or object storage — only
  `server/storage.py` + `server/merger.py` touch the filesystem, so that's the
  seam to change.
- **HTTPS:** the prototype uses plain HTTP for LAN; put it behind TLS for
  production (the client already sends a bearer token).
