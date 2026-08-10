// Fire-and-forget webhook notifications (interface: env NOTIFY_WEBHOOK_URL).
// On a job transition to done/failed/rejected we POST a small JSON payload
// {"title","message","jobId","status"}. Never blocks or fails the request
// path: errors are swallowed and the fetch runs via ctx.waitUntil when
// available. Silent no-op when NOTIFY_WEBHOOK_URL is unset.

const TITLES = {
  done: "Video listo",
  failed: "Video con error",
  rejected: "Cambio no aplicado",
};

function buildMessage(status, filename, error) {
  const name = filename ? `"${filename}"` : "el video";
  if (status === "done") return `El video ${name} está listo para descargar.`;
  if (status === "failed") {
    return error ? `El video ${name} falló: ${error}` : `El video ${name} falló.`;
  }
  if (status === "rejected") {
    return error
      ? `No se pudo aplicar el cambio en ${name}: ${error}`
      : `No se pudo aplicar el cambio en ${name}.`;
  }
  return `El trabajo ${name} cambió de estado.`;
}

export function notifyJobTransition(env, ctx, jobId, status, extra = {}) {
  if (!env.NOTIFY_WEBHOOK_URL) return;
  const task = (async () => {
    let filename = extra.filename || null;
    if (!filename) {
      try {
        const row = await env.DB.prepare("SELECT filename FROM jobs WHERE id=?").bind(jobId).first();
        filename = row ? row.filename : null;
      } catch {
        filename = null;
      }
    }
    const payload = {
      title: TITLES[status] || "Trabajo actualizado",
      message: buildMessage(status, filename, extra.error ? String(extra.error).slice(0, 300) : null),
      jobId,
      status,
    };
    await fetch(env.NOTIFY_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  })().catch((e) => {
    console.log("notify webhook failed:", e);
  });
  if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(task);
}
