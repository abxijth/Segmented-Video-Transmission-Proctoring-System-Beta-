# COMMANDS — everything you need to run, build, and finalize

One-stop command reference. For the *why* behind each piece see
[README.md](README.md), [DOCUMENTATION.md](DOCUMENTATION.md),
[PACKAGING.md](PACKAGING.md), and [DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md).

Legend: `<SERVER_IP>` = the proctor laptop's LAN IP
(`ip -4 addr` on Linux, `ipconfig` on Windows, `ipconfig getifaddr en0` on macOS).

---

## 0. One-time setup (from source)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

ffmpeg must be on PATH to run from source (the *packaged* app bundles its own):

```bash
ffmpeg -version                    # any recent build with an H.264 encoder
```

---

## 1. Run the SERVER (proctor laptop)

```bash
python -m server.main              # serves on http://0.0.0.0:8000
```

Optional environment overrides:

```bash
PROCTOR_STORAGE=/srv/storage \
PROCTOR_PORT=8000 \
PROCTOR_TOKEN=our-shared-secret \
python -m server.main
```

Check it's up (from the client machine too):

```bash
curl http://<SERVER_IP>:8000/health
```

---

## 2. Run the CLIENT (student laptop, from source)

```bash
python -m client.main --server http://<SERVER_IP>:8000 \
                      --exam exam2026 --student student001
```

Stop with **Ctrl+C** — it drains its queue; the server finalizes on its own.

Client flags / env / `proctor.ini` keys (CLI > env > ini > baked default):

| What | Flag | Env | ini key |
|---|---|---|---|
| Server URL | `--server` | `PROCTOR_SERVER` | `server` |
| Exam id | `--exam` | `PROCTOR_EXAM` | `exam` |
| Student id | `--student` | `PROCTOR_STUDENT` | `student` |
| Camera index | `--camera` | — | `camera` |
| Chunk length (s) | `--chunk-seconds` | — | `chunk_seconds` |
| Disable audio | `--no-audio` | `PROCTOR_AUDIO=0` | `audio = 0` |
| Mic device | `--audio-device` | `PROCTOR_AUDIO_DEVICE` | `audio_device` |
| Auth token | — | `PROCTOR_TOKEN` | — |

Audio is **on by default** (mic → AAC, muxed into every chunk). Examples:

```bash
python -m client.main --server http://<SERVER_IP>:8000 --exam exam2026 --student s1   # with audio
python -m client.main --no-audio ...                                                  # video only
python -m client.main --audio-device "Microphone (Realtek Audio)" ...                 # pin a mic (Windows)
```

> **Mic permission:** Windows → Settings → Privacy → Microphone → allow desktop
> apps. macOS → Settings → Privacy & Security → Microphone. If denied, the log
> says `no usable microphone; recording video only`.

---

## 3. Test everything on ONE laptop

```bash
# terminal 1
python -m server.main
# terminal 2
python -m client.main --server http://127.0.0.1:8000 --exam exam2026 --student student001
```

---

## 4. Build the standalone app

One build per OS (PyInstaller does not cross-compile).

**Local:**

```bash
# Windows (Command Prompt, repo root)  ->  dist\ProctorClient.exe
build\build_windows.bat

# Linux                                ->  dist/ProctorClient
bash build/build_linux.sh

# macOS                                ->  dist/ProctorClient.app (+ dist/ProctorClient-macos.zip)
bash build/build_linux.sh
```

The build scripts fetch a static ffmpeg into `vendor/` and bundle it — the
shipped app has **no external dependencies**.

**GitHub Actions (builds all three, incl. the Windows .exe):**

```bash
git push -u origin <branch>
gh workflow run build-client.yml --ref <branch>     # or push a v* tag
gh run watch
gh run download                                     # grab the artifacts
```

No `gh` CLI → **Actions tab → Build Proctor Client → Run workflow → pick branch**.

---

## 5. Run the packaged app

Put a `proctor.ini` next to the executable / `.app` (copy `proctor.ini.example`):

```ini
[client]
server = http://<SERVER_IP>:8000
exam   = exam2026
student = student001
audio  = 1
```

- **Windows:** double-click `ProctorClient.exe` (a console shows status).
- **macOS:** unzip, clear Gatekeeper once, then open:

```bash
xattr -dr com.apple.quarantine ProctorClient.app
open ProctorClient.app
```

Debug a macOS run (see live logs the background app hides):

```bash
"ProctorClient.app/Contents/MacOS/ProctorClient" --server http://<SERVER_IP>:8000 --exam exam2026 --student test1
```

> macOS also needs **Local Network** permission to reach a LAN server:
> Settings → Privacy & Security → Local Network → enable ProctorClient.

---

## 6. Get the results

```bash
# status (counts, whether a recording exists)
curl http://<SERVER_IP>:8000/exams/exam2026/student001/status

# download the on-demand merged recording
curl -O http://<SERVER_IP>:8000/exams/exam2026/student001/recording.mp4

# list every recorded session
curl http://<SERVER_IP>:8000/exams
```

On-disk layout:

```
storage/<exam>/<student>/
    recording.ts          MPEG-TS accumulator (internal; grows during exam)
    recording.mp4         fast stream-copy, built on download
    recording_final.mp4   phone-safe re-encode (after step 7)
    metadata.json
```

---

## 7. Post-exam: convert TS → phone-safe MP4 (with audio)

Run **once after the exam**, on the server (needs ffmpeg with an H.264 encoder).
Re-encodes every `recording.ts` into a clean constant-frame-rate
`recording_final.mp4` (regenerated timestamps + re-synced audio) that plays
correctly on phones — fixing the stutter / too-fast playback the raw
`recording.mp4` can show.

```bash
# convert the whole storage tree
python tools/finalize_recordings.py

# more parallel encoders (CPU-heavy)
python tools/finalize_recordings.py --jobs 4

# replace recording.mp4 in place instead of writing recording_final.mp4
python tools/finalize_recordings.py --overwrite

# just one session, or one exam
python tools/finalize_recordings.py --exam exam2026 --student student001
python tools/finalize_recordings.py --exam exam2026

# point at a custom storage root; preview commands without encoding
python tools/finalize_recordings.py --storage /srv/storage --dry-run
```

Confirm a finalized file has audio:

```bash
ffprobe -v error -show_entries stream=codec_type -of csv=p=0 recording_final.mp4
# prints:  video   and   audio     (audio only if a mic was recorded)
```

Options: `--fps 30` (output rate), `--crf 23` (quality, lower=bigger),
`--preset medium`, `--output recording_final.mp4`, `--jobs 2`.

---

## 8. Quick diagnostics

```bash
# is the server reachable from the client machine?
curl http://<SERVER_IP>:8000/health

# what encoder / mic did the client pick? (run from source or the mac binary)
#   look for:  [recorder] using H.264 encoder: libx264
#              [recorder] microphone enabled (AAC audio in each chunk)

# does a chunk / recording actually contain audio?
ffprobe -v error -show_entries stream=codec_type,codec_name -of default=nw=1 <file>

# is the audio SILENT? (mean/max ~ -84..-91 dB = digital silence, mic captured nothing)
ffmpeg -i <file> -af volumedetect -f null -

# audio present but silent -> the mic opened but captured nothing. Fix by device:
#   macOS   : System Settings > Privacy & Security > Microphone > enable the app
#   Windows : Settings > Privacy > Microphone > allow desktop apps; unmute
#   Linux   : default source may be a monitor/muted -> pick a real input:
pactl list sources short           # find a non-.monitor source name
python -m client.main --audio-device <source-name> ...   # Linux
#   (Windows: --audio-device "Microphone (Realtek Audio)"; macOS: --audio-device 0)

# find your LAN IP
ip -4 addr            # Linux
ipconfig              # Windows
ipconfig getifaddr en0  # macOS
```
