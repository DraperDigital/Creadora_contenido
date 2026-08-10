import { SELF, env } from "cloudflare:test";
import { describe, it, expect } from "vitest";
import { login } from "./helpers.js";

describe("auth", () => {
  it("rejects a wrong password", async () => {
    const res = await SELF.fetch("https://x.local/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: "nope" }),
    });
    expect(res.status).toBe(401);
  });

  it("accepts the right password and sets a session cookie", async () => {
    const cookie = await login();
    expect(cookie).toMatch(/^bionico_session=\d+\./);
  });

  it("blocks user routes without a cookie", async () => {
    const res = await SELF.fetch("https://x.local/api/jobs");
    expect(res.status).toBe(401);
  });

  it("allows user routes with the cookie", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/jobs", { headers: { Cookie: cookie } });
    expect(res.status).toBe(200);
  });

  it("rejects a tampered cookie", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/jobs", {
      headers: { Cookie: cookie.slice(0, -2) + "xx" },
    });
    expect(res.status).toBe(401);
  });

  it("blocks agent routes without the bearer token", async () => {
    const res = await SELF.fetch("https://x.local/api/agent/claim", { method: "POST" });
    expect(res.status).toBe(401);
  });

  it("logout clears the session cookie with Max-Age=0", async () => {
    const cookie = await login();
    const res = await SELF.fetch("https://x.local/api/logout", {
      method: "POST",
      headers: { Cookie: cookie },
    });
    expect(res.status).toBe(200);
    const setCookie = res.headers.get("Set-Cookie");
    expect(setCookie).toContain("bionico_session=;");
    expect(setCookie).toContain("Max-Age=0");
  });

  it("blocks logout without a cookie", async () => {
    const res = await SELF.fetch("https://x.local/api/logout", { method: "POST" });
    expect(res.status).toBe(401);
  });
});

describe("login rate limiting", () => {
  const IP = "203.0.113.9";

  async function attempt(password, ip = IP) {
    return SELF.fetch("https://x.local/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
      body: JSON.stringify({ password }),
    });
  }

  it("locks an IP out after 5 failed attempts, even with the right password", async () => {
    for (let i = 0; i < 5; i++) {
      expect((await attempt("wrong")).status).toBe(401);
    }
    expect((await attempt("wrong")).status).toBe(429);
    expect((await attempt("test-password")).status).toBe(429);
    // A different IP is unaffected.
    expect((await attempt("test-password", "198.51.100.7")).status).toBe(200);
  });

  it("a successful login clears the failure counter", async () => {
    for (let i = 0; i < 4; i++) await attempt("wrong");
    expect((await attempt("test-password")).status).toBe(200);
    // Counter was reset: 4 more failures are allowed before a lockout.
    for (let i = 0; i < 4; i++) {
      expect((await attempt("wrong")).status).toBe(401);
    }
    expect((await attempt("test-password")).status).toBe(200);
  });

  it("the lockout expires after the 15-minute window", async () => {
    for (let i = 0; i < 5; i++) await attempt("wrong");
    expect((await attempt("test-password")).status).toBe(429);
    await env.DB.prepare("UPDATE login_attempts SET last_fail_at=? WHERE ip=?")
      .bind(Date.now() - 16 * 60 * 1000, IP).run();
    expect((await attempt("test-password")).status).toBe(200);
  });
});
