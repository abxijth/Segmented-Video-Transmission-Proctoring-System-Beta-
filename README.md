# SEB Webcam Proctoring — Prototype

A resilient webcam recording system: the **client** records the webcam into
short MP4 chunks and uploads them to a **server** running on another laptop.
The server stores every chunk and merges them into a single `recording.mp4`
per student. Designed to survive network drops (chunks queue on disk and
retry until acknowledged).

This is **Phase 1–8** of the roadmap as a working prototype. It is intentionally
small and readable so it can be scaled later.

```
 STUDENT LAPTOP                         PROCTOR LAPTOP / SERVER
┌───────────────────────┐   HTTP POST  ┌──────────────────────────┐
│ camera → chunks →     │ ───────────► │ FastAPI: store + merge   │
│ disk queue → uploader │ ◄─────────── │ → recording.mp4          │
└───────────────────────┘     ACK      └──────────────────────────┘
```

## Layout

```
shared/      protocol constants shared by client & server (chunk naming, etc.)
client/      capture, chunking, disk queue, upload-with-retry
server/      FastAPI app, chunk storage, ffmpeg merge service
```

## Requirements

- Python 3.10+
- ffmpeg on PATH (used by the server to merge chunks)
- A webcam on the client laptop

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (two laptops on the same network)

**1. On the proctor/server laptop** — note its LAN IP (e.g. `192.168.1.50`):

```bash
python -m server.main
# serves on http://0.0.0.0:8000
```

**2. On the student/client laptop:**

```bash
python -m client.main --server http://192.168.1.50:8000 \
                      --exam exam2026 --student student001
```

Stop the client with Ctrl+C. Recording is finalized automatically.

## Get the result

```bash
# list students / status
curl http://192.168.1.50:8000/exams/exam2026/student001/status

# download the merged recording
curl -O http://192.168.1.50:8000/exams/exam2026/student001/recording.mp4
```

## Notes for scaling later (mapped to the roadmap)

- **HTTPS**: the prototype uses plain HTTP for LAN testing. Put it behind TLS
  (reverse proxy or `uvicorn --ssl-*`) for production — the client already sends
  a bearer token.
- **H.264**: codec is configurable in `client/config.py` (`CHUNK_FOURCC`).
  Default `mp4v` is the most portable via OpenCV; switch to `avc1` once your
  OpenCV build has H.264 support, or move capture to an ffmpeg subprocess.
- **Object storage / distributed servers**: only `server/storage.py` and
  `server/merger.py` touch the filesystem — swap those out without changing
  the client or the API.
```

