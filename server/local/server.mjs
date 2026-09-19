#!/usr/bin/env node
// Local LAN dashboard: runs the Cloudflare Worker code (server/cloud/src) on
// plain Node — SQLite instead of D1, the filesystem instead of R2, HMAC-signed
// /media URLs instead of R2 presigning. Phones on the same WiFi reach it at
// http://<lan-ip>:<port>. Node >= 22.5 (node:sqlite).
import http from "node:http";
import os from "node:os";
import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createReadStream, createWriteStream, readFileSync, writeFileSync } from "node:fs";
import { mkdir, rename, stat } from "node:fs/promises";
import { dirname, extname, join, resolve, sep } from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";

const [maj, min] = process.versions.node.split(".").map(Number);
if (maj < 22 || (maj === 22 && min < 5)) {
  console.error(`bionico dashboard: Node >= 22.5 required (node:sqlite); found ${process.versions.node}`);
  process.exit(1);
}

const worker = (await import("../cloud/src/index.js")).default;
const { openDb, migrate } = await import("./lib/d1.mjs");
const { makeMedia, validKey } = await import("./lib/r2fs.mjs");

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..");
const DATA = process.env.BIONICO_LOCAL_DATA ? resolve(process.env.BIONICO_LOCAL_DATA) : join(HERE, "data");
const ENV_FILE = process.env.BIONICO_LOCAL_ENV ? resolve(process.env.BIONICO_LOCAL_ENV) : join(HERE, ".env");
const CLOUD_ENV_FILE = process.env.BIONICO_CLOUD_ENV
  ? resolve(process.env.BIONICO_CLOUD_ENV) : join(REPO, "server", "cloud", ".env");
const PUBLIC_DIR = join(REPO, "server", "cloud", "public");
const MIGRATIONS = join(REPO, "server", "cloud", "migrations");

// ---------- tiny .env io ----------
const parseEnv = (text) => Object.fromEntries(
  text.split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#") && l.includes("="))
    .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]),
);
const loadEnvFile = (p) => { try { return parseEnv(readFileSync(p, "utf8")); } catch { return {}; } };
const appendEnv = (p, obj) => {
  const lines = Object.entries(obj).map(([k, v]) => `${k}=${v}`).join("\n");
  let cur = ""; try { cur = readFileSync(p, "utf8"); } catch { /* new file */ }
  writeFileSync(p, cur + (cur && !cur.endsWith("\n") ? "\n" : "") + lines + "\n");
};

// server/cloud/.env is how the (unchanged) engine agent finds the dashboard.
// Rewrite only our managed lines; keep anything else the operator put there.
function writeCloudEnv(base, token) {
  const MARK = "# managed-by: server/local (local dashboard mode)";
  let lines = [];
  try { lines = readFileSync(CLOUD_ENV_FILE, "utf8").split(/\r?\n/); } catch { /* new file */ }
  const managed = lines.some((l) => l.trim() === MARK);
  const hasBase = lines.some((l) => l.startsWith("DASHBOARD_BASE_URL="));
  // Respect an operator-written cloud config (e.g. a real Cloudflare deployment):
  // only manage the wiring we created (marker) or create it when absent.
  if (hasBase && !managed) return;
  const keep = lines.filter((l) => l.trim()
    && !l.startsWith("DASHBOARD_BASE_URL=") && !l.startsWith("AGENT_TOKEN=") && l.trim() !== MARK);
  writeFileSync(CLOUD_ENV_FILE, [...keep, MARK, `DASHBOARD_BASE_URL=${base}`, `AGENT_TOKEN=${token}`, ""].join("\n"));
}

// ---------- bootstrap ----------
async function bootstrap() {
  await mkdir(DATA, { recursive: true });
  const cur = loadEnvFile(ENV_FILE);
  const add = {};
  if (!cur.SESSION_SECRET) add.SESSION_SECRET = randomBytes(32).toString("hex");
  if (!cur.AGENT_TOKEN) add.AGENT_TOKEN = randomBytes(32).toString("hex");
  if (!cur.DASHBOARD_PASSWORD) add.DASHBOARD_PASSWORD = randomBytes(4).toString("hex");
  if (Object.keys(add).length) appendEnv(ENV_FILE, add);
  const env = { ...cur, ...add };
  const port = Number(process.env.LOCAL_DASHBOARD_PORT || env.LOCAL_DASHBOARD_PORT || 8787);
  writeCloudEnv(`http://127.0.0.1:${port}`, env.AGENT_TOKEN);
  return { fileEnv: env, port };
}

const { fileEnv, port } = await bootstrap();
if (process.argv.includes("--bootstrap-only")) process.exit(0);

const sha256hex = (s) => createHash("sha256").update(s, "utf8").digest("hex");
const db = openDb(join(DATA, "bionico.db"));
migrate(db, MIGRATIONS);
const media = makeMedia(join(DATA, "media"));
await media.ensureRoot();

const baseEnv = {
  DB: db,
  MEDIA: media,
  LOCAL_MODE: "1",
  ACCOUNT_ID: "local",
  BUCKET_NAME: "local",
  MAX_UPLOAD_MB: fileEnv.MAX_UPLOAD_MB || "2048",
  OWNER_PASSWORD_HASH: sha256hex(fileEnv.DASHBOARD_PASSWORD),
  ...(fileEnv.ADMIN_PASSWORD ? { OPERATOR_ADMIN_PASSWORD_HASH: sha256hex(fileEnv.ADMIN_PASSWORD) } : {}),
  SESSION_SECRET: fileEnv.SESSION_SECRET,
  AGENT_TOKEN: fileEnv.AGENT_TOKEN,
  ...(fileEnv.NOTIFY_WEBHOOK_URL ? { NOTIFY_WEBHOOK_URL: fileEnv.NOTIFY_WEBHOOK_URL } : {}),
};
const ctx = { waitUntil: (p) => Promise.resolve(p).catch((e) => console.error("waitUntil:", e)) };

// ---------- helpers ----------
const readAll = (stream) => new Promise((res, rej) => {
  const chunks = [];
  stream.on("data", (c) => chunks.push(c));
  stream.on("end", () => res(Buffer.concat(chunks)));
  stream.on("error", rej);
});
const timingSafeEq = (a, b) => {
  const ba = Buffer.from(String(a)), bb = Buffer.from(String(b));
  return ba.length === bb.length && timingSafeEqual(ba, bb);
};
const mediaSig = (method, key, exp, extra) =>
  createHmac("sha256", baseEnv.SESSION_SECRET).update(`${method}|${key}|${exp}|${extra}`).digest("hex");
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".txt": "text/plain; charset=utf-8", ".json": "application/json", ".srt": "text/plain; charset=utf-8",
  ".pdf": "application/pdf", ".zip": "application/zip", ".svg": "image/svg+xml", ".ico": "image/x-icon",
};
const typeOf = (p) => TYPES[extname(p).toLowerCase()] || "application/octet-stream";

async function sendResponse(res, response) {
  const headers = {};
  response.headers.forEach((v, k) => { if (k !== "set-cookie") headers[k] = v; });
  const setCookie = response.headers.getSetCookie();
  if (setCookie.length) headers["set-cookie"] = setCookie;
  res.writeHead(response.status, headers);
  if (response.body) await pipeline(Readable.fromWeb(response.body), res);
  else res.end();
}

// ---------- /api/* -> the Worker, unchanged ----------
async function handleApi(req, res, u) {
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (typeof v === "string") headers.set(k, v);
    else if (Array.isArray(v)) headers.set(k, v.join(", "));
  }
  headers.set("CF-Connecting-IP", req.socket.remoteAddress || "unknown");
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const request = new Request(u.toString(), {
    method: req.method, headers, body: hasBody ? await readAll(req) : undefined,
  });
  const env = { ...baseEnv, PUBLIC_BASE_URL: `http://${req.headers.host || `127.0.0.1:${port}`}` };
  await sendResponse(res, await worker.fetch(request, env, ctx));
}

// ---------- /media/<key> — the local stand-in for presigned R2 ----------
async function handleMedia(req, res, u) {
  const key = decodeURIComponent(u.pathname.slice("/media/".length));
  const exp = Number(u.searchParams.get("exp") || 0);
  const method = req.method === "HEAD" ? "GET" : req.method;
  const extra = [...u.searchParams.entries()]
    .filter(([k]) => k.startsWith("response-"))
    .map(([k, v]) => `${k}=${v}`).sort().join("&");
  const ok = validKey(key) && exp > Date.now()
    && timingSafeEq(u.searchParams.get("sig") || "", mediaSig(method, key, exp, extra));
  if (!ok) { res.writeHead(403); return res.end("forbidden"); }
  const path = media.pathOf(key);
  if (method === "PUT") {
    await mkdir(dirname(path), { recursive: true });
    const tmp = `${path}.part`;
    await pipeline(req, createWriteStream(tmp));
    await rename(tmp, path);
    res.writeHead(200); return res.end();
  }
  if (method !== "GET") { res.writeHead(405); return res.end(); }
  let s;
  try { s = await stat(path); } catch { res.writeHead(404); return res.end("not found"); }
  const base = {
    "Content-Type": u.searchParams.get("response-content-type") || typeOf(path),
    "Accept-Ranges": "bytes",
    ...(u.searchParams.get("response-content-disposition")
      ? { "Content-Disposition": u.searchParams.get("response-content-disposition") } : {}),
  };
  // Range support matters: it is why the cloud used presigned R2 (resumable
  // phone downloads). Single-range only, like R2.
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
  if (m && (m[1] || m[2])) {
    const start = m[1] ? Number(m[1]) : Math.max(0, s.size - Number(m[2]));
    const end = m[1] && m[2] ? Math.min(Number(m[2]), s.size - 1) : s.size - 1;
    if (!(start >= 0 && start <= end && end < s.size)) {
      res.writeHead(416, { "Content-Range": `bytes */${s.size}` }); return res.end();
    }
    res.writeHead(206, { ...base, "Content-Range": `bytes ${start}-${end}/${s.size}`, "Content-Length": end - start + 1 });
    if (req.method === "HEAD") return res.end();
    return pipeline(createReadStream(path, { start, end }), res);
  }
  res.writeHead(200, { ...base, "Content-Length": s.size });
  if (req.method === "HEAD") return res.end();
  return pipeline(createReadStream(path), res);
}

// ---------- static dashboard (server/cloud/public) ----------
async function handleStatic(req, res, u) {
  if (req.method !== "GET" && req.method !== "HEAD") { res.writeHead(405); return res.end(); }
  const rel = u.pathname === "/" ? "index.html" : u.pathname.slice(1);
  const path = resolve(PUBLIC_DIR, rel);
  if (path !== PUBLIC_DIR && !path.startsWith(PUBLIC_DIR + sep)) { res.writeHead(403); return res.end(); }
  let s;
  try { s = await stat(path); } catch { res.writeHead(404); return res.end("not found"); }
  if (!s.isFile()) { res.writeHead(404); return res.end("not found"); }
  res.writeHead(200, { "Content-Type": typeOf(path), "Content-Length": s.size });
  if (req.method === "HEAD") return res.end();
  return pipeline(createReadStream(path), res);
}

// ---------- B-Roll Asset Library & API Key endpoints ----------
import { readdir, unlink } from "node:fs/promises";

const BROLL_DIR = join(REPO, "pipeline", "broll_library");
const ROOT_ENV_FILE = join(REPO, ".env");

async function handleBroll(req, res, u) {
  await mkdir(BROLL_DIR, { recursive: true });
  if ((u.pathname === "/api/broll" || u.pathname === "/api/broll/list") && req.method === "GET") {
    const entries = await readdir(BROLL_DIR, { withFileTypes: true });
    const items = [];
    for (const e of entries) {
      if (e.isFile() && e.name !== "README.md" && !e.name.startsWith(".")) {
        const s = await stat(join(BROLL_DIR, e.name));
        const ext = extname(e.name).toLowerCase();
        const isVid = [".mp4", ".mov", ".m4v"].includes(ext);
        items.push({ name: e.name, size: s.size, kind: isVid ? "video" : "image" });
      }
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify(items));
  }
  if (u.pathname === "/api/broll/upload" && req.method === "POST") {
    const fn = u.searchParams.get("filename") || `asset_${Date.now()}.mp4`;
    const safeName = fn.replace(/[^a-zA-Z0-9._-]/g, "_");
    const targetPath = join(BROLL_DIR, safeName);
    const tmpPath = `${targetPath}.part`;
    await pipeline(req, createWriteStream(tmpPath));
    await rename(tmpPath, targetPath);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, filename: safeName }));
  }
  if (u.pathname.startsWith("/api/broll/delete/") && req.method === "POST") {
    const name = decodeURIComponent(u.pathname.slice("/api/broll/delete/".length));
    const safeName = name.replace(/[^a-zA-Z0-9._-]/g, "_");
    try { await unlink(join(BROLL_DIR, safeName)); } catch {}
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true }));
  }
  if (u.pathname.startsWith("/api/broll/file/") && req.method === "GET") {
    const name = decodeURIComponent(u.pathname.slice("/api/broll/file/".length));
    const targetPath = join(BROLL_DIR, name);
    let s;
    try { s = await stat(targetPath); } catch { res.writeHead(404); return res.end("not found"); }
    res.writeHead(200, { "Content-Type": typeOf(targetPath), "Content-Length": s.size });
    return pipeline(createReadStream(targetPath), res);
  }
  res.writeHead(404);
  res.end("not found");
}

async function handleKeys(req, res, u) {
  if (req.method === "GET") {
    const envObj = loadEnvFile(ROOT_ENV_FILE);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({
      ELEVENLABS_API_KEY: envObj.ELEVENLABS_API_KEY || process.env.ELEVENLABS_API_KEY || "",
      OPENROUTER_API_KEY: envObj.OPENROUTER_API_KEY || process.env.OPENROUTER_API_KEY || "",
      FREE_LLM_API_KEY: envObj.FREE_LLM_API_KEY || process.env.FREE_LLM_API_KEY || "",
      FREE_LLM_BASE_URL: envObj.FREE_LLM_BASE_URL || process.env.FREE_LLM_BASE_URL || "https://api.freellmapi.com/v1",
      ANTHROPIC_API_KEY: envObj.ANTHROPIC_API_KEY || process.env.ANTHROPIC_API_KEY || "",
      TRYPOST_API_URL: envObj.TRYPOST_API_URL || process.env.TRYPOST_API_URL || "http://trypost:8000/api/v1/posts",
      AI_PROVIDER: envObj.AI_PROVIDER || process.env.AI_PROVIDER || "auto",
      AI_MODEL_CHOICE: envObj.AI_MODEL_CHOICE || process.env.AI_MODEL_CHOICE || "default",
    }));
  }
  if (req.method === "POST" || req.method === "PUT") {
    const bodyText = await readAll(req);
    let body = {};
    try { body = JSON.parse(bodyText.toString("utf8")); } catch {}
    const updates = {};
    const allowed = [
      "ELEVENLABS_API_KEY",
      "OPENROUTER_API_KEY",
      "FREE_LLM_API_KEY",
      "FREE_LLM_BASE_URL",
      "ANTHROPIC_API_KEY",
      "TRYPOST_API_URL",
      "AI_PROVIDER",
      "AI_MODEL_CHOICE",
    ];
    for (const k of allowed) {
      if (Object.prototype.hasOwnProperty.call(body, k)) {
        const val = String(body[k] || "").trim();
        updates[k] = val;
        process.env[k] = val;
      }
    }
    if (Object.keys(updates).length) {
      appendEnv(ROOT_ENV_FILE, updates);
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true }));
  }
  res.writeHead(405);
  res.end();
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://${req.headers.host || `127.0.0.1:${port}`}`);
  const route = u.pathname.startsWith("/media/") ? handleMedia
    : u.pathname.startsWith("/api/broll") ? handleBroll
    : u.pathname === "/api/keys" ? handleKeys
    : u.pathname.startsWith("/api/") ? handleApi
    : handleStatic;
  route(req, res, u).catch((err) => {
    console.error(`${req.method} ${u.pathname}:`, err);
    if (!res.headersSent) res.writeHead(500);
    res.end("server error");
  });
});


// The Worker's 15-min maintenance cron, on a timer (plus once at boot).
const cronEnv = { ...baseEnv, PUBLIC_BASE_URL: `http://127.0.0.1:${port}` };
const runCron = () => worker.scheduled({ scheduledTime: Date.now(), cron: "*/15 * * * *" }, cronEnv, ctx);
runCron();
setInterval(runCron, 15 * 60 * 1000);

server.listen(port, "0.0.0.0", () => {
  const urls = [`http://127.0.0.1:${port}`];
  for (const ifs of Object.values(os.networkInterfaces())) {
    for (const i of ifs || []) if (i.family === "IPv4" && !i.internal) urls.push(`http://${i.address}:${port}`);
  }
  console.log("bionico dashboard (local) listening:");
  for (const uStr of urls) console.log(`  ${uStr}`);
  console.log(`  password: ${fileEnv.DASHBOARD_PASSWORD}  (change DASHBOARD_PASSWORD in server/local/.env, then restart)`);
});
