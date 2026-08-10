import { env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { presignUrl } from "../src/presign.js";

describe("presign", () => {
  it("produces a signed PUT url with expiry", async () => {
    const url = await presignUrl(env, "PUT", "inbox/abc/video.mp4", 86400);
    const u = new URL(url);
    expect(u.hostname).toBe(`${env.ACCOUNT_ID}.r2.cloudflarestorage.com`);
    expect(u.pathname).toBe("/bionico-media/inbox/abc/video.mp4");
    expect(u.searchParams.get("X-Amz-Expires")).toBe("86400");
    expect(u.searchParams.get("X-Amz-Signature")).toBeTruthy();
  });

  it("signs GET and PUT differently", async () => {
    const g = await presignUrl(env, "GET", "k", 3600);
    const p = await presignUrl(env, "PUT", "k", 3600);
    expect(new URL(g).searchParams.get("X-Amz-Signature"))
      .not.toBe(new URL(p).searchParams.get("X-Amz-Signature"));
  });
});
