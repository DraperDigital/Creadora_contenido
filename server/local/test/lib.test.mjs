import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openDb, migrate } from "../lib/d1.mjs";
import { makeMedia, validKey } from "../lib/r2fs.mjs";

const tmp = () => mkdtempSync(join(tmpdir(), "bionico-lib-"));

test("d1: bind/first/all/run with D1-shaped results", async () => {
  const db = openDb(join(tmp(), "t.db"));
  db.exec("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)");
  const ins = await db.prepare("INSERT INTO t (v) VALUES (?)").bind("a").run();
  assert.equal(ins.meta.changes, 1);
  assert.equal((await db.prepare("SELECT v FROM t WHERE id=?").bind(1).first()).v, "a");
  assert.equal(await db.prepare("SELECT v FROM t WHERE id=?").bind(99).first(), null);
  assert.deepEqual((await db.prepare("SELECT v FROM t ORDER BY id").all()).results, [{ v: "a" }]);
  const upd = await db.prepare("UPDATE t SET v='b' WHERE id=?").bind(99).run();
  assert.equal(upd.meta.changes, 0);
  // D1 tolerates JS booleans/undefined; node:sqlite does not — shim must coerce.
  await db.prepare("INSERT INTO t (id, v) VALUES (?, ?)").bind(2, undefined).run(); // undefined -> NULL
  assert.equal((await db.prepare("SELECT v FROM t WHERE id=2").first()).v, null);
  const boolWhere = await db.prepare("UPDATE t SET v='x' WHERE id=?").bind(true).run(); // true -> 1
  assert.equal(boolWhere.meta.changes, 1);
  assert.equal((await db.prepare("SELECT v FROM t WHERE id=1").first()).v, "x");
});

test("d1: migrations apply once, in order, idempotently", async () => {
  const dir = tmp();
  const mig = join(dir, "migrations");
  mkdirSync(mig);
  writeFileSync(join(mig, "0001_a.sql"), "CREATE TABLE a (x INTEGER);");
  writeFileSync(join(mig, "0002_b.sql"), "ALTER TABLE a ADD COLUMN y INTEGER;");
  const db = openDb(join(dir, "m.db"));
  migrate(db, mig);
  migrate(db, mig); // second run: no error, nothing reapplied
  await db.prepare("INSERT INTO a (x, y) VALUES (?, ?)").bind(1, 2).run();
  assert.equal((await db.prepare("SELECT COUNT(*) AS n FROM _migrations").first()).n, 2);
});

test("r2fs: head/get/text/delete/list + key validation", async () => {
  const root = tmp();
  const media = makeMedia(root);
  assert.equal(validKey("inbox/abc123/video.mp4"), true);
  assert.equal(validKey("../etc/passwd"), false);
  assert.equal(validKey("inbox/abc123/../../x"), false);
  assert.equal(await media.head("inbox/ab12/video.mp4"), null);
  assert.equal(await media.head("../etc/passwd"), null);
  assert.equal(await media.get("../etc/passwd"), null);
  await media.delete("../etc/passwd"); // invalid key: no-op, must not throw
  mkdirSync(join(root, "outbox", "ab12"), { recursive: true });
  writeFileSync(join(root, "outbox", "ab12", "caption.txt"), "hola");
  assert.equal((await media.head("outbox/ab12/caption.txt")).size, 4);
  const obj = await media.get("outbox/ab12/caption.txt");
  assert.equal(await obj.text(), "hola");
  const body = obj.body;                      // lazy stream, cached:
  assert.equal(obj.body, body);               // second access = same stream (no fd leak)
  const listing = await media.list({ prefix: "outbox/ab12/" });
  assert.deepEqual(listing.objects.map(o => o.key), ["outbox/ab12/caption.txt"]);
  assert.equal(listing.truncated, false);
  await media.delete("outbox/ab12/caption.txt");
  assert.equal(await media.get("outbox/ab12/caption.txt"), null);
});
