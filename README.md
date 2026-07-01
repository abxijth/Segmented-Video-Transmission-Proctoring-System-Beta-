# SEB Webcam Proctoring

A resilient webcam recording system. The **client** records the webcam into
short **H.264** chunks and uploads them over HTTP to a **server** running on
another laptop. The server stores every chunk and a **background worker** merges
them into one recording per student — and auto-finalizes even if the client
disconnects. Chunks queue on disk and retry until acknowledged, so a network
drop never loses footage.

```
 STUDENT LAPTOP                              PROCTOR LAPTOP / SERVER
┌────────────────────────────┐   HTTP POST  ┌────────────────────────────────────┐
│ camera → ffmpeg (H.264)    │ ───────────► │ FastAPI: store chunk, return fast  │
│  → disk queue → uploader   │ ◄─────────── │            │                       │
└────────────────────────────┘     ACK      │   MergeWorker (background thread)  │
                                            │   chunks → recording.ts (append)  │
                                            │   recording.ts → recording.mp4    │
                                            │            (built on download)     │
                                            └────────────────────────────────────┘
```

See **[DOCUMENTATION.md](DOCUMENTATION.md)** for the full architecture, the
H.264 + MPEG-TS merge strategy, code walkthrough, API reference, and scaling
notes. See **[PACKAGING.md](PACKAGING.md)** to build the standalone `.exe`.

## Layout

```
shared/      wire contract shared by client & server (chunk naming, headers, metadata)
client/      camera capture, H.264 chunking (ffmpeg), disk queue, upload-with-retry
server/      FastAPI app, chunk storage, MergeWorker, TS-based merger
```

## Requirements

- Python 3.10+
- **ffmpeg on PATH** — client uses it to encode H.264, server to merge
- A webcam on the client laptop

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

Both processes must be alive at the same time (order doesn't matter — the client
survives the server being down and flushes its queue when it comes back).

**1. Server** — on the proctor laptop. Note its LAN IP (find it with
`ip -4 addr` on Linux / `ipconfig` on Windows):

```bash
python -m server.main
# serves on http://0.0.0.0:8000
```

**2. Client** — on the student laptop:

```bash
python -m client.main --server http://<SERVER_IP>:8000 \
                      --exam exam2026 --student student001
```

> The server URL is **baked into `client/config.py`** as `DEFAULT_SERVER_URL`,
> so `--server` is optional once that matches your network. Give each student a
> unique `--student` id. Stop the client with **Ctrl+C** — it drains its queue,
> and the server finalizes the recording on its own.

**Test both on one laptop** (two terminals):

```bash
# terminal 1
python -m server.main
# terminal 2
python -m client.main --server http://127.0.0.1:8000 --exam exam2026 --student student001
```

## Get the result

```bash
# status: counts, lastMerged, whether a recording exists
curl http://<SERVER_IP>:8000/exams/exam2026/student001/status

# download the merged recording (built from the TS accumulator on demand)
curl -O http://<SERVER_IP>:8000/exams/exam2026/student001/recording.mp4

# list every recorded session
curl http://<SERVER_IP>:8000/exams
```

Videos live on disk under:

```
storage/<exam>/<student>/
    chunks/               transient — merged chunks are deleted
    recording.ts          MPEG-TS accumulator (grows during the exam)
    recording.mp4         built on demand from the TS at download time
    metadata.json         status, lastReceived, lastMerged, lastChunkAt, ...
```

## Configuration

| Setting        | Where                         | Default                     |
|----------------|-------------------------------|-----------------------------|
| Server URL     | `--server` / `PROCTOR_SERVER` / `proctor.ini` / baked default | `client/config.py` |
| Exam / student | `--exam` / `--student` (prompted if unset) | `exam2026` / prompt |
| Chunk length   | `--chunk-seconds`             | `5`                         |
| Auth token     | `PROCTOR_TOKEN` (both sides)  | `prototype-shared-secret`   |
| Storage root   | `PROCTOR_STORAGE` (server)    | `./storage`                 |
| Merge interval | `PROCTOR_MERGE_INTERVAL` (server) | `10` s                  |
| Stale timeout  | `PROCTOR_STALE_AFTER` (server) | `60` s (auto-finalize)     |

## Scaling notes

- **HTTPS**: prototype uses plain HTTP for LAN testing. Put it behind TLS
  (reverse proxy or `uvicorn --ssl-*`) for production — the client already sends
  a bearer token.
- **Throughput**: uploads are handled by a sync route in Starlette's threadpool,
  so students upload in parallel; a single background worker does the merging off
  the request path. For a real ~300-student exam use a wired gigabit link, a
  static server IP, hundreds of GB of storage, and multiple uvicorn workers.
- **Object storage / distributed servers**: only `server/storage.py` and
  `server/merger.py` touch the filesystem — swap those out without changing the
  client or the API.
