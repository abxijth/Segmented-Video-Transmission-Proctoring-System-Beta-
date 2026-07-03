# Packaging the client as a standalone executable

The client can be bundled into a **single self-contained executable** with
PyInstaller — no Python install, no `pip`, no dependencies needed on the
student's machine. The bundle includes the Python runtime, OpenCV, NumPy, and
`requests`.

## ffmpeg is bundled automatically — students install nothing

The client encodes H.264 video (and captures **microphone audio**, muxed as
AAC) with **ffmpeg**, so it must travel *inside* the exe. **The build handles
this for you** — you do not download or install ffmpeg manually, and neither do
the students:

- `build\build_windows.bat` / `build/build_linux.sh` run a fetch step
  (`build/fetch_ffmpeg.ps1` / `build/fetch_ffmpeg.sh`) that downloads a **static
  ffmpeg** (with libx264) into `vendor/` before packaging.
- The GitHub Actions workflow does the same on each OS runner.
- The `.spec` then bundles `vendor/ffmpeg[.exe]` into the executable;
  `client/media.py` finds it inside the bundle at runtime.

Result: **a single self-contained exe with zero external dependencies.** If you
ever build with the raw `pyinstaller` command and forget to fetch ffmpeg, the
spec prints a loud warning (the exe would then only work where ffmpeg is on
PATH). To fetch it yourself: `powershell -ExecutionPolicy Bypass -File
build\fetch_ffmpeg.ps1` (Windows) or `bash build/fetch_ffmpeg.sh` (Linux/macOS).

> The static gyan.dev / johnvansickle / evermeet builds used by the fetch
> scripts already include **libx264**. The client also auto-detects the encoder
> at runtime (`libx264` → `libopenh264` → hardware), so it still runs if a
> machine only has a system ffmpeg without libx264 (e.g. Fedora's `ffmpeg-free`,
> which ships `libopenh264`).

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

Create a clean virtualenv, install `requirements-build.txt`, **fetch a static
ffmpeg into `vendor/`**, then run:

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
4. **baked defaults** — nothing prompts; just double-click. An unset student id
   falls back to the machine hostname, so it always runs unattended.

A typical classroom setup: ship the exe with a `proctor.ini` that pins
`server`, `exam`, and each contestant's `student` id.

```ini
[client]
server = http://192.168.1.50:8000
exam = exam2026
student = student001
camera = 0
chunk_seconds = 5
audio = 1          ; mic audio (AAC) into every chunk; set 0 for video only
```

Microphone audio is on by default and muxed into each chunk; set `audio = 0`
(or `--no-audio` / `PROCTOR_AUDIO=0`) for video-only. The recording queue is
written to a `client_queue/` folder **next to the exe**, so chunks survive a
crash and resume on the next launch.

### Windows: microphone privacy

Audio is captured via DirectShow. Windows 10/11 gates mic access behind a
privacy switch — turn on **Settings → Privacy & security → Microphone → "Let
desktop apps access your microphone"**, or the client logs `no usable
microphone` and records video only.

### macOS: camera & microphone permission

The macOS build is a `.app` bundle whose `Info.plist` declares
`NSCameraUsageDescription` and `NSMicrophoneUsageDescription` — **required**, or
macOS silently denies capture. On first launch the OS prompts the user to grant
camera and mic access; the audio probe at startup triggers the mic prompt early.
Because the app is only ad-hoc signed (not Apple-notarized), the user must also
clear Gatekeeper once — see the Gatekeeper note in the distribution checklist.

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
