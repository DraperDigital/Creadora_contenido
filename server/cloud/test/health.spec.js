import { SELF } from "cloudflare:test";
import { describe, it, expect } from "vitest";

describe("health", () => {
  it("responds ok without auth", async () => {
    const res = await SELF.fetch("https://x.local/api/health");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
  });

  it("rejects unknown api paths with 401 when unauthenticated", async () => {
    const res = await SELF.fetch("https://x.local/api/nope");
    expect(res.status).toBe(401);
  });
});
