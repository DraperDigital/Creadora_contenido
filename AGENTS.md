# Contenido Bionico V6 — public release

This file is the source of truth for any AI (Claude Code, Codex, etc.) asked to
install, set up, or "make this work". Follow it **in order**. Do not fall back to
a generic Python-install guess.

This is a self-hosted product: a local web dashboard (`server/local`) plus a
self-hosted engine on this machine (`server/engine`) that pulls each upload
and runs the editing pipeline (`pipeline/`). The operator provisions and
maintains the machine **manually** — copies the repo folder over, installs
the packages by hand, writes `.env` — there is no end-user onboarding flow
and no `bionico` install/provision/doctor command. The only `bionico`
commands are orchestrator control: `skills`, `start`, `stop`, `restart`,
`status`, `autostart`. The same dashboard code is also deployable to
Cloudflare (`server/cloud`) as an optional alternative, provisioned
separately — see `server/cloud/README.md`. Default to the local path unless
the operator asks for Cloudflare.

## Principles

- **You do the work; the user only does what software cannot.** Automate every
  step yourself. Only ask the user for accounts / API keys: an ElevenLabs key
  (free account, `speech_to_text` permission) and, if they choose the Anthropic
  API-key engine, an Anthropic key.
- **Narrate as you go.** Before each step, say in plain language what is about to
  happen and how long it takes — dependency install 5–20 min on first run, npm
  deps 1–5 min — so nothing looks frozen.
- Talk to the user in their language (default Spanish unless they write in
  another). Ask one question at a time.

## 0. Pre-approve permissions (one upfront message, before anything runs)

Ask the user — in **one upfront message** — to pre-approve every sensitive action:
running package installs (`pip`, `npm`), writing the ElevenLabs (and optional
Anthropic) key into the local `.env`, and registering login autostart. AI
permission systems block these mid-flow; collecting consent first prevents
mid-install blockers.

## 1. Collect the few human inputs (ask the user, one at a time)

1. **Which AI does the editing?** Claude (recommended) or Codex/ChatGPT. If
   Claude: their logged-in subscription (recommended) or an Anthropic API key.
2. **ElevenLabs key:** a free account and a key with `speech_to_text` permission.
   Without it, cut quality drops sharply. Only if they chose the Anthropic
   API-key engine: also ask for that key.

## 2. Install the engine + pipeline (manual recipe)

There is no `bionico install`/`provision`/`doctor`. Provision the machine by
hand, in order. System tools (**Node.js >= 22.5** + npm, `ffmpeg` +
`ffprobe`) must already be on PATH — install them yourself first if missing.
Nothing puts a bare `bionico` on PATH, so invoke the venv launcher directly
(`.venv/bin/bionico`; Windows: `.venv\Scripts\bionico.exe`).

1. **Copy the repo folder to the machine** (the operator does this manually; if
   it is already here, skip).
2. **Create the venv and install both Python packages editable:**

   ```bash
   python -m venv .venv
   .venv/bin/pip install -e ./server/engine
   .venv/bin/pip install -e ./pipeline
   ```

3. **Install the Node deps** in both `pipeline/` and `server/cloud/` (the
   dashboard code lives in `server/cloud/src`; `server/local/server.mjs` runs
   it on plain Node, so its dependencies come from this same install):

   ```bash
   ( cd pipeline && npm install --no-audit --no-fund --loglevel=error )
   ( cd server/cloud && npm install --no-audit --no-fund --loglevel=error )
   ```

4. **Write the root `.env` file by hand.** Set `ELEVENLABS_API_KEY=<key>`
   (and `ANTHROPIC_API_KEY=<key>` only if the Claude api_key engine was
   chosen). Nothing else needs writing by hand for the default path:
   `bionico start` bootstraps `server/local/.env` (a random
   `DASHBOARD_PASSWORD`, `SESSION_SECRET`, `AGENT_TOKEN`) and wires the pull
   agent to the dashboard itself on first run.
5. **Log in the Claude subscription** (unless using the Anthropic api_key engine):
   run `claude` (or `codex` for the ChatGPT engine) and complete its login so the
   editing agent is authenticated.
6. **Install the AI-client skills:** `.venv/bin/bionico skills` (installs
   `/bionico` and `/short`).

Windows (PowerShell): the same with `.venv\Scripts\python.exe`,
`.venv\Scripts\pip.exe`, `.venv\Scripts\bionico.exe`, and
`Push-Location pipeline; npm install ...; Pop-Location` (likewise for
`server/cloud`).

Deploying this same dashboard to Cloudflare instead of running it locally is
optional and provisioned separately — see `server/cloud/README.md`. Do not
set that up unless the operator explicitly asks for it.

## 3. Start

`bionico start --detach` starts the watcher, the local dashboard
(`server/local`), and the pull agent — the dashboard wires the agent to
itself automatically, so all three come up from one command. Confirm with
`bionico status`, which also prints every dashboard URL. If the dashboard is
not reachable, run `bionico status` and check
`server/engine/runtime/_work/supervisor.log` for the cause (commonly a Node
version below 22.5).

Tell the user: the dashboard is reachable at the printed
`http://127.0.0.1:8787` (this PC) and `http://192.168.x.x:8787` addresses
(any phone or device on the same WiFi); the login password was
auto-generated into `server/local/.env` on first start (change it there, then
`bionico restart`). On the very first start, Windows will prompt to allow
Node through the firewall — tell the user to allow it on **private
networks**.

The operator then uploads raw vertical videos in the dashboard; each finished
job's deliverables upload back for download. The engine machine runs ONE
agent at a time.

Enable login autostart as part of the install — `.venv/bin/bionico autostart
enable` (Windows: `.venv\Scripts\bionico.exe autostart enable`) — so the stack
comes back after a reboot. `bionico status` reports whether autostart is
registered; `bionico autostart disable` unregisters it.

Two manual skills exist for use outside the dashboard flow: `/bionico`
((re)starts the stack) and `/short` (runs the editor on a 9:16 video) — the
`/short` flow is defined in
`pipeline/src/contenido_bionico/short/SKILL.md`.

During production, animations can take 10–20 minutes per scene. Do not kill or
relaunch processes on silence.

## What you cannot do (ask, don't fake)

Creating accounts and API keys happens off this machine. Guide the user precisely;
never pretend you performed them.
