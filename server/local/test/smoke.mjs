// Full-lifecycle smoke test against a real spawned server on port 18787:
// login (cookie w/o Secure) -> create job -> PUT upload -> finalize ->
// agent claim (bearer) -> download input (+Range) -> status -> upload-url ->
// PUT deliverable -> complete -> files (302) -> zip -> LAN-host URLs -> delete.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PORT = 18787;
const BASE = `http://127.0.0.1:${PORT}`;
const tmp = mkdtempSync(join(tmpdir(), "bionico-smoke-"));
const envFile = join(tmp, "local.env");
const cloudEnvFile = join(tmp, "cloud.env");

const child = spawn(process.execPath, [join(HERE, "..", "server.mjs")], {
  env: {
    ...process.env,
    BIONICO_LOCAL_DATA: join(tmp, "data"),
    BIONICO_LOCAL_ENV: envFile,
    BIONICO_CLOUD_ENV: cloudEnvFile,
    LOCAL_DASHBOARD_PORT: String(PORT),
  },
  stdio: ["ignore", "pipe", "inherit"],
});
child.stdout.on("data", () => {});
const stop = (code) => { child.kill(); process.exit(code); };
process.on("uncaughtException", (e) => { console.error(e); stop(1); });
process.on("unhandledRejection", (e) => { console.error(e); stop(1); });

for (let i = 0; ; i++) {
  try { if ((await fetch(`${BASE}/api/health`)).ok) break; } catch { /* not up yet */ }
  if (i > 100) throw new Error("server never came up");
  await new Promise((r) => setTimeout(r, 100));
}

const fileEnv = Object.fromEntries(readFileSync(envFile, "utf8").split(/\r?\n/)
  .filter((l) => l.includes("=")).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
const cloudEnv = Object.fromEntries(readFileSync(cloudEnvFile, "utf8").split(/\r?\n/)
  .filter((l) => l.includes("=") && !l.startsWith("#")).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
assert.equal(cloudEnv.DASHBOARD_BASE_URL, BASE, "engine wiring written");
assert.equal(cloudEnv.AGENT_TOKEN, fileEnv.AGENT_TOKEN, "one shared agent token");

// -- login: works over http (no Secure), bad password rejected
assert.equal((await fetch(`${BASE}/api/login`, { method: "POST", body: JSON.stringify({ password: "wrong" }) })).status, 401);
const login = await fetch(`${BASE}/api/login`, { method: "POST", body: JSON.stringify({ password: fileEnv.DASHBOARD_PASSWORD }) });
assert.equal(login.status, 200);
const setCookie = login.headers.getSetCookie()[0];
assert.match(setCookie, /bionico_session=/);
assert.doesNotMatch(setCookie, /Secure/, "cookie must not be Secure in local mode");
const cookie = setCookie.split(";")[0];
assert.equal((await fetch(`${BASE}/api/settings`)).status, 401, "no cookie -> 401");
assert.equal((await fetch(`${BASE}/api/settings`, { headers: { cookie } })).status, 200);

// -- UI upload flow: create -> PUT -> finalize
const raw = Buffer.from("fake-mp4-bytes-0123456789");
const created = await (await fetch(`${BASE}/api/jobs`, {
  method: "POST", headers: { cookie, "content-type": "application/json" },
  body: JSON.stringify({ filename: "video.mp4", bytes: raw.length }),
})).json();
assert.ok(created.id && created.upload_url, "job id + upload url");
assert.equal((await fetch(created.upload_url, { method: "PUT", body: raw })).status, 200);
assert.equal((await fetch(`${BASE}/api/jobs/${created.id}/finalize`, { method: "POST", headers: { cookie } })).status, 200);

// -- engine flow: claim with bearer, download input (with Range), heartbeat, deliver
const bearer = { Authorization: `Bearer ${fileEnv.AGENT_TOKEN}` };
assert.equal((await fetch(`${BASE}/api/agent/claim`, { method: "POST", headers: { Authorization: "Bearer nope" } })).status, 401);
const claim = await fetch(`${BASE}/api/agent/claim`, { method: "POST", headers: bearer });
assert.equal(claim.status, 200);
const job = await claim.json();
assert.equal(job.id, created.id);
assert.equal(Buffer.from(await (await fetch(job.input_url)).arrayBuffer()).toString(), raw.toString());
const ranged = await fetch(job.input_url, { headers: { Range: "bytes=0-3" } });
assert.equal(ranged.status, 206);
assert.equal(await ranged.text(), "fake");
assert.equal((await fetch(`${BASE}/api/agent/jobs/${job.id}/status`, {
  method: "POST", headers: bearer, body: JSON.stringify({ status: "processing", stage: "cut" }),
})).status, 200);
const up = await (await fetch(`${BASE}/api/agent/jobs/${job.id}/upload-url`, {
  method: "POST", headers: bearer, body: JSON.stringify({ name: "final_video.mp4" }),
})).json();
const finalBytes = Buffer.from("final-render-bytes");
// PINNED: uploadUrl() in server/cloud/src/agent.js returns { key, url } — field is `url`.
assert.equal((await fetch(up.url, { method: "PUT", body: finalBytes })).status, 200);
assert.equal((await fetch(`${BASE}/api/agent/jobs/${job.id}/complete`, {
  method: "POST", headers: bearer,
  body: JSON.stringify({ run_id: "run_1", tokens_total: 0,
    outputs: [{ name: "final_video.mp4", key: `outbox/${job.id}/final_video.mp4`, kind: "video" }] }),
})).status, 200);
const rows = await (await fetch(`${BASE}/api/jobs`, { headers: { cookie } })).json();
// PINNED: listJobs() in server/cloud/src/jobs.js returns a bare array (json(rows.results.map(...))), not { jobs: [...] }.
const row = rows.find((r) => r.id === created.id);
assert.equal(row.status, "done");

// -- downloads: mp4 -> 302 to signed /media URL; bad sig -> 403; zip 'all' -> PK
const dl = await fetch(`${BASE}/api/jobs/${created.id}/files/final_video.mp4`, { headers: { cookie }, redirect: "manual" });
assert.equal(dl.status, 302);
const loc = dl.headers.get("location");
assert.ok(loc.startsWith(`${BASE}/media/outbox/`), `302 to local media, got ${loc}`);
assert.equal(Buffer.from(await (await fetch(loc)).arrayBuffer()).toString(), finalBytes.toString());
assert.equal((await fetch(loc.replace(/sig=[0-9a-f]{10}/, "sig=deadbeefde"))).status, 403);
const tampered = new URL(loc);
tampered.searchParams.set("response-content-type", "text/html");
assert.equal((await fetch(tampered)).status, 403, "unsigned response-* override must be rejected");
const zip = await fetch(`${BASE}/api/jobs/${created.id}/zip/all`, { headers: { cookie } });
assert.equal(zip.status, 200);
assert.equal(Buffer.from(await zip.arrayBuffer()).subarray(0, 2).toString(), "PK");

// -- LAN correctness: presigned URLs are built from the request's Host header,
// so a phone that loaded the page via 192.168.x.x gets 192.168.x.x links.
const lanLoc = await new Promise((resolveP, rejectP) => {
  const r = http.request({
    host: "127.0.0.1", port: PORT, method: "GET",
    path: `/api/jobs/${created.id}/files/final_video.mp4`,
    headers: { cookie, host: `192.168.99.5:${PORT}` },
  }, (resp) => { resp.resume(); resolveP(resp.headers.location || ""); });
  r.on("error", rejectP);
  r.end();
});
assert.ok(lanLoc.startsWith(`http://192.168.99.5:${PORT}/media/`), `Host-derived URL, got ${lanLoc}`);

// -- delete: row gone and media purged
assert.equal((await fetch(`${BASE}/api/jobs/${created.id}/delete`, { method: "POST", headers: { cookie } })).status, 200);
assert.equal((await fetch(loc)).status, 404, "deliverable purged from disk");

console.log("smoke: all assertions passed");
stop(0);
