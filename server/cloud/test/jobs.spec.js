import { SELF, env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { login } from "./helpers.js";

async function createJob(cookie, filename = "clip.mp4") {
  const res = await SELF.fetch("https://x.local/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie },
    body: JSON.stringify({ filename, size_bytes: 123 }),
  });
  return res;
}

describe("jobs api", () => {
  it("creates a job in uploading with an upload url", async () => {
    const cookie = await login();
    const res = await createJob(cookie);
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.id).toMatch(/^[0-9a-f]{12}$/);
    expect(body.upload_url).toContain(`/bionico-media/inbox/${body.id}/video.mp4`);
    const list = await (await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).json();
    const job = list.find((j) => j.id === body.id);
    expect(job.status).toBe("uploading");
    expect(job.filename).toBe("clip.mp4");
  });

  it("finalize moves uploading -> queued", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/finalize`, {
      method: "POST", headers: { Cookie: cookie },
    });
    expect(res.status).toBe(200);
    const list = await (await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).json();
    expect(list.find((j) => j.id === id).status).toBe("queued");
  });

  it("retry moves failed -> queued and clears error", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    await env.DB.prepare("UPDATE jobs SET status='failed', error='boom' WHERE id=?").bind(id).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/retry`, {
      method: "POST", headers: { Cookie: cookie },
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, error FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("queued");
    expect(row.error).toBeNull();
  });

  it("retry rejects a job that is not failed", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/retry`, {
      method: "POST", headers: { Cookie: cookie },
    });
    expect(res.status).toBe(409);
  });

  it("redirects the result to a presigned R2 GET when done", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    await env.MEDIA.put(`outbox/${id}.mp4`, "FAKE-MP4-BYTES");
    await env.DB.prepare("UPDATE jobs SET status='done', output_key=? WHERE id=?")
      .bind(`outbox/${id}.mp4`, id).run();
    // Downloads redirect to R2 so the client streams directly (Range/resume),
    // instead of proxying the whole body through the Worker. `redirect: manual`
    // so we inspect the 302 rather than chasing it to real R2.
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/result`, {
      headers: { Cookie: cookie }, redirect: "manual",
    });
    expect(res.status).toBe(302);
    const loc = res.headers.get("Location");
    expect(loc).toContain("r2.cloudflarestorage.com");
    expect(loc).toContain(`outbox/${id}.mp4`);
    expect(loc).toContain("X-Amz-Signature");
    expect(loc).toContain("response-content-disposition");
  });

  it("404s the result when not done", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/result`, { headers: { Cookie: cookie } });
    expect(res.status).toBe(404);
  });

  it("redirects an mp4 deliverable to R2 but keeps caption text proxied", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    await env.MEDIA.put(`outbox/${id}/final.mp4`, "FAKE-MP4");
    await env.MEDIA.put(`outbox/${id}/caption.txt`, "hola mundo");
    await env.DB.prepare("UPDATE jobs SET status='done', outputs=? WHERE id=?")
      .bind(JSON.stringify([
        { name: "final.mp4", key: `outbox/${id}/final.mp4`, kind: "video" },
        { name: "caption.txt", key: `outbox/${id}/caption.txt`, kind: "caption" },
      ]), id).run();
    // mp4 -> 302 to R2 (direct download, Range/resume)
    const vid = await SELF.fetch(`https://x.local/api/jobs/${id}/files/final.mp4`, {
      headers: { Cookie: cookie }, redirect: "manual",
    });
    expect(vid.status).toBe(302);
    expect(vid.headers.get("Location")).toContain("r2.cloudflarestorage.com");
    // caption text stays proxied through the Worker (its fetch()-based preview
    // must keep working without cross-origin CORS on the bucket)
    const cap = await SELF.fetch(`https://x.local/api/jobs/${id}/files/caption.txt`, {
      headers: { Cookie: cookie },
    });
    expect(cap.status).toBe(200);
    expect(await cap.text()).toBe("hola mundo");
  });
});

describe("upload formats and size cap", () => {
  async function post(cookie, body) {
    return SELF.fetch("https://x.local/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify(body),
    });
  }

  it("accepts a .mov upload and keys it with the .mov extension", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "clip.MOV", size_bytes: 123 });
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.upload_url).toContain(`/bionico-media/inbox/${body.id}/video.mov`);
    const row = await env.DB.prepare("SELECT input_key FROM jobs WHERE id=?").bind(body.id).first();
    expect(row.input_key).toBe(`inbox/${body.id}/video.mov`);
  });

  it("accepts a .m4v upload", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "clip.m4v", size_bytes: 123 });
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.upload_url).toContain(`/bionico-media/inbox/${body.id}/video.m4v`);
  });

  it("accepts an unknown extension when the declared content type is video/*, keyed as .mp4", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "clip.webm", content_type: "video/webm", size_bytes: 123 });
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.upload_url).toContain(`/bionico-media/inbox/${body.id}/video.mp4`);
  });

  it("rejects a non-video upload", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "doc.pdf", content_type: "application/pdf", size_bytes: 123 });
    expect(res.status).toBe(400);
  });

  it("rejects a declared size over the cap (default 2048 MB) with 413", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "big.mp4", size_bytes: 2049 * 1024 * 1024 });
    expect(res.status).toBe(413);
    const body = await res.json();
    expect(body.error).toContain("2048 MB");
    const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM jobs").first();
    expect(count.n).toBe(0);
  });

  it("accepts a declared size just under the cap", async () => {
    const cookie = await login();
    const res = await post(cookie, { filename: "big.mp4", size_bytes: 2048 * 1024 * 1024 });
    expect(res.status).toBe(201);
  });
});

describe("request changes", () => {
  async function doneJobWithRun(id = "abcdefabcdef") {
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, output_key, run_id, outputs, lineage_id, version_no, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', 'inbox/x/video.mp4', ?, '7_short', ?, ?, 1, ?, ?)`,
    ).bind(id, `outbox/${id}/final_7.mp4`,
      JSON.stringify([{ name: "final_7.mp4", key: `outbox/${id}/final_7.mp4`, kind: "video" }]),
      id, now, now).run();
    return id;
  }

  it("creates a new version (child) and leaves the parent intact", async () => {
    const cookie = await login();
    const id = await doneJobWithRun();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ target: "quotes", instructions: "shorter quotes please" }),
    });
    expect(res.status).toBe(201);
    const { id: childId, version_no } = await res.json();
    expect(version_no).toBe(2);
    // Parent stays done and downloadable — never overwritten.
    const parent = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(id).first();
    expect(parent.status).toBe("done");
    expect(parent.version_no).toBe(1);
    expect(parent.outputs).not.toBeNull();
    // Child is a queued edit in the same lineage, pointing at the parent's run.
    const child = await env.DB.prepare("SELECT * FROM jobs WHERE id=?").bind(childId).first();
    expect(child.status).toBe("queued");
    expect(child.kind).toBe("edit");
    expect(child.target).toBe("quotes");
    expect(child.instructions).toBe("shorter quotes please");
    expect(child.lineage_id).toBe(id);
    expect(child.parent_id).toBe(id);
    expect(child.parent_run_id).toBe("7_short");
  });

  it("rejects changes when the job is not done or has no run_id", async () => {
    const cookie = await login();
    const id = "bbbbbbbbbbbb";
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', 'inbox/x/video.mp4', ?, ?)`,
    ).bind(id, now, now).run(); // done but run_id NULL
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ target: "video", instructions: "x" }),
    });
    expect(res.status).toBe(409);
  });

  it("rejects a bad target", async () => {
    const cookie = await login();
    const id = await doneJobWithRun("cccccccccccc");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ target: "everything", instructions: "x" }),
    });
    expect(res.status).toBe(400);
  });
});

describe("files streaming", () => {
  it("streams a manifest-listed output with the right content type", async () => {
    const cookie = await login();
    const id = "dddddddddddd";
    const now = Date.now();
    await env.MEDIA.put(`outbox/${id}/carrusel_slide1.png`, "PNGBYTES");
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, outputs, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', ?, ?, ?, ?)`,
    ).bind(id, `inbox/${id}/video.mp4`,
      JSON.stringify([{ name: "carrusel_slide1.png", key: `outbox/${id}/carrusel_slide1.png`, kind: "slide" }]), now, now).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/files/carrusel_slide1.png`, { headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/png");
    expect(await res.text()).toBe("PNGBYTES");
  });

  it("404s a name not in the manifest", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/jobs/dddddddddddd/files/evil.mp4", { headers: { Cookie: cookie } });
    expect(res.status).toBe(404);
  });

  it("serves .srt subtitles and .pdf documents with their content types", async () => {
    const cookie = await login();
    const id = "ddddddddddd2";
    const now = Date.now();
    await env.MEDIA.put(`outbox/${id}/subtitulos.srt`, "1\n00:00:00,000 --> 00:00:01,000\nHola\n");
    await env.MEDIA.put(`outbox/${id}/carrusel_linkedin.pdf`, "%PDF-1.4 FAKE");
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, outputs, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', ?, ?, ?, ?)`,
    ).bind(id, `inbox/${id}/video.mp4`,
      JSON.stringify([
        { name: "subtitulos.srt", key: `outbox/${id}/subtitulos.srt`, kind: "caption" },
        { name: "carrusel_linkedin.pdf", key: `outbox/${id}/carrusel_linkedin.pdf`, kind: "pdf" },
      ]), now, now).run();
    const srt = await SELF.fetch(`https://x.local/api/jobs/${id}/files/subtitulos.srt`, { headers: { Cookie: cookie } });
    expect(srt.status).toBe(200);
    expect(srt.headers.get("Content-Type")).toBe("text/plain; charset=utf-8");
    expect(await srt.text()).toContain("Hola");
    const pdf = await SELF.fetch(`https://x.local/api/jobs/${id}/files/carrusel_linkedin.pdf`, { headers: { Cookie: cookie } });
    expect(pdf.status).toBe(200);
    expect(pdf.headers.get("Content-Type")).toBe("application/pdf");
    expect(await pdf.text()).toContain("%PDF");
  });
});

describe("edit-aware retry", () => {
  it("keeps outputs when retrying a failed edit", async () => {
    const cookie = await login();
    const id = "eeeeeeeeeeee";
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, kind, output_key, outputs, run_id, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'failed', ?, 'edit', ?, ?, '7_short', ?, ?)`,
    ).bind(id, `inbox/${id}/video.mp4`, `outbox/${id}/final_7.mp4`,
      JSON.stringify([{ name: "final_7.mp4", key: `outbox/${id}/final_7.mp4`, kind: "video" }]), now, now).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/retry`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, outputs, output_key FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("queued");
    expect(row.outputs).not.toBeNull();
    expect(row.output_key).not.toBeNull();
  });

  it("clears cancel_requested and stage when retrying a flagged failed job", async () => {
    const cookie = await login();
    const id = "eeeeeeeeeeef";
    await insertJob(id, "failed", { cancel_requested: 1 });
    await env.DB.prepare("UPDATE jobs SET stage='render' WHERE id=?").bind(id).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/retry`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, cancel_requested, stage FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("queued");
    expect(row.cancel_requested).toBe(0);
    expect(row.stage).toBeNull();
  });
});

async function insertJob(id, status, fields = {}) {
  const now = Date.now();
  await env.DB.prepare(
    `INSERT INTO jobs (id, filename, format, status, input_key, output_key, created_at, updated_at, cancel_requested)
     VALUES (?, 'v.mp4', 'short', ?, ?, ?, ?, ?, ?)`,
  ).bind(
    id, status, fields.input_key ?? `inbox/${id}/video.mp4`, fields.output_key ?? null,
    fields.created_at ?? now, fields.updated_at ?? now, fields.cancel_requested ?? 0,
  ).run();
}

describe("cancel", () => {
  it("cancels a queued job directly", async () => {
    const cookie = await login();
    const id = "111111111111";
    await insertJob(id, "queued");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/cancel`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ ok: true, canceled: true });
    const row = await env.DB.prepare("SELECT status, cancel_requested FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("canceled");
    expect(row.cancel_requested).toBe(0);
  });

  it("flags a live processing job (fresh heartbeat) instead of canceling directly", async () => {
    const cookie = await login();
    const id = "222222222222";
    await insertJob(id, "processing");
    // Fresh heartbeat => a live worker is on it: cancel is a flag it applies.
    await env.DB.prepare("UPDATE jobs SET heartbeat_at=? WHERE id=?").bind(Date.now(), id).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/cancel`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ ok: true, canceled: false });
    const row = await env.DB.prepare("SELECT status, cancel_requested FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("processing");
    expect(row.cancel_requested).toBe(1);
  });

  it("cancels a stuck processing job (stale heartbeat, no live worker) directly", async () => {
    const cookie = await login();
    const id = "222222222223";
    await insertJob(id, "processing");
    // Heartbeat 10 min old => the worker is dead/orphaned; there's nothing to
    // apply a flag, so the button must cancel it outright (the reported bug).
    await env.DB.prepare("UPDATE jobs SET heartbeat_at=? WHERE id=?")
      .bind(Date.now() - 10 * 60 * 1000, id).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/cancel`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ ok: true, canceled: true });
    const row = await env.DB.prepare("SELECT status, cancel_requested FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("canceled");
    expect(row.cancel_requested).toBe(0);
  });

  it("keeps a canceled job canceled when the agent later reports done (no resurrection)", async () => {
    const id = "222222222224";
    await insertJob(id, "canceled");
    // A late/restarted worker tries to complete it — must be rejected.
    const res = await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer test-agent-token" },
      body: JSON.stringify({ output_key: `outbox/${id}.mp4` }),
    });
    const row = await env.DB.prepare("SELECT status FROM jobs WHERE id=?").bind(id).first();
    expect(row.status).toBe("canceled");
  });

  it("rejects canceling a done job", async () => {
    const cookie = await login();
    const id = "333333333333";
    await insertJob(id, "done");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/cancel`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(409);
  });
});

describe("delete", () => {
  it("deletes a failed job, its events, and its R2 objects", async () => {
    const cookie = await login();
    const id = "444444444444";
    await insertJob(id, "failed", { input_key: `inbox/${id}/video.mp4` });
    await env.MEDIA.put(`inbox/${id}/video.mp4`, "in");
    await env.MEDIA.put(`outbox/${id}/x.png`, "out");
    await env.DB.prepare(
      "INSERT INTO events (job_id, ts, source, event, detail) VALUES (?, ?, 'user', 'created', NULL)",
    ).bind(id, Date.now()).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/delete`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id=?").bind(id).first()).toBeNull();
    expect(await env.DB.prepare("SELECT id FROM events WHERE job_id=?").bind(id).first()).toBeNull();
    expect(await env.MEDIA.get(`inbox/${id}/video.mp4`)).toBeNull();
    expect(await env.MEDIA.get(`outbox/${id}/x.png`)).toBeNull();
  });

  it("rejects deleting a processing job", async () => {
    const cookie = await login();
    const id = "555555555555";
    await insertJob(id, "processing");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/delete`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(409);
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id=?").bind(id).first()).not.toBeNull();
  });

  it("queues the deleted version's run for local reclaim", async () => {
    const cookie = await login();
    const id = "444400004444";
    await insertJob(id, "done");
    await env.DB.prepare("UPDATE jobs SET run_id='7_short' WHERE id=?").bind(id).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/delete`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT run_id FROM reclaims WHERE run_id='7_short'").first();
    expect(row).not.toBeNull();
  });

  it("does not queue a reclaim for a version that never ran", async () => {
    const cookie = await login();
    const id = "444400005555";
    await insertJob(id, "uploading"); // run_id stays NULL
    await SELF.fetch(`https://x.local/api/jobs/${id}/delete`, { method: "POST", headers: { Cookie: cookie } });
    const cnt = await env.DB.prepare("SELECT COUNT(*) AS c FROM reclaims").first();
    expect(cnt.c).toBe(0);
  });

  it("allows deleting an uploading job", async () => {
    const cookie = await login();
    const id = "666666666666";
    await insertJob(id, "uploading");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/delete`, { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id=?").bind(id).first()).toBeNull();
  });
});

describe("delete-all", () => {
  it("deletes only terminal jobs, leaving processing ones", async () => {
    const cookie = await login();
    await insertJob("777777777771", "done");
    await insertJob("777777777772", "failed");
    await insertJob("777777777773", "processing");
    const res = await SELF.fetch("https://x.local/api/jobs/delete-all", { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ ok: true, deleted: 2 });
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id='777777777771'").first()).toBeNull();
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id='777777777772'").first()).toBeNull();
    expect(await env.DB.prepare("SELECT id FROM jobs WHERE id='777777777773'").first()).not.toBeNull();
  });

  it("queues reclaims for every deleted version that ran", async () => {
    const cookie = await login();
    await insertJob("888800000001", "done");
    await insertJob("888800000002", "failed");
    await env.DB.prepare("UPDATE jobs SET run_id='1_short' WHERE id='888800000001'").run();
    await env.DB.prepare("UPDATE jobs SET run_id='2_short' WHERE id='888800000002'").run();
    await SELF.fetch("https://x.local/api/jobs/delete-all", { method: "POST", headers: { Cookie: cookie } });
    const rows = await env.DB.prepare("SELECT run_id FROM reclaims ORDER BY run_id").all();
    expect(rows.results.map((r) => r.run_id)).toEqual(["1_short", "2_short"]);
  });
});

describe("events audit trail", () => {
  it("logs at least 5 events across create -> finalize -> claim -> status -> complete", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    await SELF.fetch(`https://x.local/api/jobs/${id}/finalize`, { method: "POST", headers: { Cookie: cookie } });
    await SELF.fetch("https://x.local/api/agent/claim", { method: "POST", headers: { Authorization: "Bearer test-agent-token" } });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/status`, {
      method: "POST",
      headers: { Authorization: "Bearer test-agent-token", "Content-Type": "application/json" },
      body: JSON.stringify({ status: "processing", stage: "render" }),
    });
    await SELF.fetch(`https://x.local/api/agent/jobs/${id}/complete`, {
      method: "POST", headers: { Authorization: "Bearer test-agent-token" },
    });
    const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM events WHERE job_id=?").bind(id).first();
    expect(count.n).toBeGreaterThanOrEqual(5);
  });
});

describe("settings", () => {
  it("defaults anim_quality to mid, accepts a PUT, then reflects it on GET", async () => {
    const cookie = await login();
    const initial = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(initial.anim_quality).toBe("mid");
    const put = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_quality: "high" }),
    });
    expect(put.status).toBe(200);
    const after = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(after.anim_quality).toBe("high");
  });

  it("rejects an invalid anim_quality with 400", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_quality: "garbage" }),
    });
    expect(res.status).toBe(400);
  });

  it("blocks settings routes without a cookie", async () => {
    const res = await SELF.fetch("https://x.local/api/settings");
    expect(res.status).toBe(401);
  });
});

describe("debug endpoint", () => {
  it("returns the job and its ordered events", async () => {
    const cookie = await login();
    const { id } = await (await createJob(cookie)).json();
    await SELF.fetch(`https://x.local/api/jobs/${id}/finalize`, { method: "POST", headers: { Cookie: cookie } });
    const res = await SELF.fetch(`https://x.local/api/debug/jobs/${id}`, { headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.job.id).toBe(id);
    expect(body.events.length).toBeGreaterThanOrEqual(2);
    const timestamps = body.events.map((e) => e.ts);
    expect([...timestamps].sort((a, b) => a - b)).toEqual(timestamps);
  });

  it("404s an unknown job id", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/debug/jobs/deadbeefdead", { headers: { Cookie: cookie } });
    expect(res.status).toBe(404);
  });

  it("blocks the debug endpoint without a cookie", async () => {
    const res = await SELF.fetch("https://x.local/api/debug/jobs/deadbeefdead");
    expect(res.status).toBe(401);
  });
});

describe("anim quality snapshot", () => {
  async function setQuality(q) {
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('anim_quality', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).bind(q).run();
  }

  it("createJob snapshots the current setting onto the row", async () => {
    const cookie = await login();
    await setQuality("high");
    const res = await SELF.fetch("https://x.local/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ filename: "snap.mp4", size_bytes: 5 }),
    });
    const { id } = await res.json();
    const row = await env.DB.prepare("SELECT anim_quality FROM jobs WHERE id=?").bind(id).first();
    expect(row.anim_quality).toBe("high");
  });

  it("requestChanges snapshots quality onto the new version at request time", async () => {
    const cookie = await login();
    await setQuality("low");
    const id = "abcd1234ef56";
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, run_id, anim_quality, lineage_id, version_no, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', 'inbox/x/video.mp4', '7_short', 'max', ?, 1, ?, ?)`,
    ).bind(id, id, now, now).run();
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ target: "video", instructions: "mas rapido" }),
    });
    expect(res.status).toBe(201);
    const { id: childId } = await res.json();
    const child = await env.DB.prepare("SELECT anim_quality FROM jobs WHERE id=?").bind(childId).first();
    expect(child.anim_quality).toBe("low"); // re-read at request time
    const parent = await env.DB.prepare("SELECT anim_quality FROM jobs WHERE id=?").bind(id).first();
    expect(parent.anim_quality).toBe("max"); // parent untouched
  });
});

describe("feature toggles + anim-count snapshot", () => {
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

  it("createJob snapshots features + anim_count from settings onto the row", async () => {
    const cookie = await login();
    await setFeatures({ captions: true, camera: false, animations: true });
    await setAnimCount("few");
    const res = await createJob(cookie, "snap2.mp4");
    const { id } = await res.json();
    const row = await env.DB.prepare("SELECT features, anim_count FROM jobs WHERE id=?").bind(id).first();
    expect(JSON.parse(row.features)).toEqual({ captions: true, camera: false, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(row.anim_count).toBe("few");
  });

  it("the new version carries forward the parent's own features + anim_count (not the live panel)", async () => {
    const cookie = await login();
    await setFeatures({ captions: true, camera: false, animations: true });
    await setAnimCount("few");
    const id = "ffff1234ef56";
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, run_id, anim_quality, features, anim_count, lineage_id, version_no, created_at, updated_at)
       VALUES (?, 'v.mp4', 'short', 'done', 'inbox/x/video.mp4', '7_short', 'mid', ?, 'max', ?, 1, ?, ?)`,
    ).bind(id, JSON.stringify({ captions: false, camera: false, animations: false }), id, now, now).run();
    // Flip the global settings after the job was created — the child must not pick these up for features/anim_count.
    await setFeatures({ captions: true, camera: true, animations: true });
    await setAnimCount("max");
    const res = await SELF.fetch(`https://x.local/api/jobs/${id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ target: "video", instructions: "mas rapido" }),
    });
    expect(res.status).toBe(201);
    const { id: childId } = await res.json();
    const child = await env.DB.prepare("SELECT features, anim_count FROM jobs WHERE id=?").bind(childId).first();
    expect(JSON.parse(child.features)).toEqual({ captions: false, camera: false, animations: false });
    expect(child.anim_count).toBe("max");
    // Parent row is left untouched.
    const parent = await env.DB.prepare("SELECT status FROM jobs WHERE id=?").bind(id).first();
    expect(parent.status).toBe("done");
  });

  it("getSettings defaults features all-true and anim_count 'default'", async () => {
    const cookie = await login();
    const res = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(res.features).toEqual({ captions: true, camera: true, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(res.anim_count).toBe("default");
  });

  it("putSettings merges a partial features object to a valid full-key object", async () => {
    const cookie = await login();
    const put = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ features: { camera: false } }),
    });
    expect(put.status).toBe(200);
    const after = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(after.features).toEqual({ captions: true, camera: false, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
  });

  it("putSettings accepts a valid anim_count", async () => {
    const cookie = await login();
    const put = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_count: "few" }),
    });
    expect(put.status).toBe(200);
    const after = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(after.anim_count).toBe("few");
  });

  it("putSettings rejects an invalid anim_count with 400", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_count: "bogus" }),
    });
    expect(res.status).toBe(400);
  });

  it("putSettings rejects non-object features with 400", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ features: "nope" }),
    });
    expect(res.status).toBe(400);
  });

  it("putSettings leaves anim_quality untouched when only features is sent, and vice versa", async () => {
    const cookie = await login();
    await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_quality: "high" }),
    });
    await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ features: { music: false } }),
    });
    const after = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(after.anim_quality).toBe("high");
    expect(after.features).toEqual({ captions: true, camera: true, animations: true, music: false, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
  });

  it("listJobs includes parsed features and anim_count", async () => {
    const cookie = await login();
    await setFeatures({ captions: true, camera: false, animations: true });
    await setAnimCount("max");
    const { id } = await (await createJob(cookie, "list1.mp4")).json();
    const list = await (await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).json();
    const job = list.find((j) => j.id === id);
    expect(job.features).toEqual({ captions: true, camera: false, animations: true, music: true, sfx: true, carousel: true, quotes: true, videos_extra: true, video_carruseles: true, carruseles_extra: true, imagenes: true, posters: true, textos: true });
    expect(job.anim_count).toBe("max");
  });
});

describe("V6 format feature toggles", () => {
  const FULL_ON = {
    captions: true, camera: true, animations: true, music: true, sfx: true,
    carousel: true, quotes: true,
    videos_extra: true, video_carruseles: true, carruseles_extra: true,
    imagenes: true, posters: true, textos: true,
  };

  it("settings roundtrip includes the five new format keys", async () => {
    const cookie = await login();
    const put = await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ features: { videos_extra: false, textos: false } }),
    });
    expect(put.status).toBe(200);
    const after = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(after.features).toEqual({ ...FULL_ON, videos_extra: false, textos: false });
  });

  it("a legacy features row missing the format keys coerces them to true", async () => {
    const cookie = await login();
    // A pre-V6 settings row: only the seven legacy keys, two of them off.
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('features', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).bind(JSON.stringify({
      captions: false, camera: true, animations: true, music: false,
      sfx: true, carousel: true, quotes: true,
    })).run();
    const res = await (await SELF.fetch("https://x.local/api/settings", { headers: { Cookie: cookie } })).json();
    expect(res.features).toEqual({ ...FULL_ON, captions: false, music: false });
  });

  it("createJob snapshots the format keys onto the job row", async () => {
    const cookie = await login();
    await SELF.fetch("https://x.local/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ features: { imagenes: false } }),
    });
    const res = await createJob(cookie, "formats.mp4");
    const { id } = await res.json();
    const row = await env.DB.prepare("SELECT features FROM jobs WHERE id=?").bind(id).first();
    expect(JSON.parse(row.features)).toEqual({ ...FULL_ON, imagenes: false });
  });
});

describe("server restart", () => {
  const AGENT_AUTH = { Authorization: "Bearer test-agent-token" };

  async function getControl() {
    return (await SELF.fetch("https://x.local/api/agent/control/restart", { headers: AGENT_AUTH })).json();
  }

  it("user request sets restart_requested_at, then the agent sees restart_pending true", async () => {
    const cookie = await login();
    const before = await getControl();
    expect(before.restart_pending).toBe(false);
    const res = await SELF.fetch("https://x.local/api/server/restart", {
      method: "POST", headers: { Cookie: cookie },
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ ok: true });
    const after = await getControl();
    expect(after.restart_pending).toBe(true);
    expect(after.requested_at).toBeGreaterThan(0);
  });

  it("acking clears restart_pending", async () => {
    const cookie = await login();
    await SELF.fetch("https://x.local/api/server/restart", { method: "POST", headers: { Cookie: cookie } });
    expect((await getControl()).restart_pending).toBe(true);
    const ack = await SELF.fetch("https://x.local/api/agent/control/restart/ack", {
      method: "POST", headers: AGENT_AUTH,
    });
    expect(ack.status).toBe(200);
    expect(await ack.json()).toEqual({ ok: true });
    expect((await getControl()).restart_pending).toBe(false);
  });

  it("requires user auth for the restart request (no cookie -> 401)", async () => {
    const res = await SELF.fetch("https://x.local/api/server/restart", { method: "POST" });
    expect(res.status).toBe(401);
  });

  it("requires the agent bearer for the control endpoints", async () => {
    const control = await SELF.fetch("https://x.local/api/agent/control/restart");
    expect(control.status).toBe(401);
    const ack = await SELF.fetch("https://x.local/api/agent/control/restart/ack", { method: "POST" });
    expect(ack.status).toBe(401);
  });
});
