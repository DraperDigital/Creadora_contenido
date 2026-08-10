# pipeline/ — the video editing pipeline

Python package `contenido_bionico` (in `src/contenido_bionico/`). Turns a raw
talking-head MP4 into a captioned, animated short with per-run intermediates
that stay editable after delivery. Install, setup, and orchestrator commands
live in the root [`AGENTS.md`](../AGENTS.md) — that file is the authoritative
playbook; this one only maps the pipeline code.

## Stage flow -> directories

| Stage | Directory | What it does |
| --- | --- | --- |
| 1. Transcribe + cut | `shared/cut/` | ElevenLabs transcription, agentic editor/reviewer cut, mapper + surgical patcher, EDL render of the working source. Surgical recut primitives for post-delivery cut edits. |
| 2. Animate | `short/animate/` | Plans scenes per kept segment; Claude agents author one Remotion `Scene.tsx` per scene from scratch, then a repair/QA pass. |
| 3. Audio | `shared/audio/` | Resolves author-selected SFX cues against `audio_library/MANIFEST.json`, picks background music, builds the mix plan. |
| 4. Assembly + render | `short/animate/assembly_render.py` + `shared/render_remotion.py` | Composites captions, camera moves, scenes, and audio into `final.mp4`; extras (quote post, carousel) render from `short/quote/` and `short/carousel/`. |
| 5. Formats (V6) | `short/formats/` | Registry-driven multi-format stage: ~30 extra deliverables re-rendered from the run's atoms (transcript, voice, captions, plans, brand palette, `broll_library/` photos) — extra videos, 6 video-carousels, carousel layout/ratio variants, quote videos, static images, text posts, SRT, per-category ZIP packs + `formats_manifest.json`. Best-effort: a format failure never fails the video. |

The formats stage is data-driven: `short/formats/registry.py` maps each format to
its producer, required atoms, output files, and dashboard category
(`videos`, `video_carruseles`, `carruseles`, `citas`, `imagenes`, `textos`).
Category toggles arrive from the dashboard as CLI flags
(`--no-videos-extra --no-video-carruseles --no-carruseles-extra --no-imagenes
--no-textos`; quote formats follow the existing quotes toggle). Operator photos
for the photo-backed formats live in `pipeline/broll_library/` (optional — those
formats skip cleanly when it is empty).

Post-delivery changes go through `short/change/`: a registry-driven
orchestrator (`registry.py` maps each editable attribute to the intermediate
that owns it) with surgical primitives (`primitives.py`, `cut_editor.py`) such
as `rerender_part` and `recompose` — never a full rebuild.

## Key per-run manifests (`runs/<id>/`)

- `transcript.json` — word-level transcript of the CUT working source; every
  later stage times against it.
- `_intermediates/edl_render.json` — the kept source ranges (the cut itself);
  surgical recuts edit this file and re-derive downstream.
- `Style_Tokens.json` — per-run palette/fonts/easing/motion tokens injected
  into the scene author and repair agents.
- `Short_Assembly.json` — the assembly manifest: segments, captions, camera,
  and scene wiring the final render composites.

Also present: `Assembly_Instructions.json` (segment timing for audio/extras),
`Audio_Plan.json` (resolved music + SFX cues), and `animations/<seg>/` (per-scene
TSX + `Sound_Cues.json`).

## Remotion tree

`src/contenido_bionico/shared/remotion/` is the single Remotion project. Shared
components (`src/Captions.tsx`, `src/lib/`) are the templates;
each run gets its own copies under `src/runs/<id>/` (scenes, plus a per-run
`Captions` copy when a change needs novel attributes) so surgical edits
stay isolated per run. Authored scenes are self-contained modules; any helpers
are inlined per run, never imported from shared code.

## Tests

Fast, dependency-free unit tests live in `tests/` (no ffmpeg, no network):

```
.venv\Scripts\python.exe -m pytest pipeline/tests -q
```

## Configuration flags

Optional env vars, read from the environment or the repo `.env`. Everything has
a production-safe default; nothing here is required. Scope `pipeline` = this
package; scope `engine` = the pull agent / job watcher in `server/engine`.

| Variable | Scope | Default | Effect |
| --- | --- | --- | --- |
| `BIONICO_VISUAL_QA` | pipeline | `1` (on) | Visual QA stills gate over each freshly rendered animated scene; `0` skips it. |
| `BIONICO_VOICE_CLEANUP` | pipeline | `0` (off) | Voice-cleanup chain (highpass + denoise + gentle compression) in the audio mix; `1` enables it. |
| `BIONICO_MIN_KEEP_RATIO` | pipeline | `0.35` | Kept-text floor for the cut: the attempt fails when `final.txt` keeps less than this fraction of the transcript's alignment units; `0` disables the check. |
| `BIONICO_CUT_REVIEWER_MODEL` | pipeline | `claude-sonnet-5` | Model id of the cut reviewer agent. |
| `BIONICO_ALLOW_FULL_REBUILD` | pipeline | unset (blocked) | A full pipeline rebuild of an existing run is refused unless this is `1`; cut changes go through `surgical_recut` instead. |
| `BIONICO_CHANGE_TIMEOUT_MINUTES` | pipeline | `60` | Wall-clock budget (minutes) for a change session (surgical/cut/look edits). |
| `BIONICO_CHANGE_CASCADE_TIMEOUT_MINUTES` | pipeline | `120` | Larger budget (minutes) for video edits over runs with animated scenes, which can cascade into scene re-authoring. |
| `BIONICO_PRIMITIVE_QUIET` | pipeline | `1` (on) | Change primitives capture verbose render/assemble/mix logs to `runs/<id>/logs/primitives/` and print one status line per part; `0` streams full logs. |
| `BIONICO_WA_NAME` | pipeline | `Contenido Biónico` | Display name in the WhatsApp-mock format's chat header. |
| `BIONICO_WA_PFP` | pipeline | `broll_library/pfp.jpeg` | Header photo path for the WhatsApp-mock format; absent file -> letter avatar. |
| `MAX_VIDEO_SECONDS` | engine | `420` | Preflight: uploads longer than this are rejected. |
| `BIONICO_STALL_MINUTES` | engine | `30` | Watcher: a running job whose log has not grown for this long is killed and marked failed. |
| `BIONICO_JOB_CEILING_HOURS` | engine | `3` | Watcher: wall-clock ceiling per job; a job that exceeds it is killed and marked failed. |
| `BIONICO_QUOTA_DEFER_PCT` | engine | `85` | Agent: above this five-hour Claude quota utilization %, no new jobs are claimed until the window resets. |
| `NTFY_TOPIC` | engine | unset | Operator alerts via `https://ntfy.sh/<topic>`; silent no-op when unset. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | engine | unset | Operator alerts via the Telegram Bot API (both required); silent no-op when unset. |

Worker-side vars (`NOTIFY_WEBHOOK_URL`, `OPERATOR_ADMIN_PASSWORD_HASH`,
`MAX_UPLOAD_MB`) are documented in the env table of
[`server/cloud/README.md`](../server/cloud/README.md).
