import { env, fetchMock, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { describe, it, expect, beforeAll, afterEach } from "vitest";
import worker from "../src/index.js";

// Outbound webhook notifications (NOTIFY_WEBHOOK_URL). The webhook is faked
// with fetchMock; the worker is dispatched directly so the env can carry the
// webhook URL (the shared vitest env does not set it).
const HOOK = "https://hooks.local";
const AGENT = { Authorization: "Bearer test-agent-token", "Content-Type": "application/json" };

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});
afterEach(() => fetchMock.assertNoPendingInterceptors());

async function dispatch(testEnv, path, opts = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://x.local${path}`, opts), testEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function insertProcessing(id) {
  const now = Date.now();
  await env.DB.prepare(
    `INSERT INTO jobs (id, filename, format, status, input_key, created_at, updated_at, heartbeat_at)
     VALUES (?, 'mi video.mp4', 'short', 'processing', ?, ?, ?, ?)`,
  ).bind(id, `inbox/${id}/video.mp4`, now, now, now).run();
}

function expectHook(check) {
  fetchMock.get(HOOK).intercept({
    method: "POST",
    path: "/notify",
    body: (raw) => {
      const body = JSON.parse(raw);
      return check(body);
    },
  }).reply(200, "ok");
}

describe("webhook notifications", () => {
  it("POSTs title/message/jobId/status when a job fails", async () => {
    const id = "f0f0f0f0f0f0";
    await insertProcessing(id);
    expectHook((b) => b.status === "failed" && b.jobId === id
      && typeof b.title === "string" && b.message.includes("mi video.mp4"));
    const res = await dispatch({ ...env, NOTIFY_WEBHOOK_URL: `${HOOK}/notify` }, `/api/agent/jobs/${id}/status`, {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ status: "failed", error: "boom" }),
    });
    expect(res.status).toBe(200);
  });

  it("POSTs on rejected and on done", async () => {
    const a = "f1f1f1f1f1f1";
    const b = "f2f2f2f2f2f2";
    await insertProcessing(a);
    await insertProcessing(b);
    const testEnv = { ...env, NOTIFY_WEBHOOK_URL: `${HOOK}/notify` };
    expectHook((body) => body.status === "rejected" && body.jobId === a);
    await dispatch(testEnv, `/api/agent/jobs/${a}/status`, {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ status: "rejected", error: "no aplicable" }),
    });
    expectHook((body) => body.status === "done" && body.jobId === b);
    await dispatch(testEnv, `/api/agent/jobs/${b}/complete`, {
      method: "POST", headers: AGENT, body: JSON.stringify({}),
    });
  });

  it("does not notify on non-terminal status updates", async () => {
    const id = "f3f3f3f3f3f3";
    await insertProcessing(id);
    // No interceptor registered: any outbound fetch would throw (net disabled)
    // and assertNoPendingInterceptors stays clean.
    const res = await dispatch({ ...env, NOTIFY_WEBHOOK_URL: `${HOOK}/notify` }, `/api/agent/jobs/${id}/status`, {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ status: "processing", stage: "render" }),
    });
    expect(res.status).toBe(200);
  });

  it("is a silent no-op when NOTIFY_WEBHOOK_URL is unset", async () => {
    const id = "f4f4f4f4f4f4";
    await insertProcessing(id);
    const res = await dispatch(env, `/api/agent/jobs/${id}/status`, {
      method: "POST", headers: AGENT,
      body: JSON.stringify({ status: "failed", error: "boom" }),
    });
    expect(res.status).toBe(200);
  });
});
