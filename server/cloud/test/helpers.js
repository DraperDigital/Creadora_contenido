import { SELF } from "cloudflare:test";

export async function login() {
  const res = await SELF.fetch("https://x.local/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "test-password" }),
  });
  const cookie = res.headers.get("Set-Cookie").split(";")[0];
  return cookie; // "bionico_session=<value>"
}
