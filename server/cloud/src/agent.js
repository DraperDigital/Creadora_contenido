import { json } from "./index.js";
import { presignUrl } from "./presign.js";
import { notifyJobTransition } from "./notify.js";
import {
  logEvent, QUALITIES, currentAnimQuality, ANIM_COUNTS, DEFAULT_FEATURES,
} from "./jobs.js";

const ACTIVE = ["claimed", "processing", "publishing"];
export const OUTPUT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const STAGES = [
  "desilence", "transcribe", "edit_cut", "apply_cut", "plan",
  "animate", "captions", "render", "quotes", "carousel", "finalize", "quota",
];

// Whitelisted keys for the optional stage_detail progress object
// ("Animando escena n de t"). Anything else is dropped; a detail that ends up
// empty or oversized is ignored entirely.
const STAGE_DETAIL_KEYS = ["scene", "total", "pct"];
const STAGE_DETAIL_MAX_LEN = 200;

function sanitizeStageDetail(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = {};
  for (const key of STAGE_DETAIL_KEYS) {
    if (!(key in raw)) continue;
    const value = Number(raw[key]);
    if (!Number.isFinite(value) || value < 0) return null;
    out[key] = value;
  }
  if (!Object.keys(out).length) return null;
  const serialized = JSON.stringify(out);
  if (serialized.length > STAGE_DETAIL_MAX_LEN) return null;
  return serialized;
}

function rowFeatures(row) {
  // A legacy/NULL snapshot resolves to all-on defaults, NEVER the live panel —
  // an edit must not restructure a video by dropping features it originally had.
  if (row.features) {
    try {
      const parsed = JSON.parse(row.features);
      // Merge over defaults so a legacy snapshot that predates a newer toggle
      // (e.g. music/sfx) treats the missing keys as ON, never dropping them.
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return { ...DEFAULT_FEATURES, ...parsed };
      }
    } catch {
      // fall through to defaults
    }
  }
  return { ...DEFAULT_FEATURES };
}

async function withUrls(env, row, animQuality) {
  // A job's own snapshot (taken at create/changes time) wins over the panel's
  // current setting; anim_quality is the only field with a live-panel fallback
  // (deliberately re-read on edits). features/anim_count resolve to the row's
  // snapshot or all-on/default — never the live panel.
  if (QUALITIES.includes(row.anim_quality)) animQuality = row.anim_quality;
  const rowAnimCount = ANIM_COUNTS.includes(row.anim_count) ? row.anim_count : "default";
  const kind = row.kind || "new";
  return {
    id: row.id,
    filename: row.filename,
    format: row.format,
    status: row.status,
    kind,
    target: row.target || null,
    instructions: row.instructions || null,
    run_id: row.run_id || null,
    parent_run_id: row.parent_run_id || null,
    cancel_requested: row.cancel_requested ? 1 : 0,
    anim_quality: animQuality,
    features: rowFeatures(row),
    anim_count: rowAnimCount,
    input_url: kind === "edit" || !row.input_key
      ? null
      : await presignUrl(env, "GET", row.input_key, 86400),
  };
}

export async function claim(env) {
  const now = Date.now();
  const row = await env.DB.prepare(
    `UPDATE jobs SET status='claimed', claimed_at=?, heartbeat_at=?, updated_at=?
     WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1)
     RETURNING *`,
  ).bind(now, now, now).first();
  if (!row) return new Response(null, { status: 204 });
  await logEvent(env, row.id, "agent", "claimed");
  const animQuality = await currentAnimQuality(env);
  return json(await withUrls(env, row, animQuality));
}

export async function activeJobs(env) {
  const rows = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status IN (${ACTIVE.map(() => "?").join(",")}) ORDER BY created_at`,
  ).bind(...ACTIVE).all();
  const animQuality = await currentAnimQuality(env);
  return json(await Promise.all(rows.results.map((r) => withUrls(env, r, animQuality))));
}

export async function agentStatus(request, env, id, ctx = null) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const status = String(body.status || "");
  if (!["processing", "publishing", "failed", "canceled", "rejected"].includes(status)) {
    return json({ error: "invalid status" }, 400);
  }
  const now = Date.now();
  const error = ["failed", "rejected"].includes(status)
    ? String(body.error || "").slice(0, 4000)
    : null;
  const stage = STAGES.includes(String(body.stage || "")) ? String(body.stage) : null;
  const stageDetail = sanitizeStageDetail(body.stage_detail);
  // 'rejected' is terminal like canceled (no requeue, cron-expired), but kept
  // distinct so the dashboard can render an amber "no se pudo aplicar" card.
  const terminal = status === "canceled" || status === "rejected";
  const clearFlag = terminal || status === "failed";
  const res = await env.DB.prepare(
    `UPDATE jobs SET status=?, error=?, heartbeat_at=?, updated_at=?,
     stage=CASE WHEN ? THEN NULL ELSE COALESCE(?, stage) END,
     stage_detail=CASE WHEN ? THEN NULL ELSE COALESCE(?, stage_detail) END,
     cancel_requested=CASE WHEN ? THEN 0 ELSE cancel_requested END
     WHERE id=? AND status NOT IN ('done','canceled','rejected')`,
  ).bind(status, error, now, now, terminal, stage, terminal, stageDetail, clearFlag, id).run();
  if (!res.meta.changes) return json({ error: "unknown job" }, 404);
  await logEvent(env, id, "agent", "status:" + status, stage || body.error || null);
  if (status === "failed" || status === "rejected") {
    notifyJobTransition(env, ctx, id, status, { error });
  }
  return json({ ok: true });
}

export async function uploadUrl(request, env, id) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const name = String(body.name || "");
  if (!OUTPUT_NAME.test(name)) return json({ error: "invalid name" }, 400);
  const row = await env.DB.prepare("SELECT id FROM jobs WHERE id=?").bind(id).first();
  if (!row) return json({ error: "unknown job" }, 404);
  const key = `outbox/${id}/${name}`;
  return json({ key, url: await presignUrl(env, "PUT", key, 86400) });
}

function outputSize(o) {
  const size = Number(o.size);
  return Number.isFinite(size) && size >= 0 ? size : undefined;
}

export async function complete(request, env, id, ctx = null) {
  let body = {};
  try {
    body = await request.json();
  } catch {
    body = {};
  }
  const outputs = (Array.isArray(body.outputs) ? body.outputs : [])
    .filter((o) => o && OUTPUT_NAME.test(String(o.name || ""))
      && String(o.key || "") === `outbox/${id}/${o.name}`)
    .map((o) => {
      const entry = { name: String(o.name), key: String(o.key), kind: String(o.kind || "file") };
      const size = outputSize(o);
      if (size !== undefined) entry.size = size;
      return entry;
    });
  const video = outputs.find((o) => o.kind === "video");
  const runId = body.run_id ? String(body.run_id).slice(0, 40) : null;
  const tokensTotal = Number.isInteger(body.tokens_total) && body.tokens_total >= 0
    ? body.tokens_total
    : null;
  const now = Date.now();
  const res = await env.DB.prepare(
    `UPDATE jobs SET status='done', output_key=?, outputs=?, run_id=COALESCE(?, run_id),
     tokens_total=?, cancel_requested=0, stage=NULL, stage_detail=NULL,
     error=NULL, heartbeat_at=?, updated_at=? WHERE id=? AND status NOT IN ('done','canceled','rejected')`,
  ).bind(
    video ? video.key : `outbox/${id}.mp4`,
    outputs.length ? JSON.stringify(outputs) : null,
    runId, tokensTotal, now, now, id,
  ).run();
  if (!res.meta.changes) return json({ error: "unknown job" }, 404);
  // The customer input is deliberately KEPT in R2 (durability: re-edits and
  // retries can still reach it); the 30-day cron expiry collects it with the
  // rest of the job's objects.
  await logEvent(env, id, "agent", "complete", runId);
  notifyJobTransition(env, ctx, id, "done");
  return json({ ok: true });
}

export async function agentMetrics(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const payload = JSON.stringify({
    five_hour_pct: num(body.five_hour_pct),
    five_hour_resets_at: body.five_hour_resets_at ? String(body.five_hour_resets_at).slice(0, 40) : null,
    seven_day_pct: num(body.seven_day_pct),
    seven_day_resets_at: body.seven_day_resets_at ? String(body.seven_day_resets_at).slice(0, 40) : null,
    reported_at: Date.now(),
  });
  await env.DB.prepare(
    "INSERT INTO settings (key, value) VALUES ('usage_metrics', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
  ).bind(payload).run();
  return json({ ok: true });
}

export async function listReclaims(env) {
  // Run ids of deleted versions whose local disk the PC agent still has to
  // reclaim. Bounded so one poll never returns an unbounded backlog.
  const rows = await env.DB.prepare(
    "SELECT run_id FROM reclaims ORDER BY created_at LIMIT 500",
  ).all();
  return json({ run_ids: rows.results.map((r) => r.run_id) });
}

export async function ackReclaims(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const runIds = (Array.isArray(body.run_ids) ? body.run_ids : [])
    .filter((r) => typeof r === "string" && r);
  for (const runId of runIds) {
    await env.DB.prepare("DELETE FROM reclaims WHERE run_id=?").bind(runId).run();
  }
  return json({ ok: true, acked: runIds.length });
}
