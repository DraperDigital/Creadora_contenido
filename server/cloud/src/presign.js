import { AwsClient } from "aws4fetch";

// Private HMAC helper (duplicated from auth.js on purpose: importing auth.js
// here would lengthen the existing index<->auth module cycle).
async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function presignUrl(env, method, key, expiresSeconds = 86400, responseParams = null) {
  // Local mode (server/local): HMAC-signed URL served by the adapter's /media
  // routes — same contract (opaque URL, honored response-content-* overrides).
  if (env.LOCAL_MODE) {
    const exp = Date.now() + expiresSeconds * 1000;
    const extras = [];
    if (responseParams) {
      for (const [k, v] of Object.entries(responseParams)) {
        if (v != null) extras.push([k, String(v)]);
      }
    }
    // The response-* overrides are folded into the signature (SigV4 parity):
    // tampering the served Content-Type/Disposition invalidates the URL.
    const extra = extras.map(([k, v]) => `${k}=${v}`).sort().join("&");
    const url = new URL(`/media/${key}`, env.PUBLIC_BASE_URL);
    url.searchParams.set("exp", String(exp));
    url.searchParams.set("sig", await hmacHex(env.SESSION_SECRET, `${method}|${key}|${exp}|${extra}`));
    for (const [k, v] of extras) url.searchParams.set(k, v);
    return url.toString();
  }
  const client = new AwsClient({
    accessKeyId: env.R2_ACCESS_KEY_ID,
    secretAccessKey: env.R2_SECRET_ACCESS_KEY,
  });
  const url = new URL(
    `https://${env.ACCOUNT_ID}.r2.cloudflarestorage.com/${env.BUCKET_NAME}/${key}`,
  );
  url.searchParams.set("X-Amz-Expires", String(expiresSeconds));
  // Optional S3 response-header overrides (e.g. response-content-disposition to
  // force a download with a nice filename, response-content-type). These are
  // signed as part of the query so R2 applies them to the GET response.
  if (responseParams) {
    for (const [k, v] of Object.entries(responseParams)) {
      if (v != null) url.searchParams.set(k, String(v));
    }
  }
  const signed = await client.sign(new Request(url, { method }), {
    aws: { signQuery: true, service: "s3", region: "auto" },
  });
  return signed.url;
}
