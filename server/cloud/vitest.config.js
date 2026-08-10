import path from "node:path";
import { createHash } from "node:crypto";
import { defineWorkersConfig, readD1Migrations } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, "migrations"));
  const passwordHash = createHash("sha256").update("test-password").digest("hex");
  return {
    test: {
      setupFiles: ["./test/apply-migrations.js"],
      poolOptions: {
        workers: {
          wrangler: { configPath: "./wrangler.toml" },
          miniflare: {
            bindings: {
              TEST_MIGRATIONS: migrations,
              OWNER_PASSWORD_HASH: passwordHash,
              SESSION_SECRET: "test-session-secret",
              AGENT_TOKEN: "test-agent-token",
              R2_ACCESS_KEY_ID: "testkey",
              R2_SECRET_ACCESS_KEY: "testsecret",
            },
          },
        },
      },
    },
  };
});
