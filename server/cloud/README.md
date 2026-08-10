# Bionico Cloud Dashboard

> **Optional path.** The product runs fully locally by default — `server/local/`
> serves this same dashboard on your LAN with zero Cloudflare dependency. Deploy
> to Cloudflare only if you want the dashboard reachable from outside your home
> network. Local mode writes its managed `DASHBOARD_BASE_URL`/`AGENT_TOKEN`
> block in this directory's `.env` only when the file is absent or already
> carries its `# managed-by: server/local` marker; a hand-written
> `DASHBOARD_BASE_URL` (your Cloudflare URL) is never overwritten. To return to
> local mode, delete the `DASHBOARD_BASE_URL`/`AGENT_TOKEN` lines — the next
> `bionico start` regenerates the managed block.

Cloudflare front door for the bionico pipeline. Upload from anywhere (multiple
files at once); this machine's agent pulls jobs one at a time, the watcher
edits them, and every deliverable of the run uploads back: the short video,
the quote-post mp4s, the carousel PNGs, the caption and metadata — all
downloadable per file from the job's row.

## v3 panel (Spanish, client-facing)
- Single "Subir videos" button (multi-select, auto-start, per-file ✕ +
  "Cancelar todo"); per-file sizes shown.
- Live pipeline stage per job (Quitando silencios → … → Exportando), ticking
  elapsed time, tokens spent per job, per-file download sizes.
- Cancel at any point (queued cancels instantly; running jobs are killed on
  the engine machine). Cancel during the final upload lets the job finish —
  a nearly-done render is never thrown away.
- Delete per row + "Borrar todo" (terminal rows only; R2 files purged).
- "Calidad de animación" selector (Baja/Media/Alta/Máxima) — affects ONLY the
  scene animation agents (low=sonnet-5/low, mid=opus/medium=default,
  high=opus/high, max=opus/max).
- Account usage meters (5 h / 7 d, % used) fed by the agent every 5 min from
  the engine machine's Claude session; hidden if stale (>30 min).
- Logout button. Every video now ALWAYS produces at least one quote post
  (hard floor + retry in the selection agent).

## v5 panel — pipeline toggles, animation count, preview-first deliverables
- Feature toggles in the upload area, in two labeled sections (all on by
  default, snapshotted per job):
  - **Video** — `Subtítulos` / `Cámara` / `Animaciones` / `Música` /
    `SFX`. The editorial cut is always on and is not shown as a toggle (it can
    never be disabled). A no-camera run uses a neutral static camera; captions
    are made optional in the ffmpeg assembly;
    `Música` and `SFX` gate the two audio-post-process layers independently
    (both off = final.mp4 stays voice-only).
  - **Entregables** — `Carrusel` / `Quote posts`. These gate the two secondary
    deliverables built after the video (`build_short_extras`); off means that
    deliverable isn't produced. `Quote posts` off skips the quotes step
    entirely (the ≥1-quote floor only applies when quotes are on).
  Each toggle maps to a `--no-<x>` pipeline flag; all seven are independent
  (e.g. cut+subtitles only, or video with no carousel/quotes).
- `Cantidad de animaciones` selector: `Pocas` (≤2 scenes) / `Normal` (≤3) /
  `Máximas` (≥3, no cap). Injected into the scene planner + clamped in code;
  the clamp is skipped on edits so a cached plan never loses scenes.
- Snapshot rules: a new upload freezes the current toggles + count + quality
  onto the job. An edit RE-READS quality (bump effort for an edit) but CARRIES
  FORWARD the job's own toggles + count (an edit never restructures the video).
  Legacy jobs (no snapshot) are treated as all-on/default everywhere.
- Deliverables are preview-first: each row shows buttons only — `Video`,
  `Quote N`, `Carrusel N`, `Texto` — and clicking one opens a preview
  (player for video/quotes, image for slides, text for the caption) with
  `Descargar` + `Cambios…` inside. `Descargar todo` and `Borrar` sit in the
  row actions, each behind a Sí/No confirmation.

## v8 panel — versioned edits (version history)
Editing no longer overwrites a video. Each `Cambios…` request produces a new
**version** kept as its own row inside the same **card** (one card = one
original upload + all its edits, grouped by `lineage_id`; the card header is the
filename). Every version is independently downloadable, re-editable (branch a
new change from ANY version, including the original), and deletable.
- **Per-version metering:** each row shows its OWN elapsed time + tokens — the
  cost of producing THAT version only, never cumulative.
- **Pinpointed reuse (the fork):** an edit forks the parent's run — `fork_run`
  copies `runs/<parent>` → a fresh run id EXCLUDING `logs/` (so the new
  version's token sum counts only its own agent calls) and copies the parent's
  `output_short/run_<n>` so unchanged deliverables carry over (the final video
  is renamed to the new run number). Then ONLY the edited format is
  regenerated: a video edit re-animates just the touched scenes and reuses the
  forked carousel/quotes (`--edit-run … --fork`, `build_extras` skipped); a
  quotes/carousel edit regenerates just that format (`--quotes-run` /
  `--carousel-run … --fork`). The original run is never touched, so its version
  stays intact.
- **Per-version delete:** deleting a version removes its row + its R2 files and
  queues its run id for the engine to reclaim (D1 `reclaims` table → the agent
  drains it via `contenido-bionico reclaim-run <id>`, which drops that version's
  `runs/<id>/` + `output_short/run_<n>/`). Deleting one version never touches
  its siblings (each has its own forked run + outputs). `Borrar todo` unchanged.

## Debugging a server (owner-only, invisible to clients)
- `GET /api/debug/jobs/<id>` (logged-in session) → full row + event trail
  (created/claimed/status:stage/complete/cancel/… with timestamps).
- D1 `events` table keeps 30 days; `settings` holds anim_quality +
  usage_metrics.
- Engine machine: `server/engine/runtime/_work/agent/agent-events.jsonl` (agent
  event log), `server/engine/runtime/_work/jobs/*.log` (per-job pipeline logs),
  `pipeline/runs/<id>/logs/agent-calls/*.stream.jsonl` (per-agent-call
  transcripts incl. token usage). Finished deliverables land in
  `pipeline/output_short/run_<n>/` (final video + carousel + quotes) before they
  upload back.

## Request changes (LLM change orchestrator)
Preview a finished version's deliverable (Video / Quote N / Carrusel N / Texto)
and hit `Cambios…`; type what you want in plain language. The request goes to a
**change-orchestrator agent** (Opus 4.8, medium; standalone prompt at
`pipeline/.../short/change/agents/change_orchestrator.md`) scoped to the
deliverable you opened. It decides which pipeline op(s) satisfy the request and
runs them on a fresh fork (the previous version stays intact):
- **Video** — `reanimate` (re-author only the touched scenes), `set_music`
  (swap the song), `set_music_volume` (louder/quieter/mute). *`retrim` and
  caption edits are recognized but report "todavía no puedo" — next increment.*
- **Carrusel** / **Quotes** — regenerate that deliverable with your notes.

**Delta publish:** only the deliverable that changed is re-uploaded to the new
version (a music edit → just the video; a carousel edit → just the slides), so a
change no longer re-uploads the whole set. A request the tools can't satisfy
fails that version with the reason instead of doing the wrong thing. Changes need
the edited version's run to still exist on the engine (`runs/` + its output
folder). Effort matrix: all short agents run at medium effort except the cut
**editor + reviewer** (max); the animation-quality selector maps author/repair
to low / medium / max (`high → max`).

## URLs & credentials
- Dashboard: value of `DASHBOARD_BASE_URL` in `server/cloud/.env` (gitignored).
- Owner login: password chosen at provisioning (hash stored as Worker secret).
- All Cloudflare credentials for this project live in `server/cloud/.env`.

## Worker environment (secrets & vars)
| Name | Kind | Default | What it does |
| --- | --- | --- | --- |
| `OWNER_PASSWORD_HASH` | secret | required | sha256 hex of the customer login password. |
| `OPERATOR_ADMIN_PASSWORD_HASH` | secret | unset | sha256 hex of the OPERATOR password. Logging in with it returns `{"admin":true}` and the session carries the admin claim. While unset, the owner password keeps full admin powers (legacy fallback). Admin-only: `PUT /api/settings`, `POST /api/server/restart`, `POST /api/jobs/delete-all`, `POST /api/admin/logout-all`, `GET /api/debug/jobs/<id>`, plus the `tokens_total` field in job lists and `usage_metrics` in settings. |
| `SESSION_SECRET` | secret | required | HMAC key for session cookies. The MAC also covers the matching password hash and a server-side generation counter, so rotating a password or bumping the counter (`POST /api/admin/logout-all`) invalidates all outstanding sessions. Deploying this version invalidates existing sessions once. |
| `AGENT_TOKEN` | secret | required | Bearer token for the PC agent (compared timing-safe). |
| `MAX_UPLOAD_MB` | var | `2048` | Server-side cap on the declared upload size; a bigger `size_bytes` gets a 413 at job creation. |
| `NOTIFY_WEBHOOK_URL` | var/secret | unset | If set, the Worker fire-and-forgets a POST `{"title","message","jobId","status"}` to this URL whenever a job transitions to `done`/`failed`/`rejected`. Silent no-op when unset. |
| `ACCOUNT_ID`, `BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | vars/secrets | required | R2 presigning. |

Login is rate limited: 5 failed attempts from one IP (`CF-Connecting-IP`) lock
it out for 15 minutes; a successful login clears the counter.

Uploads accept `.mp4`, `.mov` and `.m4v` filenames (or any declared `video/*`
content type, keyed as `.mp4`). The customer input object is KEPT in R2 after
a job completes (re-edits/retries can reach it); the 30-day cron expiry
collects it together with the rest of the job's files.

## Daily ops
| Action | Command |
| --- | --- |
| Deploy after code changes | `cd server/cloud && npx wrangler d1 migrations apply bionico --remote && npx wrangler deploy` (or `npm run deploy`) |
| Live Worker logs | `cd server/cloud && npx wrangler tail` |
| Cloud tests | `cd server/cloud && npm test` |
| Python tests | `.venv/Scripts/python -m pytest server/engine/tests -v` |
| Rotate owner password | sha256-hex the new password (the format in the Worker env table above) → `npx wrangler secret put OWNER_PASSWORD_HASH` |
| Rotate agent token | `npx wrangler secret put AGENT_TOKEN` + update `server/cloud/.env` + `bionico start` |
| DB peek | `npx wrangler d1 execute bionico --remote --command "SELECT id,status,filename FROM jobs ORDER BY created_at DESC LIMIT 20"` |
| Set/verify bucket CORS | REST API (see cors.json; `wrangler r2 bucket cors put` crashes on Windows) |

## Job states
`uploading → queued → claimed → processing → publishing → done` (`failed` +
Retry from the dashboard). A "request changes" INSERTs a NEW version row
(`kind=edit`, chip shows `editando <format>`) in the same `lineage_id`; the
parent version stays `done` and downloadable. A change the orchestrator cannot
apply ends as `rejected` (terminal, non-red; amber "No se pudo aplicar el
cambio" card with the reason — no requeue, deletable, cron-expired like
failed). During `processing` the agent may attach a small `stage_detail` JSON
(`{"scene":n,"total":t}`) to its status POSTs; job lists return it parsed so
the dashboard can render "Animando escena n de t". Cron (every 15 min)
requeues jobs whose heartbeat is stale by more than 30 min, purges uploads
abandoned >24 h, and after 30 days deletes done/failed/canceled/rejected rows
plus all their R2 files (inputs included).

## Deploy order (schema changes)
Apply migrations BEFORE deploying a Worker that uses new columns, then restart
the stack so the agent matches the Worker:
`npx wrangler d1 migrations apply bionico --remote && npx wrangler deploy`,
then `bionico restart`.

## Moving the engine to a VPS
Install the repo + run the installer on the VPS, copy root `.env` and
`server/cloud/.env`, log in the Claude subscription, `bionico start`. The dashboard
URL and all cloud state are unchanged. Run ONE agent at a time: stop the PC
stack (`bionico stop`) before starting the VPS one. (Simultaneous agents need
a claimed_by column — not implemented.)

## Known limitations
- One agent at a time (see above).
- Heartbeat pauses during very large transfers (cron threshold set to 30 min
  to compensate).
- An edit that produces zero quotes clears the previously published quote
  files for that run (re-run another edit to regenerate them).
