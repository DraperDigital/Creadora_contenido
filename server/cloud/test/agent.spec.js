import { SELF, env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { login } from "./helpers.js";

const AGENT = { Authorization: "Bearer test-agent-token" };

async function queuedJob(cookie) {
  const { id } = await (await SELF.fetch("https://x.local/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie },
    body: JSON.stringify({ filename: "a.mp4", size_bytes: 1 }),
  })).json();
  await SELF.fetch(`https://x.local/api/jobs/${id}/finalize`, { method: "POST", headers: { Cookie: cookie } });
  return id;
}

async function createQueuedJob() {
  const cookie = await login();
  return queuedJob(cookie);
}

describe("agent api", () => {
  it("claim returns 204 when nothing is queued", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    expect(res.status).toBe(204);
  });

  it("claim returns the oldest queued job with presigned urls", async () => {
    const cookie = await login();
    const first = await queuedJob(cookie);
    await queuedJob(cookie);
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    expect(res.status).toBe(200);
    const job = await res.json();
    expect(job.id).toBe(first);
    expect(job.input_url).toContain(`/bionico-media/inbox/${first}/video.mp4`);
    expect(job.output_put_url).toBeUndefined();
    const row = await env.DB.prepare("SELECT status, heartbeat_at FROM jobs WHERE id=?").bind(first).first();
    expect(row.status).toBe("claimed");
    expect(row.heartbeat_at).toBeGreaterThan(0);
  });

  it("two sequential claims return different jobs", async () => {
    const cookie = await login();
    await queuedJob(cookie);
    await queuedJob(cookie);
    const a = await (await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT })).json();
    const b = await (await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT })).json();
    expect(a.id).not.toBe(b.id);
  });

  it("status updates heartbeat and error", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "failed", error: "boom log tail" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, error FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("failed");
    expect(row.error).toBe("boom log tail");
  });

  it("rejects an invalid status value", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "done" }),
    });
    expect(res.status).toBe(400);
  });

  it("complete marks done with the outbox key", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, output_key FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("done");
    expect(row.output_key).toBe(`outbox/${id}.mp4`);
  });

  it("complete keeps the input object in storage (cron expiry collects it later)", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await env.MEDIA.put(`inbox/${id}/video.mp4`, "bytes");
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    expect(res.status).toBe(200);
    // head() (not get()) — an unconsumed R2 body stream trips the Windows
    // isolated-storage teardown (see the note in cron.spec.js).
    expect(await env.MEDIA.head(`inbox/${id}/video.mp4`)).not.toBeNull();
  });

  it("complete does not modify an already-done job", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "failed", error: "should not apply" }),
    });
    expect(res.status).toBe(404);
    const row = await env.DB.prepare("SELECT status, error FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("done");
    expect(row.error).toBeNull();
  });

  it("active list returns claimed/processing/publishing jobs", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch("https://x.local/api/agent/jobs", { headers: AGENT });
    const jobs = await res.json();
    expect(jobs.map((j) => j.id)).toContain(id);
    expect(jobs[0].input_url).toBeTruthy();
  });
});

describe("upload-url", () => {
  it("returns a presigned PUT under outbox/<id>/", async () => {
    const id = await createQueuedJob();
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/upload-url`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ name: "quote_1.mp4" }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.key).toBe(`outbox/${id}/quote_1.mp4`);
    expect(new URL(body.url).pathname).toContain(`outbox/${id}/quote_1.mp4`);
  });

  it("rejects a path-traversal name", async () => {
    const id = await createQueuedJob();
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/upload-url`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ name: "../evil.mp4" }),
    });
    expect(res.status).toBe(400);
  });

  it("returns 404 for an unknown job", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/jobs/deadbeefdead/upload-url", {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ name: "quote_1.mp4" }),
    });
    expect(res.status).toBe(404);
  });
});

describe("complete with manifest", () => {
  it("stores outputs, run_id and the video output_key", async () => {
    const id = await createQueuedJob();
    const outputs = [
      { name: "final_7.mp4", key: `outbox/${id}/final_7.mp4`, kind: "video" },
      { name: "quote_1.mp4", key: `outbox/${id}/quote_1.mp4`, kind: "quote" },
    ];
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: "7_short", outputs }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("done");
    expect(row.run_id).toBe("7_short");
    expect(row.output_key).toBe(`outbox/${id}/final_7.mp4`);
    expect(JSON.parse(row.outputs)).toEqual(outputs);
  });

  it("drops manifest entries whose key is outside outbox/<id>/", async () => {
    const id = await createQueuedJob();
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ outputs: [{ name: "x.mp4", key: "inbox/steal/video.mp4", kind: "video" }] }),
    });
    const row = await env.DB.prepare("SELECT outputs, output_key FROM jobs WHERE id=?").bind(id).first();
    expect(row.outputs).toBeNull();
    expect(row.output_key).toBe(`outbox/${id}.mp4`); // legacy fallback
  });

  it("still supports a no-body complete with the legacy outbox key (input retained)", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await env.MEDIA.put(`inbox/${id}/video.mp4`, "bytes");
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, output_key, outputs FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("done");
    expect(row.output_key).toBe(`outbox/${id}.mp4`);
    expect(row.outputs).toBeNull();
    expect(await env.MEDIA.head(`inbox/${id}/video.mp4`)).not.toBeNull();
  });
});

describe("agent status: stage and cancel", () => {
  it("stores a valid stage alongside processing", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage: "animate" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, stage FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("processing");
    expect(row.stage).toBe("animate");
  });

  it("ignores an invalid stage value", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage: "not_a_real_stage" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT stage FROM jobs WHERE id=?").bind(id).first();
    expect(row.stage).toBeNull();
  });

  it("accepts a terminal canceled status and clears the cancel flag and stage", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await env.DB.prepare("UPDATE jobs SET cancel_requested=1, stage='render' WHERE id=?").bind(id).run();
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "canceled" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, cancel_requested, stage FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("canceled");
    expect(row.cancel_requested).toBe(0);
    expect(row.stage).toBeNull();
  });

  it("clears the cancel flag when a failed status arrives on a flagged row", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await env.DB.prepare("UPDATE jobs SET cancel_requested=1 WHERE id=?").bind(id).run();
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "failed", error: "boom" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, cancel_requested FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("failed");
    expect(row.cancel_requested).toBe(0);
  });
});

describe("claim payload includes cancel_requested", () => {
  it("surfaces cancel_requested on the claim response", async () => {
    const cookie = await login();
    await queuedJob(cookie);
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.cancel_requested).toBe(0);
  });
});

describe("complete with tokens_total and output size", () => {
  it("stores tokens_total and preserves numeric size on outputs", async () => {
    const id = await createQueuedJob();
    const outputs = [
      { name: "final_7.mp4", key: `outbox/${id}/final_7.mp4`, kind: "video", size: 123456 },
    ];
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: "7_short", outputs, tokens_total: 12345 }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT tokens_total, outputs FROM jobs WHERE id=?").bind(id).first();
    expect(row.tokens_total).toBe(12345);
    const stored = JSON.parse(row.outputs);
    expect(stored[0].size).toBe(123456);
  });

  it("drops a non-finite size but keeps the entry", async () => {
    const id = await createQueuedJob();
    const outputs = [
      { name: "final_7.mp4", key: `outbox/${id}/final_7.mp4`, kind: "video", size: -1 },
    ];
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ outputs }),
    });
    const row = await env.DB.prepare("SELECT outputs FROM jobs WHERE id=?").bind(id).first();
    const stored = JSON.parse(row.outputs);
    expect(stored[0].size).toBeUndefined();
    expect(stored[0].name).toBe("final_7.mp4");
  });
});

describe("edit claim payload", () => {
  it("includes kind/target/instructions/run_id and no input_url", async () => {
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, kind, target, instructions, run_id, created_at, updated_at)
       VALUES ('e1e1e1e1e1e1', 'v.mp4', 'short', 'queued', 'inbox/e1e1e1e1e1e1/video.mp4', 'edit', 'carousel', 'make it blue', '7_short', ?, ?)`,
    ).bind(now, now).run();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.kind).toBe("edit");
    expect(job.target).toBe("carousel");
    expect(job.instructions).toBe("make it blue");
    expect(job.run_id).toBe("7_short");
    expect(job.input_url).toBeNull();
  });
});

describe("claim/activeJobs anim_quality", () => {
  it("defaults anim_quality to mid on the claim response", async () => {
    const id = await createQueuedJob();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe(id);
    expect(job.anim_quality).toBe("mid");
  });

  it("reflects the configured anim_quality on claim and active jobs", async () => {
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('anim_quality', 'high') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).run();
    const id = await createQueuedJob();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.anim_quality).toBe("high");
    const activeRes = await SELF.fetch("https://x.local/api/agent/jobs", { headers: AGENT });
    const jobs = await activeRes.json();
    expect(jobs.find((j) => j.id === id).anim_quality).toBe("high");
  });
});

describe("agent metrics", () => {
  it("stores metrics reachable via GET /api/settings with reported_at", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/metrics", {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({
        five_hour_pct: 42,
        five_hour_resets_at: "2026-07-04T12:00:00Z",
        seven_day_pct: 10,
        seven_day_resets_at: "2026-07-10T00:00:00Z",
      }),
    });
    expect(res.status).toBe(200);
    const cookie = await login();
    const settings = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(settings.usage_metrics.five_hour_pct).toBe(42);
    expect(settings.usage_metrics.seven_day_pct).toBe(10);
    expect(settings.usage_metrics.reported_at).toBeGreaterThan(0);
  });

  it("rejects a bad-json metrics body", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/metrics", {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: "not json",
    });
    expect(res.status).toBe(400);
  });

  it("blocks metrics without the bearer token", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ five_hour_pct: 1 }),
    });
    expect(res.status).toBe(401);
  });
});

describe("anim quality snapshot on claim", () => {
  async function setQuality(q) {
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('anim_quality', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).bind(q).run();
  }

  it("prefers the job's snapshot over the current setting", async () => {
    await setQuality("high");
    const id = await createQueuedJob(); // snapshots high at create
    await setQuality("low");            // user flips the selector afterwards
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe(id);
    expect(job.anim_quality).toBe("high");
  });

  it("falls back to the current setting for pre-snapshot rows", async () => {
    await setQuality("max");
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, created_at, updated_at)
       VALUES ('feedbeef0001', 'old.mp4', 'short', 'queued', 'inbox/feedbeef0001/video.mp4', ?, ?)`,
    ).bind(now, now).run();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe("feedbeef0001");
    expect(job.anim_quality).toBe("max");
  });
});

describe("claim payload features + anim_count", () => {
  async function setFeatures(features) {
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('features', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).bind(JSON.stringify(features)).run();
  }
  async function setAnimCount(v) {
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('anim_count', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).bind(v).run();
  }

  it("defaults features all-true and anim_count 'default' on claim", async () => {
    const id = await createQueuedJob();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe(id);
    expect(job.features).toEqual({ captions: true, camera: true, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("default");
  });

  it("prefers the job's own snapshot over the current setting (row wins)", async () => {
    await setFeatures({ captions: true, camera: false, animations: true });
    await setAnimCount("few");
    const id = await createQueuedJob(); // snapshots camera:false / few at create
    await setFeatures({ captions: true, camera: true, animations: true });
    await setAnimCount("max");
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe(id);
    expect(job.features).toEqual({ captions: true, camera: false, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("few");
  });

  it("legacy rows (features/anim_count NULL) resolve to all-on/default, NOT the live panel", async () => {
    await setFeatures({ captions: false, camera: true, animations: true });
    await setAnimCount("max");
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, created_at, updated_at)
       VALUES ('feedbeef0002', 'old.mp4', 'short', 'queued', 'inbox/feedbeef0002/video.mp4', ?, ?)`,
    ).bind(now, now).run();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe("feedbeef0002");
    expect(job.features).toEqual({ captions: true, camera: true, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("default");
  });

  it("a legacy edit (kind=edit, run_id set, features NULL) never inherits the live panel", async () => {
    await setFeatures({ captions: false, camera: true, animations: true });
    await setAnimCount("max");
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, kind, target, instructions, run_id, created_at, updated_at)
       VALUES ('feedbeef0003', 'legacy.mp4', 'short', 'queued', 'inbox/feedbeef0003/video.mp4', 'edit', 'video', 'tweak it', '9_short', ?, ?)`,
    ).bind(now, now).run();
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const job = await res.json();
    expect(job.id).toBe("feedbeef0003");
    expect(job.kind).toBe("edit");
    expect(job.features).toEqual({ captions: true, camera: true, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("default");
  });

  it("surfaces features + anim_count on activeJobs too", async () => {
    await setFeatures({ captions: true, camera: true, music: false, animations: true });
    await setAnimCount("few");
    const id = await createQueuedJob();
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch("https://x.local/api/agent/jobs", { headers: AGENT });
    const jobs = await res.json();
    const job = jobs.find((j) => j.id === id);
    expect(job.features).toEqual({ captions: true, camera: true, music: false, animations: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("few");
  });
});

describe("stage_detail progress", () => {
  it("roundtrips a whitelisted stage_detail from status POST to listJobs", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage: "animate", stage_detail: { scene: 2, total: 5 } }),
    });
    expect(res.status).toBe(200);
    const list = await (await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).json();
    const job = list.find((j) => j.id === id);
    expect(job.stage_detail).toEqual({ scene: 2, total: 5 });
  });

  it("drops non-whitelisted keys and ignores a non-numeric detail entirely", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage_detail: { scene: 1, total: 4, evil: "x".repeat(500) } }),
    });
    let row = await env.DB.prepare("SELECT stage_detail FROM jobs WHERE id=?").bind(id).first();
    expect(JSON.parse(row.stage_detail)).toEqual({ scene: 1, total: 4 });
    // A garbage detail is ignored (COALESCE keeps the previous one).
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage_detail: { scene: "not-a-number" } }),
    });
    row = await env.DB.prepare("SELECT stage_detail FROM jobs WHERE id=?").bind(id).first();
    expect(JSON.parse(row.stage_detail)).toEqual({ scene: 1, total: 4 });
  });

  it("clears stage_detail on complete", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage_detail: { scene: 3, total: 3 } }),
    });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    const row = await env.DB.prepare("SELECT status, stage_detail FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("done");
    expect(row.stage_detail).toBeNull();
  });
});

describe("rejected status", () => {
  it("stores rejected with the reason and clears stage", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage: "animate" }),
    });
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "rejected", error: "el cambio pedido no se puede aplicar" }),
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, error, stage, cancel_requested FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("rejected");
    expect(row.error).toBe("el cambio pedido no se puede aplicar");
    expect(row.stage).toBeNull();
    expect(row.cancel_requested).toBe(0);
    // Distinct terminal status surfaces on the dashboard list.
    const list = await (await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).json();
    expect(list.find((j) => j.id === id).status).toBe("rejected");
  });

  it("is terminal: no later status update or complete can resurrect it", async () => {
    const cookie = await login();
    const id = await queuedJob(cookie);
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: AGENT });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "rejected", error: "no aplicable" }),
    });
    const late = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing" }),
    });
    expect(late.status).toBe(404);
    const done = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, { method: "POST", headers: AGENT });
    expect(done.status).toBe(404);
    const row = await env.DB.prepare("SELECT status FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("rejected");
  });
});

describe("agent reclaims", () => {
  it("lists queued reclaims oldest-first and acks the ones freed", async () => {
    await env.DB.prepare(
      "INSERT INTO reclaims (run_id, created_at) VALUES ('1_short', 1), ('2_short', 2)",
    ).run();
    const list = await (await SELF.fetch("https://x.local/api/agent/reclaims", { headers: AGENT })).json();
    expect(list.run_ids).toEqual(["1_short", "2_short"]);

    const ack = await SELF.fetch("https://x.local/api/agent/reclaims/ack", {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ run_ids: ["1_short"] }),
    });
    expect(ack.status).toBe(200);
    expect(await ack.json()).toEqual({ ok: true, acked: 1 });

    const after = await (await SELF.fetch("https://x.local/api/agent/reclaims", { headers: AGENT })).json();
    expect(after.run_ids).toEqual(["2_short"]); // only the un-acked one remains
  });

  it("ignores non-string run ids in an ack body", async () => {
    await env.DB.prepare("INSERT INTO reclaims (run_id, created_at) VALUES ('3_short', 1)").run();
    const ack = await SELF.fetch("https://x.local/api/agent/reclaims/ack", {
      method: "POST",
      headers: { ...AGENT, "Content-Type": "application/json" },
      body: JSON.stringify({ run_ids: [42, null, "3_short"] }),
    });
    expect(await ack.json()).toEqual({ ok: true, acked: 1 });
    const after = await (await SELF.fetch("https://x.local/api/agent/reclaims", { headers: AGENT })).json();
    expect(after.run_ids).toEqual([]);
  });

  it("requires the agent bearer for the reclaim endpoints", async () => {
    expect((await SELF.fetch("https://x.local/api/agent/reclaims")).status).toBe(401);
    expect((await SELF.fetch("https://x.local/api/agent/reclaims/ack", { method: "POST" })).status).toBe(401);
  });
});
