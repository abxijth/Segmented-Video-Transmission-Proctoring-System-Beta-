# SEB Webcam Proctoring — Technical Documentation

A complete reference for the prototype: what it does, how every piece works,
the wire protocol, and exactly how to set it up and run it.

> **Status:** working prototype (roadmap phases 1–8). It records a webcam into
> short MP4 chunks on the student's laptop and uploads them to a server on the
> proctor's laptop, which stores every chunk and merges them into one
> `recording.mp4` per student. Built to survive network drops.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [How it works (the big picture)](#2-how-it-works-the-big-picture)
3. [Project layout](#3-project-layout)
4. [Setup](#4-setup)
5. [Running it](#5-running-it)
6. [Code walkthrough — client](#6-code-walkthrough--client)
7. [Code walkthrough — server](#7-code-walkthrough--server)
8. [The shared protocol](#8-the-shared-protocol)
9. [HTTP API reference](#9-http-api-reference)
10. [metadata.json schema](#10-metadatajson-schema)
11. [Lifecycle scenarios](#11-lifecycle-scenarios)
12. [Configuration reference](#12-configuration-reference)
13. [Testing](#13-testing)
14. [Troubleshooting](#14-troubleshooting)
15. [Scaling notes](#15-scaling-notes)

---

## 1. What it does

- **Client** (student laptop): captures the webcam, encodes video, and writes
  it to disk as a stream of short (~5 second) MP4 chunks. A background uploader
  sends each chunk to the server over HTTP and deletes the local copy only
  after the server confirms receipt.
- **Server** (proctor laptop): authenticates each upload, stores the chunk,
  records progress in a small JSON file, and merges the chunks — in order —
  into a single playable `recording.mp4` per student.

Key property: **the local disk is the queue.** If the network drops, recording
continues, chunks accumulate on disk, and the uploader keeps retrying until the
connection returns. Nothing is lost.

---

## 2. How it works (the big picture)

```
 STUDENT LAPTOP (client)                         PROCTOR LAPTOP (server)
┌───────────────────────────────┐               ┌────────────────────────────────┐
│ CameraManager                 │               │ FastAPI app                    │
│   reads frames                │               │   POST .../chunks              │
│        │                      │               │     │                          │
│        ▼                      │   HTTP POST    │     ▼                          │
│ ChunkRecorder ── chunk_000001 │  (one chunk    │  authenticate                 │
│   rotates every ~5s           │   per request) │     │                          │
│        │                      │ ─────────────► │  store chunk (SessionStore)   │
│        ▼                      │                │     │                          │
│ DiskQueue (folder on disk)    │                │  merge contiguous chunks      │
│        │                      │ ◄───────────── │   (ffmpeg concat -c copy)     │
│        ▼                      │   200 / 409    │     │                          │
│ UploadManager                 │   (ACK)        │  update metadata.json         │
│   upload → on ACK → delete    │                │     │                          │
└───────────────────────────────┘               │     ▼  recording.mp4           │
                                                 └────────────────────────────────┘
```

**Two threads on the client**, running independently:

- The **recorder thread** captures frames and rotates a new MP4 file every few
  seconds, dropping finished chunks into the queue folder.
- The **uploader thread** scans the queue folder, uploads the
  oldest-numbered chunk first, and deletes it once the server ACKs.

Because the two threads only communicate through the **disk queue folder**, a
slow or absent network never blocks recording.

**On the server**, every upload runs through the same short pipeline:
authenticate → store chunk → re-merge the contiguous run of chunks into
`recording.mp4` → update `metadata.json`.

---

## 3. Project layout

```
ICPC/
├── README.md             quick start
├── DOCUMENTATION.md      this file
├── requirements.txt      Python dependencies
├── .gitignore
│
├── shared/               code shared by client AND server
│   └── protocol.py       chunk naming, HTTP header names, metadata schema
│
├── client/               runs on the student laptop
│   ├── config.py         ClientConfig — all client tunables
│   ├── camera.py         CameraManager — owns the webcam device
│   ├── recorder.py       ChunkRecorder — frames → rotating MP4 chunks
│   ├── disk_queue.py     DiskQueue — durable on-disk buffer
│   ├── uploader.py       UploadManager — upload + retry + ACK-delete
│   └── main.py           entry point; wires the pipeline together
│
└── server/               runs on the proctor laptop
    ├── config.py         ServerConfig — all server tunables
    ├── storage.py        SessionStore — the only file-touching module
    ├── merger.py         merge chunks → recording.mp4 via ffmpeg
    └── main.py           FastAPI app + entry point
```

Design rule: **one responsibility per module.** The client never imports server
code and vice-versa; the only thing they share is `shared/protocol.py`, which
defines the contract between them.

---

## 4. Setup

### Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Python 3.10+ | runs both client and server | `python3 --version` |
| ffmpeg on PATH | server merges chunks; tests generate chunks | `ffmpeg -version` |
| A webcam | client capture (client laptop only) | — |
| Two machines on one LAN | real deployment (or use one machine to test) | — |

### Install

Run this on **both** laptops (the client laptop needs OpenCV; the server laptop
needs FastAPI — installing everything on both is simplest):

```bash
cd ICPC
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt`:

```
opencv-python>=4.8     # client: webcam capture + MP4 writing
requests>=2.31         # client: HTTP uploads
fastapi>=0.110         # server: web framework
uvicorn[standard]      # server: ASGI server
python-multipart       # server: parse multipart file uploads
```

> **ffmpeg vs OpenCV.** OpenCV (on the client) *writes* the chunk MP4s. ffmpeg
> (on the server) *merges* them. The server never decodes video — it only
> stitches containers — so the server side stays lightweight.

---

## 5. Running it

### Real deployment (two laptops)

**Step 1 — start the server** on the proctor laptop and note its LAN IP:

```bash
source .venv/bin/activate
python -m server.main
# [server] storage at ./storage
# [server] listening on http://0.0.0.0:8000
```

Find the LAN IP: `ip addr` (Linux), `ipconfig` (Windows), or
`ipconfig getifaddr en0` (macOS). Say it's `192.168.1.50`.

**Step 2 — start the client** on the student laptop:

```bash
source .venv/bin/activate
python -m client.main --server http://192.168.1.50:8000 \
                      --exam exam2026 --student student001
```

You'll see a live status line:

```
[client] recorded=7 uploaded=6 queued=1
```

**Step 3 — stop** with `Ctrl+C`. The client finalizes: it stops recording,
drains any remaining queued chunks to the server, and exits.

### Get the result

```bash
# session status (counts + metadata)
curl http://192.168.1.50:8000/exams/exam2026/student001/status

# download the merged recording
curl -O http://192.168.1.50:8000/exams/exam2026/student001/recording.mp4
```

### Test on a single machine (no second laptop)

Open two terminals on the same computer:

```bash
# terminal 1
python -m server.main

# terminal 2
python -m client.main --server http://127.0.0.1:8000 \
                      --exam exam2026 --student student001
```

### CLI flags (client)

| Flag | Default | Meaning |
|---|---|---|
| `--server` | *(required)* | server base URL, e.g. `http://192.168.1.50:8000` |
| `--exam` | *(required)* | exam id (becomes a storage folder) |
| `--student` | *(required)* | student id (becomes a storage folder) |
| `--camera` | `0` | webcam device index |
| `--chunk-seconds` | `5.0` | length of each chunk |

---

## 6. Code walkthrough — client

The client is a small pipeline. Data flows **camera → recorder → disk queue →
uploader → server**. Each stage is one class.

### 6.1 `client/config.py` — `ClientConfig`

A single dataclass holding every tunable so there are no magic numbers spread
through the code. Notable fields:

- `server_url`, `exam_id`, `student_id`, `auth_token` — session identity.
- `frame_width / frame_height / fps` — requested camera settings.
- `chunk_seconds` — how often the recorder rotates to a new file.
- `chunk_fourcc` — the codec FourCC for OpenCV's MP4 writer. `"mp4v"` (MPEG-4,
  default) works everywhere; `"avc1"` is H.264 but needs an OpenCV build with
  H.264 support.
- `queue_dir` — where chunks live before upload.
- `retry_base_delay / retry_max_delay` — exponential-backoff bounds.

Helper properties:

- `session_queue_dir` → `queue_dir/<exam>/<student>`, so two sessions never mix
  their chunks.
- `codec_name` → `"H264"` or `"MPEG4"` for reporting.

Environment overrides: `PROCTOR_TOKEN`, `PROCTOR_FOURCC`, `PROCTOR_QUEUE`.

### 6.2 `client/camera.py` — `CameraManager`

A thin wrapper over OpenCV's `VideoCapture` so the rest of the client never
touches OpenCV's device API directly.

- `open()` — opens the device, applies width/height/fps, raises if the camera
  can't be opened.
- `actual_resolution` — the size the camera *actually* gave (cameras often
  ignore the requested size); the recorder uses this to size the MP4 writer
  correctly.
- `read_frame()` — returns the next BGR frame, or `None` on a transient glitch
  (the recorder just tries again).
- Usable as a context manager (`with CameraManager(cfg) as cam:`).

### 6.3 `client/disk_queue.py` — `DiskQueue`

The durable buffer between recording and uploading. **The queue is just a
folder.** That is what makes the system crash- and outage-resilient: anything
not yet uploaded is still a file on disk after a restart.

- `path_for(sequence)` → the on-disk path for chunk *N*.
- `pending()` → all queued chunks as `(sequence, path)`, **oldest first**.
  Files still being written carry a `.part` suffix and are ignored here, so the
  uploader can never grab a half-written chunk.
- `remove(path)` → delete a chunk after the server ACKs it (idempotent).
- `is_empty()` → used by the uploader to know when it has fully drained.

Because chunk filenames are zero-padded (`chunk_000005.mp4`), a plain sorted
directory listing is also the correct playback/upload order.

### 6.4 `client/recorder.py` — `ChunkRecorder`

Runs on its own thread. Captures frames and writes them as **rotating** MP4
chunks: every `chunk_seconds` it closes the current file and opens the next.

Important details:

- **Write-then-rename.** Each chunk is written to `chunk_NNNNNN.mp4.part` and
  renamed to its final name only when complete (`os.replace`, an atomic rename).
  The uploader therefore only ever sees finished chunks.
- **Resume after a crash.** On startup the recorder inspects the queue and
  continues numbering *after* any leftover chunks, so a restarted session never
  overwrites chunks that haven't been uploaded yet.
- **Glitch tolerance.** If `read_frame()` returns `None`, it skips and keeps
  going rather than crashing.
- **Empty-chunk guard.** If a rotation captured zero frames (e.g. camera
  hiccup), the `.part` file is discarded instead of being published.

`stop()` sets an event and joins the thread, so the in-progress chunk is closed
cleanly on shutdown.

### 6.5 `client/uploader.py` — `UploadManager`

Runs on its own thread. Drains the queue to the server.

- Uploads chunks **strictly in sequence order** (oldest first).
- A chunk is deleted **only after** the server returns `200`/`201` (stored) or
  `409` (server already has it — e.g. a duplicate after a retry). Either way the
  client safely drops its local copy.
- On any network error or non-OK status it **retries forever** with exponential
  backoff (`retry_base_delay` doubling up to `retry_max_delay`). This is the
  offline-recovery behavior: lose the network, chunks pile up, retry until it
  returns.
- `drain_and_stop(timeout)` — used at shutdown: keep uploading until the queue
  is empty (or the timeout hits), then stop. Returns whether it fully drained.

### 6.6 `client/main.py` — entry point

Parses CLI args into a `ClientConfig`, opens the camera, then starts the
recorder and uploader threads. The main thread prints a live status line until
`Ctrl+C`, then:

1. `recorder.stop()` — stop capturing (finishes the current chunk).
2. `uploader.drain_and_stop()` — flush remaining chunks to the server.
3. Reports how many chunks uploaded, or warns if some remain (they'll upload on
   the next run, since the queue folder persists).

---

## 7. Code walkthrough — server

### 7.1 `server/config.py` — `ServerConfig`

Host, port, `storage_root`, and `auth_token`. All overridable via environment
variables: `PROCTOR_HOST`, `PROCTOR_PORT`, `PROCTOR_STORAGE`, `PROCTOR_TOKEN`.

### 7.2 `server/storage.py` — `SessionStore`

**The only module that touches the filesystem.** Scoped to one
`(exam, student)` session. Swap this out (e.g. for S3) and nothing else changes.

On-disk layout it manages:

```
storage/<exam>/<student>/
    chunks/chunk_000000.mp4
    chunks/chunk_000001.mp4
    ...
    recording.mp4
    metadata.json
```

Methods:

- `save_chunk(seq, data)` — atomic write (temp file + rename).
- `has_chunk(seq)` — duplicate detection.
- `stored_sequences()` — all stored chunk numbers, ascending.
- `contiguous_sequences()` — **the key one.** Returns the longest gap-free run
  `0,1,2,…`. Only this run is safe to merge; if chunk 3 is missing, the merge
  stops at 2 even if chunk 4 has arrived.
- `load_metadata()` / `save_metadata()` / `init_metadata_if_absent()` — manage
  `metadata.json` (atomic writes).

Plus a module function `list_sessions(storage_root)` returning every
`(exam, student)` on disk, used by the `/exams` endpoint.

### 7.3 `server/merger.py` — merge service

`merge_recording(store)` rebuilds `recording.mp4` from the contiguous run of
chunks and returns the highest sequence now included.

How:

1. Get `contiguous_sequences()`.
2. Write an ffmpeg **concat demuxer** list file (`file '…/chunk_000000.mp4'`…).
3. Run `ffmpeg -f concat -safe 0 -i list.txt -c copy recording.mp4.building.mp4`.
   - `-c copy` = **stream copy, no re-encoding** — fast and lossless. The server
     never decodes H.264.
4. Atomically rename the `.building.mp4` into place, so a viewer downloading the
   file never sees a half-built recording.

Failures raise `MergeError`, which the API turns into an HTTP 500.

> **Prototype trade-off.** It rebuilds the whole recording on every upload
> (O(n²) over a long exam). Correct and simple; switch to incremental append
> during load testing (roadmap phase 9). See [Scaling notes](#15-scaling-notes).

### 7.4 `server/main.py` — FastAPI app

Wires everything into HTTP endpoints. Highlights:

- `require_auth` dependency checks the `Authorization: Bearer <token>` header
  against the configured token; mismatch → `401`.
- **Per-session lock.** A `defaultdict` of `threading.Lock` keyed by
  `(exam, student)` serializes *store + merge* for one student, while different
  students upload fully in parallel. This prevents two concurrent uploads for
  the same student from racing on the merge.
- The upload handler: validate sequence/body → (under the lock) reject
  duplicates with `409` → `save_chunk` → update `lastReceived` → `merge` →
  update `lastMerged` / `expectedChunk` → save metadata → ACK.

See the full [API reference](#9-http-api-reference) below.

---

## 8. The shared protocol

`shared/protocol.py` is the contract both sides agree on. Keeping it in one file
means the client and server can never drift apart.

**Chunk naming**

```python
chunk_filename(5)                       # -> "chunk_000005.mp4"
sequence_from_filename("chunk_000005.mp4")  # -> 5
```

Zero-padded to 6 digits (`CHUNK_SEQ_WIDTH`), so lexical sort == numeric order.

**HTTP contract**

| Constant | Value | Use |
|---|---|---|
| `UPLOAD_FILE_FIELD` | `chunk` | multipart file field name |
| `HEADER_SEQUENCE` | `X-Chunk-Sequence` | which chunk number this is |
| `HEADER_AUTH` | `Authorization` | `Bearer <token>` |
| `DEFAULT_AUTH_TOKEN` | `prototype-shared-secret` | override via `PROCTOR_TOKEN` |

**Metadata**

`new_metadata(...)` builds the initial `metadata.json`; status constants
`STATUS_RECORDING` / `STATUS_FINALIZED`.

---

## 9. HTTP API reference

Base URL: `http://<server>:8000`

### `POST /exams/{exam_id}/{student_id}/chunks`

Upload one chunk. **Auth required.**

- Headers: `Authorization: Bearer <token>`, `X-Chunk-Sequence: <int>`
- Body: multipart form, file field `chunk` (the MP4)

Responses:

| Status | Meaning | Client action |
|---|---|---|
| `200` | stored & merged | delete local copy |
| `409` | server already had this chunk | delete local copy |
| `400` | empty body or negative sequence | fix and resend |
| `401` | bad/missing token | — |
| `500` | merge failed | retry |

Example `200` body:

```json
{ "status": "stored", "sequence": 7, "lastMerged": 7 }
```

```bash
curl -X POST http://127.0.0.1:8000/exams/exam2026/student001/chunks \
  -H "Authorization: Bearer prototype-shared-secret" \
  -H "X-Chunk-Sequence: 0" \
  -F "chunk=@chunk_000000.mp4;type=video/mp4"
```

### `POST /exams/{exam_id}/{student_id}/finalize`

Force a final merge and mark the session `Finalized`. **Auth required.**

```json
{ "status": "Finalized", "lastMerged": 41 }
```

### `GET /exams/{exam_id}/{student_id}/status`

Metadata plus live counts. No auth (read-only).

```json
{
  "studentId": "student001",
  "examId": "exam2026",
  "expectedChunk": 3,
  "lastReceived": 4,
  "lastMerged": 2,
  "status": "Finalized",
  "codec": "H264",
  "resolution": "unknown",
  "fps": 0,
  "storedChunks": 4,
  "contiguousChunks": 3,
  "hasRecording": true
}
```

### `GET /exams/{exam_id}/{student_id}/recording.mp4`

Download the merged recording (`404` if not ready yet).

### `GET /exams`

List every session on disk.

```json
{ "sessions": [ { "examId": "exam2026", "studentId": "student001" } ] }
```

### `GET /health`

`{ "status": "ok" }`

> FastAPI also serves interactive docs at `http://<server>:8000/docs`.

---

## 10. metadata.json schema

Written per student at `storage/<exam>/<student>/metadata.json`.

| Field | Meaning |
|---|---|
| `studentId`, `examId` | session identity |
| `expectedChunk` | next contiguous sequence the server wants (`lastMerged + 1`) |
| `lastReceived` | highest sequence stored (may be ahead of merge if there's a gap) |
| `lastMerged` | highest sequence folded into `recording.mp4` |
| `status` | `Recording` or `Finalized` |
| `codec` | reported codec label |
| `resolution`, `fps` | reported capture settings |

`lastReceived` ahead of `lastMerged` means chunks arrived out of order and a gap
is still unfilled.

---

## 11. Lifecycle scenarios

**Normal chunk:**
```
record → write .part → rename to chunk_N → uploader POSTs →
server stores → merges 0..N → 200 ACK → client deletes local chunk_N
```

**Network drops mid-exam:**
```
recorder keeps writing chunks to disk (queue grows) →
uploader's POST fails → backs off, retries → ...network returns...
→ queued chunks upload oldest-first → queue drains
```

**Out-of-order / lost chunk:** chunk 3 never arrives, chunk 4 does. Server
*stores* 4 (`lastReceived = 4`) but `contiguous_sequences()` stops at 2, so
`recording.mp4` only contains 0–2 (`lastMerged = 2`). When 3 finally arrives,
the next merge extends to 4 automatically.

**Duplicate upload** (client retried after a missed ACK): server already has the
chunk → returns `409` → client drops its copy. No corruption.

**Client crash/restart:** the queue folder persists. On restart the recorder
resumes numbering after the leftover chunks and the uploader flushes them.

**Graceful stop (`Ctrl+C`):** recorder stops, uploader drains the queue, session
can be finalized.

---

## 12. Configuration reference

### Client (`client/config.py` or env)

| Setting | Default | Env override |
|---|---|---|
| chunk length | `5.0 s` | — (or `--chunk-seconds`) |
| resolution | `1280x720` | — |
| fps | `30` | — |
| codec FourCC | `mp4v` | `PROCTOR_FOURCC` |
| queue dir | `./client_queue` | `PROCTOR_QUEUE` |
| auth token | shared secret | `PROCTOR_TOKEN` |
| retry backoff | `1s → 30s` | — |

### Server (`server/config.py` or env)

| Setting | Default | Env override |
|---|---|---|
| host | `0.0.0.0` | `PROCTOR_HOST` |
| port | `8000` | `PROCTOR_PORT` |
| storage root | `./storage` | `PROCTOR_STORAGE` |
| auth token | shared secret | `PROCTOR_TOKEN` |

> The client and server **must share the same token.** Set `PROCTOR_TOKEN` to
> the same value on both, or change `DEFAULT_AUTH_TOKEN` in `shared/protocol.py`.

---

## 13. Testing

The pipeline was verified end-to-end without a physical webcam by generating
synthetic MP4 chunks with ffmpeg and pushing them through the real server and
the real client upload classes. Verified behaviors:

- store → merge contiguous chunks into a valid H.264 `recording.mp4`
  (ffprobe-confirmed codec and duration)
- duplicate chunk → `409`
- gap handling: an out-of-order chunk is stored but not merged past the gap
- auth rejection (`401`) on missing token
- the real `DiskQueue` + `UploadManager` path: queue → upload in order →
  delete on ACK

To re-run a quick manual check on one machine: start the server, start the
client against `127.0.0.1`, let it record for ~20s, `Ctrl+C`, then
`GET .../status` and download `recording.mp4`.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `could not open camera index 0` | wrong index / camera in use | try `--camera 1`; close other apps using the webcam |
| `VideoWriter failed for fourcc 'avc1'` | OpenCV build lacks H.264 | use `mp4v` (default), or move capture to an ffmpeg subprocess |
| client prints `retry in Ns` forever | server unreachable / wrong IP / firewall | verify `--server` URL; `curl http://<ip>:8000/health`; open the port |
| upload returns `401` | token mismatch | set the same `PROCTOR_TOKEN` on both sides |
| `merge failed` / `ffmpeg ... not found` | ffmpeg missing on server | install ffmpeg, ensure it's on `PATH` |
| `recording not ready` (404) | no contiguous chunks merged yet | wait for chunk 0+; check `/status` `contiguousChunks` |
| recording shorter than expected | a gap (missing chunk) | check `lastReceived` vs `lastMerged`; the gap chunk never arrived |
| chunks pile up in `client_queue/` | network was down, or drain timed out | they upload automatically on the next client run |

---

## 15. Scaling notes

Mapped to the roadmap's later phases — none are required for the prototype:

- **HTTPS (production):** the prototype uses plain HTTP for LAN testing. Put the
  server behind TLS (reverse proxy, or `uvicorn --ssl-keyfile/--ssl-certfile`).
  The client already sends a bearer token; just change the `--server` URL to
  `https://…`.
- **True H.264:** flip `chunk_fourcc` to `avc1` once OpenCV supports it, or
  capture via an ffmpeg subprocess for guaranteed H.264.
- **Incremental merge:** replace the full rebuild in `merger.py` with an append
  so merge cost stays O(1) per chunk during long exams / load testing.
- **Object storage / distributed servers:** only `server/storage.py` and
  `server/merger.py` touch the filesystem — reimplement those against S3 or a
  shared store without changing the client or the API.
- **Per-student tokens:** replace the single shared secret with tokens issued at
  exam start (the header contract already supports it).
- **Live dashboard / AI detection / screen + multi-camera:** additive features
  from the roadmap's "future improvements" — the chunked-upload backbone here is
  the foundation they build on.
```

