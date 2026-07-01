# Online Deployment Plan — Distributing the Proctoring Client to Contestants

This document is the roadmap for taking the working LAN prototype (two laptops
on the same WiFi) to a real deployment: **online contestants**, each running
the recorder inside **Safe Exam Browser (SEB)**, streaming to a public server.

It answers the operational questions that code alone doesn't: where student
identity comes from, how the client is distributed, how SEB launches and
force-quits it, and what infrastructure and legal groundwork is required.

> The recording pipeline itself (H.264 chunks → disk queue → upload → server-side
> MergeWorker → `recording.ts` → `recording.mp4`) **does not change**. See
> [DOCUMENTATION.md](DOCUMENTATION.md). Only the surrounding deployment does.

---

## 1. The core shift: LAN prototype → internet deployment

Everything today assumes a trusted local network. Online contestants break three
assumptions, and these three changes are **non-negotiable**:

| Today (LAN prototype)            | Needed (online)                                        |
|----------------------------------|--------------------------------------------------------|
| Baked-in IP `10.47.244.1`        | Public **domain + static IP** (a cloud VPS)            |
| Plain HTTP                       | **HTTPS/TLS** — webcam footage is PII; firewalls/SEB expect it |
| One shared token for everyone    | **Per-contestant token = their identity**              |

---

## 2. Where student details come from (identity)

Details are **not** collected inside the app. They are captured at
**registration** and turned into a credential.

1. Contestants register through the existing form (name, email, contest ID).
2. The system generates a **unique token per contestant** and stores
   `token → {name, email, studentId, examId}` in a small DB on the server.
3. The client sends that token. The server looks it up — **the token *is* the
   student's identity**. No login screen, no mistyped IDs, no impersonating
   another student by editing a URL path.

**So: "where do we get student details?" → the registration DB; the token is the
bridge.** This simultaneously replaces the shared secret with real per-student
auth. Today the path `/exams/exam2026/student009` is trusted as-typed; online it
must be derived from the validated token instead.

---

## 3. How the client is distributed

Two artifacts reach each contestant:

- **The recorder** — the PyInstaller `.exe` with ffmpeg bundled (already built
  this way; runs with no install). Host as a download link or ship inside the
  SEB package. See [PACKAGING.md](PACKAGING.md).
- **A per-contestant config** carrying *their* token and the server URL.

Two clean ways to deliver the per-student config:

- **(a) Personalized `.seb` file (recommended).** Generate one per contestant
  with the token embedded, emailed from the registration system. Opening it
  launches SEB *and* the recorder with that student's identity. The `.seb` file
  is the natural per-student unit and SEB is built around it.
- **(b) Generic exe + per-student `proctor.ini`.** The client already reads a
  `proctor.ini` beside the exe (`[client]` section: `server`, `exam`,
  `student`). Email each contestant their ini. Simpler to build, more manual.

---

## 4. SEB integration — "runs in background, force-quits on exit"

This plan maps directly onto a real SEB feature.

In the SEB config tool, the **Applications** section registers an "additional
application" that SEB **launches on start** and **terminates on quit**:

1. SEB starts → launches `ProctorClient.exe` in the background.
2. Contestant takes the exam locked down inside SEB.
3. Contestant quits SEB (with the quit password) → SEB **kills the recorder** →
   the client drains its queue; the **server auto-finalizes** the recording even
   on an abrupt kill (the MergeWorker already finalizes disconnected clients
   after `PROCTOR_STALE_AFTER`).

Recommended SEB settings:

- **Quit password** — so contestants can't exit early unnoticed.
- **Require the recorder process** to be present to start/continue the exam.

Required code change: a **headless / no-console mode** so the recorder runs
invisibly under SEB (the spec is currently `console=True`).

---

## 5. Server infrastructure

- **Cloud VPS** with a domain + **Let's Encrypt TLS**, via a reverse proxy
  (Caddy or nginx) in front of uvicorn — free HTTPS termination.
- **Multiple uvicorn workers** for real upload concurrency.
- **Bandwidth & storage** (drives the camera settings): at 640×480 the footage
  is far lighter than 720p. Rough budget: **~1 GB per contestant for a 3-hour
  exam** at a modest bitrate → **~300 GB for 300 contestants**, with peak ingress
  of a few hundred Mbit/s. **Lower the default resolution/fps/bitrate** for
  online — a proctoring face-cam does not need 720p30.

---

## 6. Honest limitations (set expectations up front)

- **Contestants own their machines.** They can cover the camera, kill the
  process, or block uploads. SEB raises the bar (lockdown; can refuse to start
  without the recorder), but on hardware you don't control this is **deterrence +
  evidence, not airtight prevention**. The value is a reviewable recording plus
  gaps that flag suspicion.
- **Unsigned exe** → SmartScreen/antivirus warnings; some machines refuse to run
  it. **Code-signing** (a cert) largely fixes this and is worth it at scale.
- **Consent & legal.** Recording webcams is PII: you need explicit consent, a
  privacy policy, and a retention/deletion plan. Do not skip this.

---

## 7. Phased rollout

1. **Server public + HTTPS + token auth** — the three must-changes. Server
   change: map `token → identity` instead of trusting a shared secret + URL path.
2. **Headless exe** (no console window) + a per-student config/`.seb` generation
   script.
3. **SEB config** — wire the recorder as an additional application; set the quit
   password; pilot end-to-end with 2–3 people over the real internet.
4. **Load test** — simulate 50–100 concurrent uploaders against the VPS; tune
   workers, bitrate, storage.
5. **Scale + code-sign + consent flow**, then run.

**Highest-value first step:** Phase 1 (token-based identity + HTTPS-ready
server).

---

## 8. Open decisions (needed to turn phases into code)

- **Scale:** how many contestants; one exam or ongoing?
- **Registration:** is there already a registration system/DB, or does this need
  to generate and store tokens too?
- **Server:** is there a VPS/domain already, or is a recommendation needed?
