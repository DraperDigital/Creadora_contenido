# Contenido Bionico V6

> **New here?** Read [START-HERE.md](START-HERE.md) — the plain-language,
> step-by-step setup guide.

A short-form video factory built around a local web dashboard: reachable from
your phone or any device on your home WiFi, it's where you upload raw
vertical videos, a self-hosted engine and pipeline running on this same PC
pull each job and do the editing, and every deliverable (the short,
quote-post mp4s, carousel PNGs, caption + metadata) uploads back to the
dashboard for preview, download, and further edits. No cloud account is
needed to run any of this.

## Repository layout

```
pipeline/            # The video-editing pipeline (Python + Node/Remotion + audio_library)
server/
  local/             # LAN dashboard: the same Worker code running on Node (SQLite + filesystem)
  cloud/             # dashboard logic + UI; also deployable to Cloudflare (optional)
  engine/            # Self-hosted host: the pull agent + job watcher that drive the pipeline
.env                 # Secrets (API keys, agent token) — gitignored
AGENTS.md            # Install/run playbook injected into the AI assistant
```

The pipeline reads two long-lived working areas (created at run time, gitignored):

```
server/engine/runtime/_work/   # Agent + per-job working state and logs
pipeline/runs/                 # Per-run pipeline working folders (raw.mp4, intermediates, logs)
pipeline/output_short/         # Finished deliverables: output_short/run_<n>/ (final video, carousel, quotes)
```

## How it works

1. Upload one or more raw vertical videos in the dashboard, from your phone
   or any device on the same WiFi.
2. The engine's agent claims jobs one at a time and hands each to the pipeline
   (`contenido-bionico <mp4> --short`), gated by system load.
3. The pipeline cuts the talk (editorial cut + silence/stumble removal), adds
   word-level captions, camera moves, mid-clip animations, music and
   SFX, then builds the secondary deliverables (carousel + quote posts).
4. Each deliverable uploads back to the dashboard; every job row is a
   downloadable, re-editable version.

Uploads carry per-job feature toggles (subtitles / camera / animations /
music / SFX / carousel / quotes), an animation-count tier, and an animation-
quality level; the editorial cut is always on. Requesting a change on a finished
deliverable forks its run and re-publishes only what changed. See
[`server/local/README.md`](server/local/README.md) for the dashboard, its data
model, and daily ops (or [`server/cloud/README.md`](server/cloud/README.md)
if you deploy the optional Cloudflare path instead).

## Prerequisites

- Python 3.11+ (with `venv`)
- **Node.js >= 22.5** + npm (the LAN dashboard needs `node:sqlite`; also runs
  the Remotion renderer in the pipeline)
- `ffmpeg` / `ffprobe` on PATH
- **ElevenLabs API key** with `speech_to_text` (word-level transcription drives the cut)
- **Anthropic API key** or an active Claude subscription via Claude Code (the AI editing agents)

## Install (manual)

This is a self-hosted setup — you provision this machine yourself, by hand.
There is no `bionico install`/`provision`/`doctor`; the only `bionico`
commands are orchestrator control (`skills`, `start`, `stop`, `restart`,
`status`, `autostart`). Make sure `ffmpeg`/`ffprobe` and Node.js/npm are on
PATH first, then:

```bash
# 1. Copy the repo folder to this machine (done manually).
# 2. Create the venv and install both Python packages editable.
python -m venv .venv
.venv/bin/pip install -e ./server/engine
.venv/bin/pip install -e ./pipeline
# 3. Install Node deps in the pipeline and the dashboard.
( cd pipeline && npm install --no-audit --no-fund --loglevel=error )
( cd server/cloud && npm install --no-audit --no-fund --loglevel=error )
# 4. Write the secrets by hand:
#      .env -> ELEVENLABS_API_KEY (+ ANTHROPIC_API_KEY for the api_key engine)
# 5. Log in the Claude subscription (skip for the Anthropic api_key engine).
claude   # complete its login
# 6. Install the AI-client skills, then start the stack.
.venv/bin/bionico skills
.venv/bin/bionico start --detach
```

On Windows (PowerShell) use `.venv\Scripts\python.exe`, `.venv\Scripts\pip.exe`,
`.venv\Scripts\bionico.exe`, and `Push-Location`/`Pop-Location` around each
`npm install`.

`bionico start` supervises the watcher, the local dashboard (`server/local`,
whenever `server/local/server.mjs` exists and `node` is on PATH), and the
pull agent — the dashboard bootstraps its own password and tokens and wires
the agent to itself automatically, so there is nothing else to configure by
hand for the local dashboard. Cloudflare-side provisioning (Worker, D1, R2,
secrets) and deploy are optional and live in
[`server/cloud/README.md`](server/cloud/README.md).

## Use from your phone

`bionico status` prints every dashboard URL: `http://127.0.0.1:8787` plus one
`http://192.168.x.x:8787` per network interface on this PC. Open any of the
LAN addresses from a phone or other device on the same WiFi. The login
password is auto-generated into `server/local/.env` on first start (and
printed there); change it by editing `DASHBOARD_PASSWORD` in that file, then
`bionico restart`.

On the very first start, Windows prompts to allow Node through the firewall —
choose **private networks**. To skip that prompt, pre-allow it yourself:

```powershell
netsh advfirewall firewall add rule name="Bionico Dashboard" dir=in action=allow protocol=TCP localport=8787 profile=private
```

## Branding

The dashboard's WhatsApp-mock deliverable format shows a chat header with a
name and photo: set the `BIONICO_WA_NAME` env var and drop a photo at
`pipeline/broll_library/pfp.jpeg` (or point `BIONICO_WA_PFP` at one
elsewhere); without a photo it falls back to a letter avatar. Regenerate the
quote-card template the same way:

```bash
.venv/Scripts/python.exe pipeline/scripts/make_quote_template.py \
    --name "Your Name" --handle "@yourhandle" [--avatar photo.jpg]
```

(Requires Pillow, already a pipeline dependency.) Photos dropped into
`pipeline/broll_library/` also activate the photo-backed deliverable formats
(carousel-over-photo, phrase-over-photo, faceless video, text-behind-person)
automatically — those formats simply skip while the folder is empty.

## Security

The dashboard speaks plain HTTP on your LAN. That's fine for a home or other
trusted network, but never port-forward it to the public internet.

## Ops

- **Start / stop the stack** (watcher + dashboard + agent): `bionico start` /
  `bionico stop` (add `--detach` to run in the background); `bionico status`
  shows each service and the dashboard URLs.
- **Cloudflare deploy (optional):** see
  [`server/cloud/README.md`](server/cloud/README.md).

## Configuration

Orchestrator settings live in `.bionico/config.json`; secrets live in `.env`
(API keys), `server/local/.env` (dashboard password/tokens), and — only if
you deploy the optional Cloudflare path — `server/cloud/.env`. All three are
gitignored. The local dashboard's own settings are documented in
[`server/local/README.md`](server/local/README.md#configuration); optional
pipeline/engine env flags (QA gates, timeouts, watchdog limits, notifications)
are listed in
[`pipeline/README.md`](pipeline/README.md#configuration-flags).
