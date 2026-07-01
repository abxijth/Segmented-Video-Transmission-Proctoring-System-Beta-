# Packaging the client as a standalone executable

The client can be bundled into a **single self-contained executable** with
PyInstaller — no Python install, no `pip`, no dependencies needed on the
student's machine. The bundle includes the Python runtime, OpenCV, NumPy, and
`requests`.

## ffmpeg is required (bundle it)

The client encodes H.264 chunks with **ffmpeg**, so the machine running the exe
needs ffmpeg. Two options:

- **Bundle it (recommended for distribution):** download a static ffmpeg for the
  target OS and drop it in a `vendor/` folder at the repo root before building:
  - Windows → `vendor/ffmpeg.exe`  (from https://www.gyan.dev/ffmpeg/builds/)
  - Linux/macOS → `vendor/ffmpeg`   (from https://ffmpeg.org/download.html)

  The `.spec` bundles it into the exe automatically; `client/media.py` finds it
  inside the bundle at runtime. Nothing else to install on student machines.
- **Rely on PATH:** if ffmpeg is already installed on the student machines, you
  can skip `vendor/` — the client falls back to the ffmpeg on PATH.

> **The ffmpeg must have an H.264 encoder.** The client auto-detects one
> (`libx264` → `libopenh264` → hardware), so most builds work as-is. The
> exception is Fedora's default `ffmpeg-free`, which omits `libx264` for patent
> reasons — install `openh264` (`sudo dnf install openh264 ffmpeg-free`) or swap
> to RPM Fusion's full `ffmpeg`, or just bundle a static build via `vendor/`.
> The static gyan.dev / ffmpeg.org builds already include libx264.

## Important: one build per operating system

PyInstaller **does not cross-compile.** A build produces an executable only for
the OS it ran on:

| Build on… | You get… |
|---|---|
| Windows | `dist\ProctorClient.exe` |
| Linux | `dist/ProctorClient` |
| macOS | `dist/ProctorClient` |

To "run everywhere" you build the same spec on each OS. The easiest way to get
all three (especially the Windows `.exe` if you don't have a Windows PC) is the
included **GitHub Actions** workflow — see below.

## Option A — GitHub Actions (recommended, builds the Windows .exe for you)

`.github/workflows/build-client.yml` builds Windows, Linux, and macOS
executables on GitHub's runners.

1. Push this repo to GitHub.
2. Go to the **Actions** tab → **Build Proctor Client** → **Run workflow**
   (or push a tag like `v0.1`).
3. When it finishes, download `ProctorClient-windows` (and the others) from the
   run's **Artifacts** section. That zip contains `ProctorClient.exe`.

## Option B — build locally

### Windows (produces the .exe)

From a Command Prompt at the repo root:

```bat
build\build_windows.bat
```

→ `dist\ProctorClient.exe`

### Linux / macOS

```bash
bash build/build_linux.sh
```

→ `dist/ProctorClient`

### What the scripts do

Create a clean virtualenv, install `requirements-build.txt`, then run:

```bash
pyinstaller --clean --noconfirm proctor-client.spec
```

> **Tip — smaller exe:** install [UPX](https://upx.github.io/) and put it on
> PATH before building; the spec already enables UPX compression
> (`upx=True`). Without UPX the bundle is ~90–100 MB (OpenCV is large); with
> UPX it shrinks substantially.

## Running the executable

The exe resolves its settings from, in priority order:

1. **command-line flags** —
   `ProctorClient.exe --server http://192.168.1.50:8000 --exam exam2026 --student student001`
2. **environment variables** — `PROCTOR_SERVER`, `PROCTOR_EXAM`, `PROCTOR_STUDENT`
3. **`proctor.ini`** placed next to the exe (copy `proctor.ini.example`)
4. **interactive prompts** — just double-click; it asks for anything missing.

A typical classroom setup: ship the exe with a `proctor.ini` that pins
`server` and `exam`, and let each student type only their own student ID at the
prompt.

```ini
[client]
server = http://192.168.1.50:8000
exam = exam2026
camera = 0
chunk_seconds = 5
```

The recording queue is written to a `client_queue/` folder **next to the exe**,
so chunks survive a crash and resume on the next launch.

## Distribution checklist

- [ ] Build the `.exe` (Actions or `build_windows.bat`).
- [ ] Put `proctor.ini` next to the `.exe` with your server IP and exam id.
- [ ] Make sure the student machine can reach the server
      (`http://<server-ip>:8000/health`) and that the webcam works.
- [ ] First launch may trigger a Windows SmartScreen / antivirus prompt because
      the exe is unsigned — for wide distribution, code-sign it.

## Notes / limitations

- **Unsigned binaries** may be flagged by SmartScreen/Gatekeeper/antivirus.
  Code-sign for production distribution.
- **macOS** bundles built on one CPU arch (Intel vs Apple Silicon) run best on
  that arch; build on each, or use a universal2 Python.
- The **server** is not packaged — it runs from source on the proctor machine
  (`python -m server.main`). Only the client is distributed as an exe.
