# START HERE — Contenido Bionico, step by step

**What this is:** a video factory that runs on your own computer. You upload a raw
vertical video from your phone; your computer edits it (cut, captions, camera
moves, animations, music) and gives you back a finished short plus posters,
quote cards, carousels and more. Nothing goes to the cloud — your phone talks to
your computer over your home WiFi.

---

## 1. What you need before starting (one time)

| # | Thing | Where | Note |
|---|-------|-------|------|
| 1 | A computer that can stay on while videos render | — | Windows, Mac or Linux |
| 2 | **Claude Code** (the AI that installs AND edits) | claude.com/claude-code | Sign in with a Claude subscription (Pro/Max) |
| 3 | **Python 3.11+** | python.org/downloads | Windows: tick "Add python.exe to PATH" |
| 4 | **Node.js 22.5+** | nodejs.org | The dashboard needs 22.5 or newer |
| 5 | **ffmpeg** | ffmpeg.org | Must be on PATH |
| 6 | A free **ElevenLabs** account + API key | elevenlabs.io | Key needs the "speech to text" permission |

Not sure how to install 3–5? Install Claude Code first (item 2) and ask it —
it can walk you through each one.

## 2. Set it up (once, ~20 minutes)

1. Unzip `Contenido-Bionico-V6-PR.zip` anywhere (example: `C:\Contenido-Bionico`).
2. Open a terminal **inside that folder** and run `claude`.
3. Tell it: **"Read AGENTS.md and set this up for me."**
4. Answer its questions — it will ask for your ElevenLabs key, then install
   everything itself (10–20 min the first time).
5. When it says the engine is running, setup is done. It will show you the
   dashboard address and password.

## 3. Use it (every time)

1. On your phone — **same WiFi as the computer** — open the dashboard address.
   It looks like `http://192.168.1.23:8787`.
   (Lost it? On the computer run `.venv\Scripts\bionico.exe status`, or ask
   Claude Code. The password is in `server/local/.env`.)
2. Log in with the password.
3. Upload a raw **vertical** video (talking to camera works best; up to 7 min).
4. Wait. A short takes roughly 10–40 minutes depending on animation settings.
5. When the job says **done**: download the final video, posters, quote cards,
   or a ZIP pack — or request changes on the same row and only the changed
   pieces are redone.

## 4. If something doesn't work

- Easiest: open Claude Code in the folder and describe the problem — it can
  read the logs and fix most things.
- Phone can't reach the dashboard → same WiFi? On Windows, allow Node.js
  through the firewall for **Private networks** (a prompt appears on first
  start).
- Dashboard shows `dead` in `bionico status` → check `node --version` is
  22.5+ and read `server/engine/runtime/_work/supervisor.log`.
- Prefer installing by hand, without the AI? The full manual recipe is in
  `README.md`.

## 5. Make it yours (optional)

- **Quote cards with your name/photo:**
  `.venv\Scripts\python.exe pipeline\scripts\make_quote_template.py --name "Your Name" --handle "@yourhandle" --avatar your-photo.jpg`
- **WhatsApp-style videos:** set `BIONICO_WA_NAME=Your Name` in `.env` and drop
  your profile photo at `pipeline\broll_library\pfp.jpeg`.
- **Photo-backed formats:** drop your photos into `pipeline\broll_library\` —
  they activate automatically.
