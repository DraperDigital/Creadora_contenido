// D1-compatible shim over node:sqlite (Node >= 22.5).
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const coerce = (v) => (v === undefined ? null : typeof v === "boolean" ? (v ? 1 : 0) : v);

class Statement {
  constructor(db, sql) { this.db = db; this.sql = sql; this.params = []; }
  bind(...params) { this.params = params.map(coerce); return this; }
  async first() {
    // node:sqlite returns rows as null-prototype objects; D1 returns plain objects.
    const row = this.db.prepare(this.sql).get(...this.params);
    return row ? { ...row } : null;
  }
  async all() {
    return { results: this.db.prepare(this.sql).all(...this.params).map((row) => ({ ...row })) };
  }
  async run() {
    const info = this.db.prepare(this.sql).run(...this.params);
    return { success: true, meta: { changes: Number(info.changes), last_row_id: Number(info.lastInsertRowid) } };
  }
}

export function openDb(dbPath) {
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  db.exec("PRAGMA journal_mode=WAL;");
  db.exec("PRAGMA busy_timeout=5000;");
  return { prepare: (sql) => new Statement(db, sql), exec: (sql) => db.exec(sql), _raw: db };
}

export function migrate(dbApi, migrationsDir) {
  dbApi.exec("CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at INTEGER)");
  const done = new Set(dbApi._raw.prepare("SELECT name FROM _migrations").all().map((r) => r.name));
  for (const f of readdirSync(migrationsDir).filter((n) => n.endsWith(".sql")).sort()) {
    if (done.has(f)) continue;
    dbApi.exec(readFileSync(join(migrationsDir, f), "utf8"));
    dbApi._raw.prepare("INSERT INTO _migrations (name, applied_at) VALUES (?, ?)").run(f, Date.now());
  }
}
