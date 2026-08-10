import { json } from "./index.js";
import { presignUrl } from "./presign.js";

export function newId() {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function logEvent(env, jobId, source, event, detail = null) {
  try {
    await env.DB.prepare(
      "INSERT INTO events (job_id, ts, source, event, detail) VALUES (?, ?, ?, ?, ?)",
    ).bind(jobId, Date.now(), source, event, detail ? String(detail).slice(0, 500) : null).run();
  } catch (e) {
    console.log("event log failed:", e);
  }
}

// Accepted upload container extensions. The R2 input key always uses one of
// these (never a raw client-supplied string), so key handling stays safe.
const UPLOAD_EXTS = ["mp4", "mov", "m4v"];
const DEFAULT_MAX_UPLOAD_MB = 2048;

export function maxUploadMb(env) {
  const configured = Number(env.MAX_UPLOAD_MB);
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_MAX_UPLOAD_MB;
}

export async function createJob(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const filename = String(body.filename || "video.mp4").slice(0, 200);
  const extMatch = filename.toLowerCase().match(/\.([a-z0-9]+)$/);
  const ext = extMatch ? extMatch[1] : "";
  const contentType = String(body.content_type || "").toLowerCase();
  if (!UPLOAD_EXTS.includes(ext) && !contentType.startsWith("video/")) {
    return json({ error: "solo se aceptan videos (.mp4, .mov o .m4v)" }, 400);
  }
  // Server-side size cap on the declared size. (The presigned PUT itself
  // cannot enforce a length: query-signed S3 PUTs have no content-length-range
  // condition — that needs POST policies, which R2 presigning does not offer.)
  const capMb = maxUploadMb(env);
  const sizeBytes = Number(body.size_bytes) || null;
  if (sizeBytes && sizeBytes > capMb * 1024 * 1024) {
    return json({ error: `el archivo supera el límite de ${capMb} MB` }, 413);
  }
  const id = newId();
  const keyExt = UPLOAD_EXTS.includes(ext) ? ext : "mp4";
  const inputKey = `inbox/${id}/video.${keyExt}`;
  const now = Date.now();
  // Snapshot the panel's animation quality, feature toggles, and anim-count
  // tier at creation time, so changing the selectors later never
  // retroactively affects an already-submitted job.
  const animQuality = await currentAnimQuality(env);
  const features = JSON.stringify(await currentFeatures(env));
  const animCount = await currentAnimCount(env);
  // A fresh upload is version 1 of its own lineage (it IS the card).
  await env.DB.prepare(
    `INSERT INTO jobs (id, filename, format, status, input_key, size_bytes, anim_quality, features, anim_count, lineage_id, version_no, created_at, updated_at)
     VALUES (?, ?, 'short', 'uploading', ?, ?, ?, ?, ?, ?, 1, ?, ?)`,
  ).bind(id, filename, inputKey, sizeBytes, animQuality, features, animCount, id, now, now).run();
  const uploadUrl = await presignUrl(env, "PUT", inputKey, 86400);
  await logEvent(env, id, "user", "created");
  return json({ id, upload_url: uploadUrl }, 201);
}

export async function finalizeJob(env, id) {
  const res = await env.DB.prepare(
    "UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='uploading'",
  ).bind(Date.now(), id).run();
  if (!res.meta.changes) return json({ error: "not in uploading state" }, 409);
  await logEvent(env, id, "user", "finalized");
  return json({ ok: true });
}

export async function listJobs(env, admin = true) {
  const rows = await env.DB.prepare(
    `SELECT id, filename, format, status, error, size_bytes, run_id, kind, target, instructions, outputs,
     stage, stage_detail, tokens_total, cancel_requested, features, anim_count,
     lineage_id, version_no, parent_id, created_at, updated_at
     FROM jobs ORDER BY created_at DESC LIMIT 200`,
  ).all();
  return json(rows.results.map((r) => {
    let outputs = null;
    try {
      outputs = r.outputs ? JSON.parse(r.outputs) : null;
    } catch {
      outputs = null;
    }
    let features = null;
    try {
      features = r.features ? JSON.parse(r.features) : null;
    } catch {
      features = null;
    }
    let stageDetail = null;
    try {
      stageDetail = r.stage_detail ? JSON.parse(r.stage_detail) : null;
    } catch {
      stageDetail = null;
    }
    const out = { ...r, outputs, features, stage_detail: stageDetail };
    // Token spend is operator-facing telemetry: hidden from customer sessions.
    if (!admin) delete out.tokens_total;
    return out;
  }));
}

export async function requestChanges(request, env, id) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const target = String(body.target || "");
  if (!["video", "quotes", "carousel"].includes(target)) {
    return json({ error: "invalid target" }, 400);
  }
  const instructions = String(body.instructions || "").trim().slice(0, 2000);
  if (!instructions) return json({ error: "instructions required" }, 400);
  // An edit BRANCHES a new version from a done version that still has its run:
  // a new row (child) joins the same lineage/card, and the parent is left
  // fully intact so its outputs stay downloadable. The parent may itself be an
  // edit — you can re-edit any version.
  const parent = await env.DB.prepare(
    `SELECT id, filename, format, input_key, run_id, features, anim_count, lineage_id, version_no
     FROM jobs WHERE id=? AND status='done' AND run_id IS NOT NULL`,
  ).bind(id).first();
  if (!parent) {
    return json({ error: "job must be done (with a recorded run) to request changes" }, 409);
  }
  const lineageId = parent.lineage_id || parent.id;
  const maxRow = await env.DB.prepare(
    "SELECT MAX(version_no) AS mx FROM jobs WHERE lineage_id=?",
  ).bind(lineageId).first();
  const nextVersion = (Number(maxRow && maxRow.mx) || parent.version_no || 1) + 1;
  // Re-read quality (an edit may bump effort) but carry forward the parent's
  // own feature toggles + anim-count — an edit never restructures the video.
  const animQuality = await currentAnimQuality(env);
  const childId = newId();
  const now = Date.now();
  await env.DB.prepare(
    `INSERT INTO jobs (id, filename, format, status, input_key, kind, target, instructions,
       anim_quality, features, anim_count, lineage_id, version_no, parent_id, parent_run_id,
       created_at, updated_at)
     VALUES (?, ?, ?, 'queued', ?, 'edit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(childId, parent.filename, parent.format, parent.input_key, target, instructions,
         animQuality, parent.features, parent.anim_count, lineageId, nextVersion, parent.id,
         parent.run_id, now, now).run();
  await logEvent(env, childId, "user", "changes", target);
  return json({ id: childId, version_no: nextVersion }, 201);
}

const FILE_TYPES = {
  mp4: "video/mp4",
  png: "image/png",
  txt: "text/plain; charset=utf-8",
  json: "application/json",
  srt: "text/plain; charset=utf-8",
  pdf: "application/pdf",
  zip: "application/zip",
};

export async function jobFile(env, id, name) {
  const row = await env.DB.prepare("SELECT outputs FROM jobs WHERE id=?").bind(id).first();
  if (!row || !row.outputs) return json({ error: "not found" }, 404);
  let outputs = [];
  try {
    outputs = JSON.parse(row.outputs);
  } catch {
    outputs = [];
  }
  const entry = outputs.find((o) => o && o.name === name);
  if (!entry) return json({ error: "not found" }, 404);
  const ext = name.split(".").pop().toLowerCase();
  const type = FILE_TYPES[ext] || "application/octet-stream";
  // Videos: redirect to a presigned R2 GET so the phone downloads directly from
  // R2 (native Range + resume, no Worker throughput bottleneck) — this fixes the
  // hours-long mobile video downloads. Small deliverables (caption text, slides)
  // stay proxied through the Worker so the fetch()-based caption preview keeps
  // working without needing cross-origin CORS on the bucket.
  if (ext === "mp4") {
    if (!(await env.MEDIA.head(entry.key))) return json({ error: "result object missing" }, 404);
    const url = await presignUrl(env, "GET", entry.key, 3600, {
      "response-content-type": type,
      "response-content-disposition": `attachment; filename="${id}-${name}"`,
    });
    return Response.redirect(url, 302);
  }
  const obj = await env.MEDIA.get(entry.key);
  if (!obj) return json({ error: "result object missing" }, 404);
  return new Response(obj.body, {
    headers: {
      "Content-Type": type,
      "Content-Disposition": `attachment; filename="${id}-${name}"`,
    },
  });
}

export async function jobResult(env, id) {
  const row = await env.DB.prepare(
    "SELECT status, output_key FROM jobs WHERE id=?",
  ).bind(id).first();
  if (!row || row.status !== "done" || !row.output_key) return json({ error: "not ready" }, 404);
  if (!(await env.MEDIA.head(row.output_key))) return json({ error: "result object missing" }, 404);
  // Redirect to a presigned R2 GET so the client downloads the video DIRECTLY
  // from R2 (native Range + resumable transfers, no Worker throughput cap). This
  // is what fixes hours-long mobile downloads: on a flaky phone connection R2 can
  // resume from where it dropped instead of restarting from byte 0, which the
  // Worker proxy (plain 200, no Range) could not do.
  const url = await presignUrl(env, "GET", row.output_key, 3600, {
    "response-content-type": "video/mp4",
    "response-content-disposition": `attachment; filename="${id}.mp4"`,
  });
  return Response.redirect(url, 302);
}

export async function retryJob(env, id) {
  const res = await env.DB.prepare(
    `UPDATE jobs SET status='queued', error=NULL, claimed_at=NULL, heartbeat_at=NULL,
     cancel_requested=0, stage=NULL, stage_detail=NULL,
     output_key=CASE WHEN kind='edit' THEN output_key ELSE NULL END,
     outputs=CASE WHEN kind='edit' THEN outputs ELSE NULL END,
     updated_at=? WHERE id=? AND status='failed'`,
  ).bind(Date.now(), id).run();
  if (!res.meta.changes) return json({ error: "not in failed state" }, 409);
  await logEvent(env, id, "user", "retry");
  return json({ ok: true });
}

// A live agent heartbeats a running job about every 60s. If there has been no
// heartbeat for this long, nothing is actively working the job, so a cancel
// FLAG would never get applied — cancel such jobs outright instead of leaving
// them stuck (the case where the button "did nothing" for up to 30 min).
const CANCEL_STALE_MS = 3 * 60 * 1000;

export async function cancelJob(env, id) {
  const now = Date.now();
  // Cancel outright when the job is not actively being worked:
  //   - not started yet (uploading/queued), OR
  //   - "running" but with a stale/missing heartbeat (dead or orphaned worker).
  const direct = await env.DB.prepare(
    `UPDATE jobs SET status='canceled', cancel_requested=0, stage=NULL, stage_detail=NULL, updated_at=?
     WHERE id=? AND (
       status IN ('uploading','queued')
       OR (status IN ('claimed','processing','publishing')
           AND COALESCE(heartbeat_at, claimed_at, 0) < ?)
     )`,
  ).bind(now, id, now - CANCEL_STALE_MS).run();
  if (direct.meta.changes) {
    await logEvent(env, id, "user", "canceled");
    return json({ ok: true, canceled: true });
  }
  // A live worker is on it (fresh heartbeat): flag the cancel; the agent relays
  // it and the worker kills the process within a poll cycle (seconds).
  const flagged = await env.DB.prepare(
    `UPDATE jobs SET cancel_requested=1, updated_at=?
     WHERE id=? AND status IN ('claimed','processing','publishing')`,
  ).bind(now, id).run();
  if (!flagged.meta.changes) return json({ error: "nothing to cancel" }, 409);
  await logEvent(env, id, "user", "cancel_requested");
  return json({ ok: true, canceled: false });
}

async function purgeJobObjects(env, row) {
  if (row.input_key) {
    try { await env.MEDIA.delete(row.input_key); } catch (e) { console.log("delete input failed:", e); }
  }
  if (row.output_key) {
    try { await env.MEDIA.delete(row.output_key); } catch (e) { console.log("delete output failed:", e); }
  }
  let cursor;
  do {
    const listing = await env.MEDIA.list({ prefix: `outbox/${row.id}/`, cursor });
    for (const obj of listing.objects) {
      try { await env.MEDIA.delete(obj.key); } catch (e) { console.log("delete output failed:", e); }
    }
    cursor = listing.truncated ? listing.cursor : undefined;
  } while (cursor);
}

const DELETABLE = ["done", "failed", "canceled", "rejected", "uploading"];

// Deleting a version that produced a local run queues its run id so the PC
// agent reclaims that version's working + output folders. Only the PC knows
// where those live, so the cloud can only hand off the run id. Best-effort:
// a reclaim-enqueue failure never blocks the D1/R2 delete.
async function enqueueReclaim(env, runId) {
  if (!runId) return;
  try {
    await env.DB.prepare(
      "INSERT OR IGNORE INTO reclaims (run_id, created_at) VALUES (?, ?)",
    ).bind(runId, Date.now()).run();
  } catch (e) {
    console.log("reclaim enqueue failed:", e);
  }
}

export async function deleteJob(env, id) {
  const row = await env.DB.prepare("SELECT id, input_key, output_key, status, run_id FROM jobs WHERE id=?").bind(id).first();
  if (!row || !DELETABLE.includes(row.status)) return json({ error: "not deletable" }, 409);
  await purgeJobObjects(env, row);
  await env.DB.prepare("DELETE FROM events WHERE job_id=?").bind(id).run();
  await env.DB.prepare("DELETE FROM jobs WHERE id=?").bind(id).run();
  await enqueueReclaim(env, row.run_id);
  return json({ ok: true });
}

export async function deleteAll(env) {
  const rows = await env.DB.prepare(
    "SELECT id, input_key, output_key, status, run_id FROM jobs WHERE status IN ('done','failed','canceled','rejected')",
  ).all();
  for (const row of rows.results) {
    await purgeJobObjects(env, row);
    await env.DB.prepare("DELETE FROM events WHERE job_id=?").bind(row.id).run();
    await env.DB.prepare("DELETE FROM jobs WHERE id=?").bind(row.id).run();
    await enqueueReclaim(env, row.run_id);
  }
  return json({ ok: true, deleted: rows.results.length });
}

export const QUALITIES = ["low", "mid", "high", "max"];
export const ANIM_COUNTS = ["few", "default", "max"];
// The five V6 multi-format toggles (videos_extra..textos) default ON like the
// rest; coerceFeatures/rowFeatures fill them in for legacy snapshots that
// predate them, so old settings rows and old job rows keep every format on.
export const DEFAULT_FEATURES = {
  captions: true, camera: true, animations: true, music: true, sfx: true,
  carousel: true, quotes: true,
  videos_extra: true, video_carruseles: true, carruseles_extra: true,
  imagenes: true, posters: true, textos: true,
};
const FEATURE_KEYS = Object.keys(DEFAULT_FEATURES);

function coerceFeatures(obj) {
  const out = {};
  for (const key of FEATURE_KEYS) {
    out[key] = key in obj ? Boolean(obj[key]) : true;
  }
  return out;
}

export async function currentAnimQuality(env) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key='anim_quality'").first();
  const value = row && row.value;
  return QUALITIES.includes(value) ? value : "mid";
}

export async function currentFeatures(env) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key='features'").first();
  if (!row || !row.value) return DEFAULT_FEATURES;
  try {
    const parsed = JSON.parse(row.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return DEFAULT_FEATURES;
    return coerceFeatures(parsed);
  } catch {
    return DEFAULT_FEATURES;
  }
}

export async function currentAnimCount(env) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key='anim_count'").first();
  const value = row && row.value;
  return ANIM_COUNTS.includes(value) ? value : "default";
}

export async function getSettings(env, admin = true) {
  const rows = await env.DB.prepare("SELECT key, value FROM settings").all();
  const map = Object.fromEntries(rows.results.map((r) => [r.key, r.value]));
  let metrics = null;
  try {
    metrics = map.usage_metrics ? JSON.parse(map.usage_metrics) : null;
  } catch {
    metrics = null;
  }
  const out = {
    anim_quality: QUALITIES.includes(map.anim_quality) ? map.anim_quality : "mid",
    features: await currentFeatures(env),
    anim_count: await currentAnimCount(env),
  };
  // Claude usage/quota meters are operator-facing: hidden from customers.
  if (admin) out.usage_metrics = metrics;
  return json(out);
}

export async function upsertSetting(env, key, value) {
  await env.DB.prepare(
    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
  ).bind(key, value).run();
}

export async function putSettings(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const hasQuality = Object.prototype.hasOwnProperty.call(body, "anim_quality");
  const hasFeatures = Object.prototype.hasOwnProperty.call(body, "features");
  const hasAnimCount = Object.prototype.hasOwnProperty.call(body, "anim_count");

  if (hasQuality) {
    const q = String(body.anim_quality || "");
    if (!QUALITIES.includes(q)) return json({ error: "invalid anim_quality" }, 400);
  }
  if (hasFeatures) {
    const f = body.features;
    if (!f || typeof f !== "object" || Array.isArray(f)) {
      return json({ error: "invalid features" }, 400);
    }
  }
  if (hasAnimCount) {
    const c = String(body.anim_count || "");
    if (!ANIM_COUNTS.includes(c)) return json({ error: "invalid anim_count" }, 400);
  }

  if (hasQuality) await upsertSetting(env, "anim_quality", String(body.anim_quality));
  if (hasFeatures) await upsertSetting(env, "features", JSON.stringify(coerceFeatures(body.features)));
  if (hasAnimCount) await upsertSetting(env, "anim_count", String(body.anim_count));
  return json({ ok: true });
}

export async function debugJob(env, id) {
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(id).first();
  if (!job) return json({ error: "not found" }, 404);
  const events = await env.DB.prepare(
    "SELECT ts, source, event, detail FROM events WHERE job_id=? ORDER BY ts",
  ).bind(id).all();
  return json({ job, events: events.results });
}

// ---- server restart (dashboard button -> PC agent cycles the whole stack) ----
//
// Storage is the existing key-value `settings` table, no migration:
//   restart_requested_at  -- set by the user when they click the button
//   restart_done_at       -- set by the agent once it has acked the request
// The agent must ack (set restart_done_at = restart_requested_at) BEFORE it
// spawns the detached restart, never after -- see agent.py's
// check_and_apply_restart for the full correctness invariant.

export async function requestRestart(env) {
  await upsertSetting(env, "restart_requested_at", String(Date.now()));
  return json({ ok: true });
}

export async function getRestartControl(env) {
  const rows = await env.DB.prepare(
    "SELECT key, value FROM settings WHERE key IN ('restart_requested_at', 'restart_done_at')",
  ).all();
  const map = Object.fromEntries(rows.results.map((r) => [r.key, r.value]));
  const requested = Number(map.restart_requested_at) || 0;
  const done = Number(map.restart_done_at) || 0;
  return json({ restart_pending: requested > done, requested_at: requested });
}

export async function ackRestart(env) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key='restart_requested_at'").first();
  const requested = Number(row && row.value) || 0;
  await upsertSetting(env, "restart_done_at", String(requested));
  return json({ ok: true });
}
