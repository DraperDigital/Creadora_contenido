// R2-compatible shim over the filesystem. Only the surface the Worker uses:
// head / get / delete / list (uploads arrive via the adapter's /media PUT).
import { createReadStream } from "node:fs";
import { mkdir, readdir, readFile, rm, stat } from "node:fs/promises";
import { join } from "node:path";
import { Readable } from "node:stream";

const KEY_RE = /^(inbox|outbox)\/[0-9a-f]+\/[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
export const validKey = (k) => typeof k === "string" && KEY_RE.test(k);

export function makeMedia(rootDir) {
  const pathOf = (key) => {
    if (!validKey(key)) throw new Error(`bad media key: ${key}`);
    return join(rootDir, ...key.split("/"));
  };
  return {
    pathOf,
    async head(key) {
      if (!validKey(key)) return null;
      try { const s = await stat(pathOf(key)); return s.isFile() ? { key, size: s.size } : null; }
      catch { return null; }
    },
    async get(key) {
      if (!validKey(key)) return null; // R2 parity: invalid key behaves like an absent object
      const p = pathOf(key);
      let s;
      try { s = await stat(p); } catch { return null; }
      if (!s.isFile()) return null;
      let cachedBody = null;
      return {
        key, size: s.size,
        // Lazy + cached: zip.js truth-checks obj.body and then reads it — one stream, no fd leak.
        get body() { return (cachedBody ??= Readable.toWeb(createReadStream(p))); },
        async text() { return readFile(p, "utf8"); },
        async arrayBuffer() { const b = await readFile(p); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); },
      };
    },
    async delete(key) {
      if (!validKey(key)) return; // nothing to delete under an invalid key
      await rm(pathOf(key), { force: true });
    },
    async list({ prefix = "" } = {}) {
      // Worker only ever lists "outbox/<id>/" (and inbox equivalents): one flat dir.
      const m = /^(inbox|outbox)\/([0-9a-f]+)\/$/.exec(prefix);
      const objects = [];
      if (m) {
        let names = [];
        try { names = await readdir(join(rootDir, m[1], m[2])); } catch { /* absent dir = empty listing */ }
        for (const n of names) objects.push({ key: `${m[1]}/${m[2]}/${n}` });
      }
      return { objects, truncated: false, cursor: undefined };
    },
    async ensureRoot() { await mkdir(rootDir, { recursive: true }); },
  };
}
