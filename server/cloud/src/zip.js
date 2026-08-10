// Server-side, on-demand ZIP of a job's deliverables — built by streaming the
// individual R2 objects into a STORE zip (no compression: mp4/png are already
// compressed). Nothing is ever uploaded twice: the engine uploads only the
// individual files, and a pack is materialized here only when someone downloads
// it. `group` is a manifest CATEGORY ("carruseles"), a FORMAT key
// ("carrusel_fotos"), or "all" (every deliverable).
import { Zip, ZipPassThrough } from "fflate";

const GROUP_RE = /^[A-Za-z0-9._-]{1,64}$/;

async function jobOutputs(env, id) {
  const row = await env.DB.prepare("SELECT outputs FROM jobs WHERE id=?").bind(id).first();
  if (!row || !row.outputs) return null;
  try {
    const outs = JSON.parse(row.outputs);
    return Array.isArray(outs) ? outs : null;
  } catch {
    return null;
  }
}

async function loadManifest(env, outputs) {
  const entry = outputs.find((o) => o && o.name === "formats_manifest.json");
  if (!entry) return null;
  const obj = await env.MEDIA.get(entry.key);
  if (!obj) return null;
  try {
    const m = JSON.parse(await obj.text());
    return m && m.categories ? m : null;
  } catch {
    return null;
  }
}

const isDeliverable = (o) =>
  o && o.name && o.key && o.kind !== "meta" && o.name !== "formats_manifest.json";

// Resolve `group` -> ordered, de-duped [{name, key}] to include in the zip.
async function resolveFiles(env, id, outputs, group) {
  const keyByName = new Map(outputs.filter((o) => o && o.name).map((o) => [o.name, o.key]));
  if (group === "all") {
    return outputs.filter(isDeliverable).map((o) => ({ name: o.name, key: o.key }));
  }
  const manifest = await loadManifest(env, outputs);
  if (!manifest) return null;
  const names = [];
  const cats = manifest.categories;
  if (Array.isArray(cats[group])) {
    for (const e of cats[group]) if (e && e.file && keyByName.has(e.file)) names.push(e.file);
  } else {
    for (const list of Object.values(cats)) {
      for (const e of list || []) {
        if (e && e.format === group && e.file && keyByName.has(e.file)) names.push(e.file);
      }
    }
  }
  const seen = new Set();
  return names
    .filter((n) => (seen.has(n) ? false : seen.add(n)))
    .map((n) => ({ name: n, key: keyByName.get(n) }));
}

export async function zipJob(env, id, group) {
  if (!GROUP_RE.test(group)) return new Response("bad group", { status: 400 });
  const outputs = await jobOutputs(env, id);
  if (!outputs) return new Response("job not found", { status: 404 });
  const files = await resolveFiles(env, id, outputs, group);
  if (!files || !files.length) return new Response("no files for group", { status: 404 });

  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const zip = new Zip((err, chunk, final) => {
    if (err) {
      writer.abort(err).catch(() => {});
      return;
    }
    if (chunk && chunk.length) writer.write(chunk).catch(() => {});
    if (final) writer.close().catch(() => {});
  });

  // Pump each R2 object through the zip sequentially, respecting backpressure so
  // a slow client never makes the Worker buffer a whole file in memory.
  (async () => {
    try {
      for (const f of files) {
        const obj = await env.MEDIA.get(f.key);
        if (!obj || !obj.body) continue;
        const entry = new ZipPassThrough(f.name);
        zip.add(entry);
        const reader = obj.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          entry.push(value, false);
          await writer.ready; // backpressure: wait for the client before the next chunk
        }
        entry.push(new Uint8Array(0), true); // finalize this entry
      }
      zip.end();
    } catch (err) {
      writer.abort(err).catch(() => {});
    }
  })();

  return new Response(readable, {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": `attachment; filename="${group}.zip"`,
      "Cache-Control": "no-store",
    },
  });
}
