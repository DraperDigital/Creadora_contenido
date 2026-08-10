import { SELF, env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { unzipSync } from "fflate";
import { login } from "./helpers.js";

async function seedJob(id, outputs, files) {
  await env.DB.prepare(
    `INSERT INTO jobs (id, filename, format, status, input_key, outputs, created_at, updated_at)
     VALUES (?, 'v.mp4', 'mp4', 'done', 'inbox/x', ?, 0, 0)`
  ).bind(id, JSON.stringify(outputs)).run();
  for (const [key, body] of Object.entries(files)) await env.MEDIA.put(key, body);
}

function getZip(id, group, cookie) {
  return SELF.fetch(`https://x.local/api/jobs/${id}/zip/${group}`, { headers: { Cookie: cookie } });
}

describe("on-demand pack zip", () => {
  it("zips a category / format / all from R2, never the manifest", async () => {
    const cookie = await login();
    const id = "aa11bb22cc33";
    const outputs = [
      { name: "a.png", key: `outbox/${id}/a.png`, kind: "image" },
      { name: "b.png", key: `outbox/${id}/b.png`, kind: "image" },
      { name: "formats_manifest.json", key: `outbox/${id}/formats_manifest.json`, kind: "meta" },
    ];
    const manifest = { categories: { imagenes: [
      { file: "a.png", format: "cita_foto" }, { file: "b.png", format: "cita_foto" },
    ] } };
    await seedJob(id, outputs, {
      [`outbox/${id}/a.png`]: "AAA",
      [`outbox/${id}/b.png`]: "BBBB",
      [`outbox/${id}/formats_manifest.json`]: JSON.stringify(manifest),
    });

    for (const group of ["imagenes", "cita_foto", "all"]) {
      const res = await getZip(id, group, cookie);
      expect(res.status).toBe(200);
      expect(res.headers.get("Content-Type")).toBe("application/zip");
      const entries = unzipSync(new Uint8Array(await res.arrayBuffer()));
      expect(Object.keys(entries).sort()).toEqual(["a.png", "b.png"]);   // the two images
      expect(new TextDecoder().decode(entries["a.png"])).toBe("AAA");     // real bytes
      expect(new TextDecoder().decode(entries["b.png"])).toBe("BBBB");
      expect(entries["formats_manifest.json"]).toBeUndefined();           // never packed
    }
  });

  it("404s an unknown job", async () => {
    const cookie = await login();
    expect((await getZip("deadbeef0000", "all", cookie)).status).toBe(404);
  });
});
