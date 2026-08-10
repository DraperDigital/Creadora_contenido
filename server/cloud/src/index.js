export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

import { handleLogin, handleLogout, logoutAll, getSession, requireAgent } from "./auth.js";
import { createJob, listJobs, finalizeJob, jobResult, retryJob, requestChanges, jobFile, cancelJob, deleteJob, deleteAll, getSettings, putSettings, debugJob, requestRestart, getRestartControl, ackRestart } from "./jobs.js";
import { claim, activeJobs, agentStatus, complete, uploadUrl, agentMetrics, listReclaims, ackReclaims } from "./agent.js";
import { zipJob } from "./zip.js";
import { runCron } from "./cron.js";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const p = url.pathname;
    if (p === "/api/health") return json({ ok: true });
    if (p === "/api/login" && request.method === "POST") return handleLogin(request, env);

    if (p.startsWith("/api/agent/")) {
      const denied = requireAgent(request, env);
      if (denied) return denied;
      if (p === "/api/agent/claim" && request.method === "POST") return claim(env);
      if (p === "/api/agent/jobs" && request.method === "GET") return activeJobs(env);
      if (p === "/api/agent/metrics" && request.method === "POST") return agentMetrics(request, env);
      if (p === "/api/agent/reclaims" && request.method === "GET") return listReclaims(env);
      if (p === "/api/agent/reclaims/ack" && request.method === "POST") return ackReclaims(request, env);
      if (p === "/api/agent/control/restart" && request.method === "GET") return getRestartControl(env);
      if (p === "/api/agent/control/restart/ack" && request.method === "POST") return ackRestart(env);
      const m = p.match(/^\/api\/agent\/jobs\/([0-9a-f]+)\/(status|complete|upload-url)$/);
      if (m) {
        const [, id, action] = m;
        if (action === "status" && request.method === "POST") return agentStatus(request, env, id, ctx);
        if (action === "complete" && request.method === "POST") return complete(request, env, id, ctx);
        if (action === "upload-url" && request.method === "POST") return uploadUrl(request, env, id);
      }
      return json({ error: "not found" }, 404);
    }

    if (p.startsWith("/api/")) {
      const session = await getSession(request, env);
      if (!session) return json({ error: "unauthorized" }, 401);
      const { admin } = session;
      const adminOnly = () => json({ error: "forbidden" }, 403);
      if (p === "/api/logout" && request.method === "POST") return handleLogout(env);
      if (p === "/api/admin/logout-all" && request.method === "POST") {
        return admin ? logoutAll(env) : adminOnly();
      }
      if (p === "/api/settings" && request.method === "GET") return getSettings(env, admin);
      if (p === "/api/settings" && request.method === "PUT") {
        return admin ? putSettings(request, env) : adminOnly();
      }
      if (p === "/api/jobs" && request.method === "POST") return createJob(request, env);
      if (p === "/api/jobs" && request.method === "GET") return listJobs(env, admin);
      if (p === "/api/server/restart" && request.method === "POST") {
        return admin ? requestRestart(env) : adminOnly();
      }
      if (p === "/api/jobs/delete-all" && request.method === "POST") {
        return admin ? deleteAll(env) : adminOnly();
      }
      const f = p.match(/^\/api\/jobs\/([0-9a-f]+)\/files\/([A-Za-z0-9._-]{1,80})$/);
      if (f && request.method === "GET") return jobFile(env, f[1], f[2]);
      // On-demand pack: zip a group (category / format key / "all") from R2.
      const z = p.match(/^\/api\/jobs\/([0-9a-f]+)\/zip\/([A-Za-z0-9._-]{1,64})$/);
      if (z && request.method === "GET") return zipJob(env, z[1], z[2]);
      const m = p.match(/^\/api\/jobs\/([0-9a-f]+)\/(finalize|result|retry|changes|cancel|delete)$/);
      if (m) {
        const [, id, action] = m;
        if (action === "finalize" && request.method === "POST") return finalizeJob(env, id);
        if (action === "result" && request.method === "GET") return jobResult(env, id);
        if (action === "retry" && request.method === "POST") return retryJob(env, id);
        if (action === "changes" && request.method === "POST") return requestChanges(request, env, id);
        if (action === "cancel" && request.method === "POST") return cancelJob(env, id);
        if (action === "delete" && request.method === "POST") return deleteJob(env, id);
      }
      const d = p.match(/^\/api\/debug\/jobs\/([0-9a-f]+)$/);
      // Debug dumps the full row (tokens included): operator-only.
      if (d && request.method === "GET") return admin ? debugJob(env, d[1]) : adminOnly();
      return json({ error: "not found" }, 404);
    }
    return json({ error: "not found" }, 404);
  },

  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runCron(env));
  },
};
