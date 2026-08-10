import { json } from "./index.js";

const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const COOKIE = "bionico_session";
// Login rate limiting: after MAX_LOGIN_FAILS failed attempts from one IP the
// IP is locked out for LOCKOUT_MS. A successful login clears the counter.
const MAX_LOGIN_FAILS = 5;
const LOCKOUT_MS = 15 * 60 * 1000;

export async function sha256hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Server-side session generation counter (settings key). Bumping it (logout
// everywhere) invalidates every outstanding session at once; it is also mixed
// into the HMAC together with the password hash, so rotating a password
// invalidates the sessions that were minted under the old one.
export async function sessionGeneration(env) {
  try {
    const row = await env.DB.prepare(
      "SELECT value FROM settings WHERE key='session_generation'",
    ).first();
    return row && row.value != null ? String(row.value) : "0";
  } catch {
    return "0";
  }
}

export async function bumpSessionGeneration(env) {
  const current = Number(await sessionGeneration(env)) || 0;
  const next = current + 1;
  await env.DB.prepare(
    "INSERT INTO settings (key, value) VALUES ('session_generation', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
  ).bind(String(next)).run();
  return next;
}

// The password hash a session of the given role proves knowledge of. Until a
// dedicated operator password is configured, admin sessions fall back to the
// owner hash (legacy single-password deployments keep working).
function claimHash(env, admin) {
  if (admin && env.OPERATOR_ADMIN_PASSWORD_HASH) return env.OPERATOR_ADMIN_PASSWORD_HASH;
  return env.OWNER_PASSWORD_HASH || "";
}

async function sessionMac(env, expMillis, admin, gen) {
  return hmacHex(
    env.SESSION_SECRET,
    `${expMillis}|${admin ? 1 : 0}|${claimHash(env, admin)}|${gen}`,
  );
}

export async function signSession(env, admin, expMillis) {
  const gen = await sessionGeneration(env);
  return `${expMillis}.${admin ? 1 : 0}.${await sessionMac(env, expMillis, admin, gen)}`;
}

export async function verifySession(env, value, nowMillis) {
  const parts = String(value || "").split(".");
  if (parts.length !== 3) return null;
  const exp = Number(parts[0]);
  if (!Number.isFinite(exp) || exp < nowMillis) return null;
  if (parts[1] !== "0" && parts[1] !== "1") return null;
  const admin = parts[1] === "1";
  const gen = await sessionGeneration(env);
  const expected = await sessionMac(env, exp, admin, gen);
  if (!timingSafeEqual(parts[2], expected)) return null;
  return { admin };
}

function cookieValue(request) {
  const raw = request.headers.get("Cookie") || "";
  for (const part of raw.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === COOKIE) return rest.join("=");
  }
  return null;
}

// Returns { admin } for a valid session, null otherwise.
export async function getSession(request, env) {
  const value = cookieValue(request);
  if (!value) return null;
  return verifySession(env, value, Date.now());
}

export function requireAgent(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (timingSafeEqual(auth, `Bearer ${env.AGENT_TOKEN}`)) return null;
  return json({ error: "unauthorized" }, 401);
}

// Local mode serves plain http on the LAN; a Secure cookie would be dropped by
// the browser and login would silently fail. Cloudflare output stays identical.
const secureFlag = (env) => (env && env.LOCAL_MODE ? "" : " Secure;");

export function handleLogout(env) {
  return json({ ok: true }, 200, {
    "Set-Cookie": `${COOKIE}=; HttpOnly;${secureFlag(env)} SameSite=Lax; Path=/; Max-Age=0`,
  });
}

// Admin-only: invalidate every outstanding session (including the caller's).
export async function logoutAll(env) {
  const generation = await bumpSessionGeneration(env);
  return json({ ok: true, generation }, 200, {
    "Set-Cookie": `${COOKIE}=; HttpOnly;${secureFlag(env)} SameSite=Lax; Path=/; Max-Age=0`,
  });
}

function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export async function handleLogin(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const ip = clientIp(request);
  const now = Date.now();
  let attempts = null;
  try {
    attempts = await env.DB.prepare(
      "SELECT fail_count, last_fail_at FROM login_attempts WHERE ip=?",
    ).bind(ip).first();
  } catch (e) {
    console.log("login attempts read failed:", e);
  }
  if (attempts && attempts.fail_count >= MAX_LOGIN_FAILS
      && now - attempts.last_fail_at < LOCKOUT_MS) {
    return json({ error: "demasiados intentos fallidos; espera 15 minutos" }, 429);
  }
  const hash = await sha256hex(String(body.password || ""));
  const isAdmin = Boolean(env.OPERATOR_ADMIN_PASSWORD_HASH)
    && timingSafeEqual(hash, env.OPERATOR_ADMIN_PASSWORD_HASH);
  const isOwner = Boolean(env.OWNER_PASSWORD_HASH)
    && timingSafeEqual(hash, env.OWNER_PASSWORD_HASH);
  if (!isAdmin && !isOwner) {
    // Count the failure; a lockout window that already passed resets the count.
    const stale = attempts && now - attempts.last_fail_at >= LOCKOUT_MS;
    const count = attempts && !stale ? attempts.fail_count + 1 : 1;
    try {
      await env.DB.prepare(
        `INSERT INTO login_attempts (ip, fail_count, last_fail_at) VALUES (?, ?, ?)
         ON CONFLICT(ip) DO UPDATE SET fail_count=excluded.fail_count, last_fail_at=excluded.last_fail_at`,
      ).bind(ip, count, now).run();
    } catch (e) {
      console.log("login attempts write failed:", e);
    }
    return json({ error: "unauthorized" }, 401);
  }
  try {
    await env.DB.prepare("DELETE FROM login_attempts WHERE ip=?").bind(ip).run();
  } catch (e) {
    console.log("login attempts clear failed:", e);
  }
  // Until a separate operator password is configured, the owner password keeps
  // full admin powers so an existing deployment never locks its operator out.
  const admin = isAdmin || (isOwner && !env.OPERATOR_ADMIN_PASSWORD_HASH);
  const value = await signSession(env, admin, now + SESSION_TTL_MS);
  return json({ ok: true, admin }, 200, {
    "Set-Cookie": `${COOKIE}=${value}; Path=/; HttpOnly;${secureFlag(env)} SameSite=Lax; Max-Age=${SESSION_TTL_MS / 1000}`,
  });
}
