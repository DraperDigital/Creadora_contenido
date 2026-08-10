import { SELF, env, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import worker from "../src/index.js";
import { sha256hex } from "../src/auth.js";
import { login } from "./helpers.js";

// The vitest worker env has no OPERATOR_ADMIN_PASSWORD_HASH (legacy mode:
// the owner keeps admin powers). Admin-vs-customer tests dispatch straight to
// the worker's fetch handler with a customized env instead.
const ADMIN_PASSWORD = "admin-password";

async function withAdminEnv() {
  return { ...env, OPERATOR_ADMIN_PASSWORD_HASH: await sha256hex(ADMIN_PASSWORD) };
}

async function dispatch(testEnv, path, opts = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://x.local${path}`, opts), testEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function loginAs(testEnv, password) {
  return dispatch(testEnv, "/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}

function cookieOf(res) {
  return res.headers.get("Set-Cookie").split(";")[0];
}

describe("admin role", () => {
  it("operator password logs in with admin:true; owner password becomes a customer", async () => {
    const testEnv = await withAdminEnv();
    const adminRes = await loginAs(testEnv, ADMIN_PASSWORD);
    expect(adminRes.status).toBe(200);
    expect((await adminRes.json()).admin).toBe(true);
    const ownerRes = await loginAs(testEnv, "test-password");
    expect(ownerRes.status).toBe(200);
    expect((await ownerRes.json()).admin).toBe(false);
  });

  it("without OPERATOR_ADMIN_PASSWORD_HASH the owner keeps admin powers (legacy fallback)", async () => {
    const res = await loginAs(env, "test-password");
    expect((await res.json()).admin).toBe(true);
    const put = await dispatch(env, "/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookieOf(res) },
      body: JSON.stringify({ anim_quality: "high" }),
    });
    expect(put.status).toBe(200);
  });

  it("customers are blocked from settings writes, restart, delete-all, logout-all and debug", async () => {
    const testEnv = await withAdminEnv();
    const cookie = cookieOf(await loginAs(testEnv, "test-password"));
    const blocked = [
      ["/api/settings", "PUT", JSON.stringify({ anim_quality: "high" })],
      ["/api/server/restart", "POST", null],
      ["/api/jobs/delete-all", "POST", null],
      ["/api/admin/logout-all", "POST", null],
      ["/api/debug/jobs/deadbeefdead", "GET", null],
    ];
    for (const [path, method, body] of blocked) {
      const res = await dispatch(testEnv, path, {
        method,
        headers: { "Content-Type": "application/json", Cookie: cookie },
        ...(body ? { body } : {}),
      });
      expect(res.status, `${method} ${path}`).toBe(403);
    }
    // Normal customer routes still work.
    const jobs = await dispatch(testEnv, "/api/jobs", { headers: { Cookie: cookie } });
    expect(jobs.status).toBe(200);
    const settings = await dispatch(testEnv, "/api/settings", { headers: { Cookie: cookie } });
    expect(settings.status).toBe(200);
  });

  it("admin sessions pass the gated routes", async () => {
    const testEnv = await withAdminEnv();
    const cookie = cookieOf(await loginAs(testEnv, ADMIN_PASSWORD));
    const put = await dispatch(testEnv, "/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: JSON.stringify({ anim_quality: "high" }),
    });
    expect(put.status).toBe(200);
    const restart = await dispatch(testEnv, "/api/server/restart", {
      method: "POST", headers: { Cookie: cookie },
    });
    expect(restart.status).toBe(200);
  });

  it("hides tokens_total and usage_metrics from customers but not from admins", async () => {
    const testEnv = await withAdminEnv();
    const customer = cookieOf(await loginAs(testEnv, "test-password"));
    const admin = cookieOf(await loginAs(testEnv, ADMIN_PASSWORD));
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO jobs (id, filename, format, status, input_key, tokens_total, created_at, updated_at)
       VALUES ('adadadadadad', 'v.mp4', 'short', 'done', 'inbox/x/video.mp4', 999, ?, ?)`,
    ).bind(now, now).run();
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('usage_metrics', '{\"five_hour_pct\":10}')",
    ).run();

    const customerJobs = await (await dispatch(testEnv, "/api/jobs", { headers: { Cookie: customer } })).json();
    const customerJob = customerJobs.find((j) => j.id === "adadadadadad");
    expect(customerJob).toBeDefined();
    expect("tokens_total" in customerJob).toBe(false);
    const customerSettings = await (await dispatch(testEnv, "/api/settings", { headers: { Cookie: customer } })).json();
    expect("usage_metrics" in customerSettings).toBe(false);

    const adminJobs = await (await dispatch(testEnv, "/api/jobs", { headers: { Cookie: admin } })).json();
    expect(adminJobs.find((j) => j.id === "adadadadadad").tokens_total).toBe(999);
    const adminSettings = await (await dispatch(testEnv, "/api/settings", { headers: { Cookie: admin } })).json();
    expect(adminSettings.usage_metrics.five_hour_pct).toBe(10);
  });
});

describe("session invalidation", () => {
  it("bumping the server-side generation invalidates existing sessions", async () => {
    const cookie = await login(); // via SELF, default env
    expect((await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).status).toBe(200);
    await env.DB.prepare(
      "INSERT INTO settings (key, value) VALUES ('session_generation', '1') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    ).run();
    expect((await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } })).status).toBe(401);
  });

  it("rotating the password invalidates sessions minted under the old one", async () => {
    const cookie = cookieOf(await loginAs(env, "test-password"));
    expect((await dispatch(env, "/api/jobs", { headers: { Cookie: cookie } })).status).toBe(200);
    const rotated = { ...env, OWNER_PASSWORD_HASH: await sha256hex("brand-new-password") };
    expect((await dispatch(rotated, "/api/jobs", { headers: { Cookie: cookie } })).status).toBe(401);
  });

  it("admin logout-all kills every outstanding session", async () => {
    const testEnv = await withAdminEnv();
    const customer = cookieOf(await loginAs(testEnv, "test-password"));
    const admin = cookieOf(await loginAs(testEnv, ADMIN_PASSWORD));
    expect((await dispatch(testEnv, "/api/jobs", { headers: { Cookie: customer } })).status).toBe(200);
    const res = await dispatch(testEnv, "/api/admin/logout-all", {
      method: "POST", headers: { Cookie: admin },
    });
    expect(res.status).toBe(200);
    expect((await res.json()).generation).toBe(1);
    expect((await dispatch(testEnv, "/api/jobs", { headers: { Cookie: customer } })).status).toBe(401);
    expect((await dispatch(testEnv, "/api/jobs", { headers: { Cookie: admin } })).status).toBe(401);
  });
});
