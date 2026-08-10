import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER = join(HERE, "..", "server.mjs");

function runBootstrap(dir) {
  const r = spawnSync(process.execPath, [SERVER, "--bootstrap-only"], {
    env: {
      ...process.env,
      BIONICO_LOCAL_DATA: join(dir, "data"),
      BIONICO_LOCAL_ENV: join(dir, "local.env"),
      BIONICO_CLOUD_ENV: join(dir, "cloud.env"),
      LOCAL_DASHBOARD_PORT: "18790",
    },
  });
  assert.equal(r.status, 0, String(r.stderr));
}

test("bootstrap respects an operator-written cloud config (no marker)", () => {
  const dir = mkdtempSync(join(tmpdir(), "bionico-boot-"));
  const operator = "DASHBOARD_BASE_URL=https://example.workers.dev\nAGENT_TOKEN=operator-token\n";
  writeFileSync(join(dir, "cloud.env"), operator);
  runBootstrap(dir);
  assert.equal(readFileSync(join(dir, "cloud.env"), "utf8"), operator);
});

test("bootstrap manages fresh files and its own marked block, idempotently", () => {
  const dir = mkdtempSync(join(tmpdir(), "bionico-boot-"));
  runBootstrap(dir);
  const first = readFileSync(join(dir, "cloud.env"), "utf8");
  assert.match(first, /managed-by: server\/local/);
  assert.match(first, /DASHBOARD_BASE_URL=http:\/\/127\.0\.0\.1:18790/);
  runBootstrap(dir);
  assert.equal(readFileSync(join(dir, "cloud.env"), "utf8"), first);
});
