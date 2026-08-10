# Bionico Local Dashboard

The LAN counterpart to `server/cloud`: runs the exact same Worker code
(`server/cloud/src`) on plain Node instead of Cloudflare, so the dashboard
works fully on your own machine with no cloud account and no deploy step.
`bionico start` runs it automatically whenever `server/local/server.mjs`
exists and `node` is on PATH.

The dashboard's features, panel behavior, job lifecycle, and versioning
model are all identical to the Cloudflare deployment (it is the same code) —
see [`server/cloud/README.md`](../cloud/README.md) for those. This file only
covers what is specific to running it locally.

## Architecture

`server/local/server.mjs` is a small Node HTTP server that hosts the Worker
unmodified:

- **`/api/*`** — every request is converted to a `Request`/`Response` pair
  and handed to `server/cloud/src/index.js`'s `fetch()` handler, exactly like
  Cloudflare would call it. All routes, auth, job logic, and validation are
  the real Worker code, untouched.
- **D1 -> SQLite** — `lib/d1.mjs` implements the `prepare().bind().first()` /
  `.all()` / `.run()` surface the Worker expects, backed by `node:sqlite`
  (`DatabaseSync`, WAL journal mode). Migrations are the same `.sql` files in
  `server/cloud/migrations/`, applied once each and tracked in a
  `_migrations` table.
- **R2 -> filesystem** — `lib/r2fs.mjs` implements `head` / `get` / `delete`
  over files under `server/local/data/media/`, keyed the same way R2 objects
  are (`inbox/<job-id>/<file>`, `outbox/<job-id>/<file>`).
- **Presigned URLs -> HMAC-signed `/media` URLs** — instead of R2 presigning,
  uploads and downloads go through `/media/<key>?exp=<ts>&sig=<hmac>`, signed
  with `SESSION_SECRET`. Same expiry model, same single-`Range` support
  (resumable phone downloads).
- **Cron -> timer** — the Worker's 15-minute maintenance cron (stale-job
  requeue, abandoned-upload purge, 30-day retention) runs once at boot and
  then on a `setInterval(..., 15 * 60 * 1000)`, calling the same
  `worker.scheduled()` handler Cloudflare would call.
- **Static dashboard** — the SPA is served straight from
  `server/cloud/public/`, the same build Cloudflare's `[assets]` binding
  serves.
- **LAN-correct links** — signed media URLs are built from the request's
  `Host` header, so a phone that opened the dashboard via `192.168.x.x`
  receives `192.168.x.x` links back, never `127.0.0.1`.

## File map

```
server/local/
  server.mjs        # entrypoint: bootstrap, HTTP server (/api, /media, static dashboard), 15-min cron
  lib/d1.mjs         # D1-compatible shim over node:sqlite
  lib/r2fs.mjs       # R2-compatible shim over the filesystem
  .env               # generated secrets + your overrides (gitignored)
  data/              # runtime state (gitignored) — see "Data locations" below
  test/smoke.mjs     # full job-lifecycle smoke test
  test/lib.test.mjs  # unit tests for the d1/r2fs shims
```

## Configuration

`server/local/.env` (gitignored). Every variable is optional — first start
fills in the three generated ones automatically.

| Variable | Default | Notes |
| --- | --- | --- |
| `DASHBOARD_PASSWORD` | random 8-hex, auto-generated on first start | Login password (owner role). Generated on first start; read or change it in `server/local/.env` (then `bionico restart`). |
| `ADMIN_PASSWORD` | unset | Optional second role. When set, it is hashed into the Worker's admin secret and that login gets admin powers. While unset, `DASHBOARD_PASSWORD` itself has full admin powers (owner fallback). |
| `SESSION_SECRET` | random 64-hex, auto-generated | HMAC key for session cookies and for signing `/media` URLs. |
| `AGENT_TOKEN` | random 64-hex, auto-generated | Bearer token the pull agent uses against the dashboard's `/api/agent/*` routes; mirrored into `server/cloud/.env` automatically (see "Engine wiring" below). |
| `LOCAL_DASHBOARD_PORT` | `8787` | HTTP port the dashboard listens on, on all interfaces. |
| `MAX_UPLOAD_MB` | `2048` | Server-side cap on the declared upload size. |
| `NOTIFY_WEBHOOK_URL` | unset | Optional webhook POSTed on job completion/failure/rejection; silent no-op when unset. |

See `server/local/.env.example` for a ready-to-copy template.

## Data locations

- `server/local/data/bionico.db` (+ `-wal` / `-shm` sidecar files) — the
  SQLite database; created and migrated automatically at boot.
- `server/local/data/media/inbox/<job-id>/<file>` — uploaded raw videos.
- `server/local/data/media/outbox/<job-id>/<file>` — rendered deliverables.

The whole `server/local/data/` tree is gitignored. It is safe to delete while
the stack is stopped — the next start recreates it empty (this also wipes
all job history and files, so treat it like dropping a database).

## Engine wiring

`server/local/server.mjs` keeps the pull agent (`server/engine`) unmodified:
on every boot — and whenever the orchestrator runs its `--bootstrap-only`
pre-flight before `bionico start` spawns any children — it manages two
lines of `server/cloud/.env`, marking the block it owns:

```
# managed-by: server/local (local dashboard mode)
DASHBOARD_BASE_URL=http://127.0.0.1:<port>
AGENT_TOKEN=<the same token issued in server/local/.env>
```

It only touches this block when the file is absent or already carries that
marker; once you (or a Cloudflare deploy) hand-write a `DASHBOARD_BASE_URL`
without the marker, local mode leaves it alone instead of clobbering it. Any
other lines already in `server/cloud/.env` are preserved regardless. Because
the agent always reads its target from `server/cloud/.env` regardless of
where the dashboard actually runs, this is the entire integration — there is
no separate agent configuration for local vs. Cloudflare mode. To go back to
local mode after a Cloudflare deploy, delete the `DASHBOARD_BASE_URL`/
`AGENT_TOKEN` lines — the next `bionico start` regenerates the managed block
(see the note at the top of `server/cloud/README.md`).

## Troubleshooting

- **`bionico status` shows `dashboard  dead` / URLs missing** — check `node
  --version` (needs >= 22.5) and read
  `server/engine/runtime/_work/supervisor.log`.
- **Exits immediately with "Node >= 22.5 required"** — `node:sqlite` needs
  Node 22.5+. Check `node --version` and upgrade Node.
- **Windows Firewall prompt** — the first time the dashboard binds
  `0.0.0.0:<port>`, Windows asks whether to allow Node through the firewall;
  choose **private networks**. Pre-allow it instead with:

  ```powershell
  netsh advfirewall firewall add rule name="Bionico Dashboard" dir=in action=allow protocol=TCP localport=8787 profile=private
  ```

- **Port already in use (`EADDRINUSE`)** — another process is already bound
  to 8787, often a previous dashboard instance that did not shut down
  cleanly. Set a different `LOCAL_DASHBOARD_PORT` in `server/local/.env` and
  `bionico restart`, or free the port and restart.
- **Phone can't reach the dashboard** — checklist:
  1. Phone and PC must be on the **same WiFi network** — not a guest or
     "isolated" SSID (many routers block device-to-device traffic there).
  2. Use one of the `http://192.168.x.x:<port>` addresses from `bionico
     status`, not `127.0.0.1` (that only ever resolves on the PC itself).
  3. Confirm the Windows Firewall rule above is in place.
  4. Confirm the stack is actually running: `bionico status` should show the
     dashboard as running and list its URLs.
- **`ExperimentalWarning: SQLite is an experimental feature...`** at startup
  — expected and harmless; Node prints it because `node:sqlite` is still
  flagged experimental upstream.

## Testing

Full job-lifecycle smoke test — spawns a real server on port 18787 against
temp data/env files (nothing touches `server/local/data/` or your real `.env`
files) and exercises login, upload, agent claim, deliver, download (including
HTTP Range), zip, LAN-derived URLs, and delete:

```bash
node server/local/test/smoke.mjs
```

Prints `smoke: all assertions passed` on success.

Unit tests for the D1/R2 shims:

```bash
node --test server/local/test/lib.test.mjs
```
