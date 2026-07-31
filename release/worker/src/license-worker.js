import {
  claimWelcomeEmail,
  findByEmail,
  getLicense,
  insertAudit,
  isExpired,
  issueLicenseRecord,
  licenseStats,
  linkSubscription,
  listLicenses,
  markWelcomeEmailSent,
  recordActivation,
  recordWebhookEvent,
  releaseWelcomeEmailClaim,
  removeActivation,
  setSubscriptionState,
  updateLicenseFields,
  upsertLicense,
  upsertSubscriptionLicense,
} from "./storage.js";
import { sendWelcomeEmail } from "./email.js";

/**
 * GrapeRoot Pro — License + Release + Dashboard Worker
 *
 * Runs on Cloudflare Workers. Provides:
 *   POST /v1/license/verify         — validate key, return customer + signed download URL
 *   GET  /v1/release/asset          — stream the private GitHub release asset (server-side auth)
 *   POST /v1/admin/issue            — create a new license (auth: Bearer ADMIN_TOKEN)
 *   POST /v1/admin/revoke           — mark a license revoked
 *   POST /v1/dashboard/login        — exchange license_key → session cookie (HttpOnly)
 *   POST /v1/dashboard/logout       — clear session
 *   GET  /v1/dashboard/license      — return current session's license info + activations
 *   POST /v1/dashboard/revoke-device — remove one activation entry (frees a seat)
 *
 * Bindings / secrets:
 *   KV   LICENSES            — per-license records
 *   SEC  GITHUB_TOKEN        — PAT with repo scope for kunal12203/graperoot-pro-releases
 *   SEC  ADMIN_TOKEN         — bearer token for issuing/revoking licenses
 *   SEC  SESSION_SECRET      — HMAC-SHA256 key for signing dashboard session JWTs
 *   VAR  PRO_REPO / PRO_VERSION / PRO_ASSET
 *   VAR  DASHBOARD_ORIGIN    — allowed CORS origin (e.g. "https://graperoot.dev")
 *
 * Storage shape (KV value):
 *   {
 *     key, customer, email, tier, seats, expires,
 *     issued, revoked, activations: [{host,os,ts}, ...]
 *   }
 */

// ─────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────
const jsonResp = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });

function corsHeaders(req, env) {
  const origin = req.headers.get("Origin") || "";
  const allowed = env.DASHBOARD_ORIGIN || "https://graperoot.dev";
  const isAllowed = origin === allowed || origin === "http://localhost:3000";
  return {
    "Access-Control-Allow-Origin": isAllowed ? origin : allowed,
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

// Defense-in-depth CSRF guard on cookie-authenticated state-changing endpoints.
// Even with SameSite=Lax, this blocks text/plain form-like CSRF, SameSite-bug
// browsers, and any non-browser client that tries to replay the cookie.
function isOriginAllowed(req, env) {
  const origin = req.headers.get("Origin") || "";
  const allowed = env.DASHBOARD_ORIGIN || "https://graperoot.dev";
  return origin === allowed || origin === "http://localhost:3000";
}

// Constant-time comparison for token checks (defends theoretical timing attacks
// on Bearer ADMIN_TOKEN verification; network jitter dominates but hygiene matters).
function safeEquals(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ─────────────────────────────────────────────────────────────────────────
// Security headers — added to every response
// ─────────────────────────────────────────────────────────────────────────
const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
};

function withSecurityHeaders(resp) {
  const h = new Headers(resp.headers);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) h.set(k, v);
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: h });
}

function clientIp(req) {
  return req.headers.get("CF-Connecting-IP") ||
         req.headers.get("X-Forwarded-For")?.split(",")[0].trim() ||
         "unknown";
}

function normalizeActivationHost(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized || "unknown";
}

function activationSeatKey(value) {
  return normalizeActivationHost(value).toLowerCase();
}

// ─────────────────────────────────────────────────────────────────────────
// Rate limiting (KV-backed, per-IP, per-bucket, per-window)
// Uses fixed windows of `windowSec` seconds (good enough for our threat model).
// ─────────────────────────────────────────────────────────────────────────
async function rateLimit(req, env, bucket, limit, windowSec) {
  if (!env.LICENSES) return null;  // fail open if KV unavailable
  const ip = clientIp(req);
  const window = Math.floor(Date.now() / 1000 / windowSec);
  const k = `rate:${bucket}:${ip}:${window}`;
  const cur = parseInt((await env.LICENSES.get(k)) || "0", 10);
  if (cur >= limit) {
    const retryAfter = windowSec - (Math.floor(Date.now() / 1000) % windowSec);
    return new Response(
      JSON.stringify({ ok: false, error: "rate_limited", retry_after_s: retryAfter }),
      {
        status: 429,
        headers: {
          "Content-Type": "application/json",
          "Retry-After": String(retryAfter),
          "Cache-Control": "no-store",
        },
      }
    );
  }
  // Increment (best-effort; KV is eventually consistent which is fine here)
  await env.LICENSES.put(k, String(cur + 1), { expirationTtl: windowSec + 10 });
  return null;
}

// ─────────────────────────────────────────────────────────────────────────
// Audit log — records every admin + dashboard action for 90 days
// KV key: audit:<ISO-timestamp>-<random>
// ─────────────────────────────────────────────────────────────────────────
const AUDIT_TTL_S = 90 * 24 * 3600;

async function audit(env, req, action, details = {}) {
  const entry = {
    ts: new Date().toISOString(),
    action,
    ip: clientIp(req),
    ua: (req.headers.get("User-Agent") || "").slice(0, 200),
    ...details,
  };
  const k = `audit:${entry.ts}-${Math.random().toString(36).slice(2, 10)}`;
  try {
    if (env.AUDIT_LOG) {
      await env.AUDIT_LOG.put(k, JSON.stringify(entry), { expirationTtl: AUDIT_TTL_S });
    }
    await insertAudit(env, entry);
  } catch {}  // never let logging failure break an operation
}

// ─────────────────────────────────────────────────────────────────────────
// Session JWT (HMAC-SHA256 via Web Crypto — no deps)
// Payload: { key, iat, exp }   (exp = iat + 30d)
// ─────────────────────────────────────────────────────────────────────────
const SESSION_TTL_S = 30 * 24 * 3600;
const enc = new TextEncoder();
const dec = new TextDecoder();

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return Uint8Array.from(atob(s), c => c.charCodeAt(0));
}

async function signSession(payload, secret) {
  const header = { alg: "HS256", typ: "JWT" };
  const h = b64url(enc.encode(JSON.stringify(header)));
  const p = b64url(enc.encode(JSON.stringify(payload)));
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(`${h}.${p}`));
  return `${h}.${p}.${b64url(sig)}`;
}

async function verifySession(token, secret) {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [h, p, s] = parts;
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]
  );
  const ok = await crypto.subtle.verify("HMAC", key, b64urlDecode(s), enc.encode(`${h}.${p}`));
  if (!ok) return null;
  try {
    const payload = JSON.parse(dec.decode(b64urlDecode(p)));
    if (payload.exp && payload.exp < Math.floor(Date.now() / 1000)) return null;
    return payload;
  } catch {
    return null;
  }
}

function parseCookie(req, name) {
  const raw = req.headers.get("Cookie") || "";
  const match = raw.match(new RegExp(`(^|;\\s*)${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

async function requireSession(req, env) {
  const token = parseCookie(req, "grp_session");
  if (!token) return null;
  if (!env.SESSION_SECRET) return null;
  const payload = await verifySession(token, env.SESSION_SECRET);
  if (!payload?.key) return null;
  const record = await getLicense(env, payload.key);
  if (!record || record.revoked) return null;
  return { payload, record };
}

async function verifyLicense(req, env) {
  const rl = await rateLimit(req, env, "verify", 120, 60);  // 120/min/IP
  if (rl) return rl;
  const body = await req.json().catch(() => ({}));
  const { license_key, host, os } = body;
  if (!license_key) return jsonResp({ valid: false, reason: "missing license_key" }, 400);
  const safeHost = String(host || "").trim();

  const record = await getLicense(env, license_key, { withActivations: true });
  if (!record) return jsonResp({ valid: false, reason: "unknown license key" }, 404);
  if (record.revoked) return jsonResp({ valid: false, reason: "license revoked — contact support" }, 403);

  if (isExpired(record)) {
    return jsonResp({ valid: false, reason: `license expired on ${record.expires}` }, 403);
  }

  // Seat enforcement — unique hostnames, bounded by `seats`
  if (record.seats && Array.isArray(record.activations)) {
    const uniqueHosts = new Set(record.activations.map(a => activationSeatKey(a.host)));
    if (safeHost && !uniqueHosts.has(activationSeatKey(safeHost)) && uniqueHosts.size >= record.seats) {
      return jsonResp({ valid: false, reason: `seat limit (${record.seats}) reached — contact support` }, 403);
    }
  }

  await recordActivation(env, record, safeHost || "unknown", os || "unknown");

  const download_url = `${new URL(req.url).origin}/v1/release/asset?k=${encodeURIComponent(license_key)}`;
  return jsonResp({
    valid: true,
    customer: record.customer,
    tier: record.tier,
    expires: record.expires,
    version: env.PRO_VERSION || "v1.0.20",
    download_url,
  });
}

async function streamAsset(req, env) {
  const rl = await rateLimit(req, env, "asset", 60, 60);  // 60/min/IP — generous for upgrades
  if (rl) return rl;
  const url = new URL(req.url);
  const key = url.searchParams.get("k");
  if (!key) return new Response("missing key", { status: 400 });

  // KV can be eventually-consistent across regions — retry briefly if the write from
  // the /verify call hasn't propagated yet.
  let record = null;
  for (let i = 0; i < 4; i++) {
    record = await getLicense(env, key);
    if (record) break;
    await new Promise(r => setTimeout(r, 200 * (i + 1)));   // 200, 400, 600, 800 ms
  }
  if (!record) return new Response(`license not found in KV after retries (key=${key})`, { status: 403 });
  if (record.revoked) return new Response("license revoked", { status: 403 });
  if (isExpired(record)) {
    return new Response("license expired", { status: 403 });
  }

  // Locate asset ID on GitHub
  const repo = env.PRO_REPO || "kunal12203/graperoot-pro-releases";
  const tag  = env.PRO_VERSION || "v1.0.20";
  const name = env.PRO_ASSET  || "graperoot-pro.tar.gz";
  const rel  = await fetch(`https://api.github.com/repos/${repo}/releases/tags/${tag}`, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "graperoot-pro-worker",
      Accept: "application/vnd.github+json",
    },
  });
  if (!rel.ok) {
    const body = await rel.text();
    return new Response(
      `release lookup failed: GitHub returned ${rel.status} ${rel.statusText}\n` +
      `repo=${repo} tag=${tag}\n` +
      `github body: ${body.slice(0, 400)}`,
      { status: 502, headers: { "Content-Type": "text/plain" } }
    );
  }
  const data = await rel.json();
  const asset = (data.assets || []).find(a => a.name === name);
  if (!asset) {
    const names = (data.assets || []).map(a => a.name).join(", ") || "(none)";
    return new Response(`asset not found: ${name}. Available: ${names}`, { status: 404 });
  }

  // Stream the asset with the PAT (GitHub redirects to a signed CDN URL)
  const r = await fetch(`https://api.github.com/repos/${repo}/releases/assets/${asset.id}`, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/octet-stream",
      "User-Agent": "graperoot-pro-worker",
    },
    redirect: "follow",
  });
  if (!r.ok) {
    const body = await r.text();
    return new Response(
      `asset fetch failed: ${r.status} ${r.statusText}\nbody: ${body.slice(0, 400)}`,
      { status: 502, headers: { "Content-Type": "text/plain" } }
    );
  }
  return new Response(r.body, {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename="${name}"`,
    },
  });
}

function randChunk() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // Crockford — no 0/O/1/I/L
  let out = "";
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  for (const b of bytes) out += alphabet[b % alphabet.length];
  return out;
}

function generateKey() { return `GRP-${randChunk()}-${randChunk()}-${randChunk()}`; }

async function requireAdmin(req, env) {
  const got = req.headers.get("Authorization") || "";
  const want = `Bearer ${env.ADMIN_TOKEN || ""}`;
  return safeEquals(got, want);
}

function dateOnlyString(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 10);
  return d.toISOString().slice(0, 10);
}

function bytesToHex(bytes) {
  return [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function hmacSha256Hex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return bytesToHex(sig);
}

async function verifyLemonSqueezySignature(req, env, raw) {
  if (!env.LEMONSQUEEZY_WEBHOOK_SECRET) return false;
  const got = req.headers.get("X-Signature") || "";
  const expected = await hmacSha256Hex(env.LEMONSQUEEZY_WEBHOOK_SECRET, raw);
  return safeEquals(got, expected);
}

async function issueLicense(req, env) {
  const rl = await rateLimit(req, env, "admin", 60, 60);
  if (rl) return rl;
  if (!(await requireAdmin(req, env))) {
    await audit(env, req, "admin_unauthorized", { endpoint: "issue" });
    return jsonResp({ error: "unauthorized" }, 401);
  }
  const body = await req.json();
  const { customer, email, tier = "pro", seats = 3, expires = "perpetual" } = body;
  if (!customer || !email) return jsonResp({ error: "customer and email required" }, 400);
  const key = body.key || generateKey();
  const existing = await getLicense(env, key);
  if (existing && !body.key) {
    return jsonResp({ error: "key collision, retry" }, 409);
  }
  const record = existing ? { ...existing, customer, email, tier, seats, expires } : {
    key, customer, email, tier, seats, expires,
    issued: new Date().toISOString().slice(0, 10),
    revoked: false, activations: [],
  };
  await issueLicenseRecord(env, record, { source: body.key ? "sync" : "admin_api" });
  await audit(env, req, "license_issued", { key, customer, email, tier, seats, expires, synced: !!body.key });
  return jsonResp({ ok: true, license: record });
}

async function revokeLicense(req, env) {
  const rl = await rateLimit(req, env, "admin", 60, 60);
  if (rl) return rl;
  if (!(await requireAdmin(req, env))) {
    await audit(env, req, "admin_unauthorized", { endpoint: "revoke" });
    return jsonResp({ error: "unauthorized" }, 401);
  }
  const { license_key } = await req.json();
  const record = await getLicense(env, license_key);
  if (!record) {
    await audit(env, req, "revoke_not_found", { key: license_key });
    return jsonResp({ error: "not found" }, 404);
  }
  record.revoked = true;
  await upsertLicense(env, record, { source: "admin_api" });
  await audit(env, req, "license_revoked", { key: license_key, customer: record.customer });
  return jsonResp({ ok: true, license: record });
}

async function handleLemonSqueezyWebhook(req, env) {
  const raw = await req.text();
  if (!(await verifyLemonSqueezySignature(req, env, raw))) {
    await audit(env, req, "lemonsqueezy_bad_signature");
    return jsonResp({ ok: false, error: "invalid signature" }, 401);
  }

  let body;
  try {
    body = JSON.parse(raw || "{}");
  } catch {
    return jsonResp({ ok: false, error: "invalid json" }, 400);
  }

  const meta = body.meta || {};
  const data = body.data || {};
  const attrs = data.attributes || {};
  const event = req.headers.get("X-Event-Name") || meta.event_name || "";
  // For payment events, data.id is the invoice ID; the real subscription ID is in attributes
  const isPaymentEvent = event.startsWith("subscription_payment_");
  const subId = String(
    isPaymentEvent ? (attrs.subscription_id || data.id || "") : (data.id || "")
  ).trim();
  const eventId = String(
    meta.webhook_id || meta.event_id || body.id ||
    `${event}:${data.id}:${attrs.updated_at || attrs.created_at || attrs.status || ""}`,
  );

  if (env.LEMONSQUEEZY_VARIANT_ID) {
    const variantId = String(attrs.variant_id || "");
    if (event === "subscription_created" && !variantId) {
      await audit(env, req, "lemonsqueezy_missing_variant", { event });
      return jsonResp({ ok: true, ignored: "missing_variant" });
    }
    if (variantId && variantId !== String(env.LEMONSQUEEZY_VARIANT_ID)) {
      await audit(env, req, "lemonsqueezy_wrong_variant", { event, variantId });
      return jsonResp({ ok: true, ignored: "wrong_variant" });
    }
  }

  if (!subId) return jsonResp({ ok: false, error: "missing subscription id" }, 400);

  // Record the webhook event for audit/idempotency. subscription_created is still
  // handled below because the subscription unique constraint is the real guard.
  const firstDelivery = await recordWebhookEvent(env, "lemonsqueezy", eventId, event, body);
  if (!firstDelivery && event !== "subscription_created") {
    return jsonResp({ ok: true, duplicate: true, event });
  }

  if (event === "subscription_created") {
    const email = String(attrs.user_email || "").trim().toLowerCase();
    const userName = String(attrs.user_name || "").trim();
    const firstName = userName.split(/\s+/)[0] || "";
    const trialEndsAt = attrs.trial_ends_at || "";
    const isTrial = !!trialEndsAt;
    if (!email) return jsonResp({ ok: false, error: "missing user_email" }, 400);

    const renewsAt = attrs.renews_at || "";
    const expires = isTrial
      ? dateOnlyString(trialEndsAt)
      : (renewsAt ? dateOnlyString(new Date(new Date(renewsAt).getTime() + 86400000)) : "perpetual");

    // Phase 2: reuse existing key for same email instead of issuing a new one
    const existing = await findByEmail(env, email);
    if (existing) {
      // Link new subscription to existing key, reactivate it
      await linkSubscription(env, existing.key, subId);
      const fields = { revoked: false, tier: "pro" };
      // Don't downgrade perpetual to date-based
      if (existing.expires !== "perpetual") fields.expires = expires;
      const record = await updateLicenseFields(env, existing.key, fields);
      await audit(env, req, "lemonsqueezy_subscription_reactivated", {
        key: existing.key, email, subId, trial: isTrial,
      });
      return jsonResp({ ok: true, key: existing.key, reactivated: true, is_trial: isTrial });
    }

    const key = generateKey();
    const record = await upsertSubscriptionLicense(env, {
      key,
      customer: userName || email.split("@")[0],
      email,
      tier: "pro",
      seats: 3,
      expires,
      issued: new Date().toISOString().slice(0, 10),
      revoked: false,
      activations: [],
    }, subId);

    const claimed = await claimWelcomeEmail(env, record.key);
    if (claimed) {
      try {
        await sendWelcomeEmail(env, {
          email,
          firstName,
          key: record.key,
          isTrial,
          trialEndsAt,
          seats: record.seats,
        });
        await markWelcomeEmailSent(env, record.key);
        await audit(env, req, "lemonsqueezy_welcome_email_sent", { key: record.key, email, subId });
      } catch (e) {
        await releaseWelcomeEmailClaim(env, record.key);
        await audit(env, req, "lemonsqueezy_welcome_email_failed", {
          key: record.key,
          email,
          subId,
          error: String(e?.message || e).slice(0, 300),
        });
      }
    }

    await audit(env, req, "lemonsqueezy_subscription_created", {
      key: record.key,
      email,
      subId,
      trial: isTrial,
      duplicate: record.key !== key,
    });
    return jsonResp({ ok: true, key: record.key, is_trial: isTrial, duplicate: record.key !== key });
  }

  if (event === "subscription_payment_success") {
    const renewsAt = attrs.renews_at || "";
    const expires = renewsAt ? dateOnlyString(new Date(new Date(renewsAt).getTime() + 86400000)) : null;
    const fields = { tier: "pro", revoked: false };
    if (expires) fields.expires = expires;
    const record = await setSubscriptionState(env, subId, fields);
    await audit(env, req, "lemonsqueezy_payment_success", { subId, key: record?.key || "", expires });
    return jsonResp({ ok: true, event: "payment_success", key: record?.key || "", expires });
  }

  if (event === "subscription_payment_failed") {
    await audit(env, req, "lemonsqueezy_payment_failed", { subId });
    return jsonResp({ ok: true, event: "payment_failed" });
  }

  if (event === "subscription_payment_refunded") {
    const record = await setSubscriptionState(env, subId, { revoked: true });
    await audit(env, req, "lemonsqueezy_payment_refunded", { subId, key: record?.key || "" });
    return jsonResp({ ok: true, event: "payment_refunded", key: record?.key || "" });
  }

  if (event === "subscription_expired") {
    const record = await setSubscriptionState(env, subId, { revoked: true });
    await audit(env, req, "lemonsqueezy_expired", { subId, key: record?.key || "" });
    return jsonResp({ ok: true, event: "expired", key: record?.key || "" });
  }

  if (event === "subscription_cancelled") {
    // User cancelled but already paid for current period — let them keep access until renews_at + 1 day
    const renewsAt = attrs.renews_at || attrs.ends_at || "";
    const fields = {};
    if (renewsAt) {
      fields.expires = dateOnlyString(new Date(new Date(renewsAt).getTime() + 86400000));
    } else {
      fields.revoked = true;
    }
    const record = await setSubscriptionState(env, subId, fields);
    await audit(env, req, "lemonsqueezy_cancelled", { subId, key: record?.key || "", expires: fields.expires || null });
    return jsonResp({ ok: true, event: "cancelled" });
  }

  if (event === "subscription_resumed") {
    const renewsAt = attrs.renews_at || "";
    const expires = renewsAt ? dateOnlyString(new Date(new Date(renewsAt).getTime() + 86400000)) : null;
    const fields = { revoked: false };
    if (expires) fields.expires = expires;
    const record = await setSubscriptionState(env, subId, fields);
    await audit(env, req, "lemonsqueezy_resumed", { subId, key: record?.key || "", expires });
    return jsonResp({ ok: true, event: "resumed" });
  }

  if (event === "subscription_updated") {
    const status = attrs.status || "";
    const renewsAt = attrs.renews_at || "";
    if (status === "active" || status === "on_trial") {
      const expires = renewsAt ? dateOnlyString(new Date(new Date(renewsAt).getTime() + 86400000)) : null;
      const fields = { revoked: false };
      if (expires) fields.expires = expires;
      const record = await setSubscriptionState(env, subId, fields);
      await audit(env, req, "lemonsqueezy_updated_active", { subId, status, key: record?.key || "", expires });
      return jsonResp({ ok: true, event: "updated", status, expires });
    }
    if (status === "past_due" || status === "unpaid") {
      await audit(env, req, "lemonsqueezy_updated_past_due", { subId, status });
      return jsonResp({ ok: true, event: "updated", status });
    }
    if (status === "expired" || status === "cancelled") {
      const record = await setSubscriptionState(env, subId, { revoked: true });
      await audit(env, req, "lemonsqueezy_updated_terminal", { subId, status, key: record?.key || "" });
      return jsonResp({ ok: true, event: "updated", status });
    }
  }

  await audit(env, req, "lemonsqueezy_ignored", { event, subId });
  return jsonResp({ ok: true, event, ignored: true });
}

// ─────────────────────────────────────────────────────────────────────────
// Dashboard (customer self-service)
// ─────────────────────────────────────────────────────────────────────────
async function dashboardLogin(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  const rl = await rateLimit(req, env, "dashboard-login", 20, 60);  // 20/min/IP
  if (rl) return rl;
  const body = await req.json().catch(() => ({}));
  const key = (body.license_key || "").trim().toUpperCase();
  if (!key || !/^GRP-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(key)) {
    await audit(env, req, "dashboard_login_bad_format");
    return jsonResp({ ok: false, error: "invalid key format" }, 400, cors);
  }
  const record = await getLicense(env, key);
  if (!record) {
    await audit(env, req, "dashboard_login_unknown", { key });
    return jsonResp({ ok: false, error: "unknown license key" }, 404, cors);
  }
  if (record.revoked) {
    await audit(env, req, "dashboard_login_revoked", { key });
    return jsonResp({ ok: false, error: "license revoked — contact support" }, 403, cors);
  }
  if (isExpired(record)) {
    await audit(env, req, "dashboard_login_expired", { key });
    return jsonResp({ ok: false, error: `license expired on ${record.expires}` }, 403, cors);
  }
  if (!env.SESSION_SECRET) {
    return jsonResp({ ok: false, error: "session signing not configured" }, 500, cors);
  }
  const now = Math.floor(Date.now() / 1000);
  const token = await signSession({ key, iat: now, exp: now + SESSION_TTL_S }, env.SESSION_SECRET);
  await audit(env, req, "dashboard_login_ok", { key, customer: record.customer });

  // SameSite=Lax blocks cross-site CSRF (attacker.com can't get cookie sent).
  // graperoot.dev ↔ api.graperoot.dev are same-site (both eTLD+1 = graperoot.dev)
  // so legitimate same-site XHR still carries the cookie.
  const cookie = [
    `grp_session=${token}`,
    "Path=/",
    "HttpOnly",
    "Secure",
    "SameSite=Lax",
    `Max-Age=${SESSION_TTL_S}`,
  ].join("; ");
  return jsonResp(
    { ok: true, customer: record.customer, tier: record.tier, expires: record.expires },
    200,
    { ...cors, "Set-Cookie": cookie }
  );
}

async function dashboardLogout(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  const cookie = "grp_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0";
  return jsonResp({ ok: true }, 200, { ...cors, "Set-Cookie": cookie });
}

async function dashboardLicense(req, env) {
  const cors = corsHeaders(req, env);
  const session = await requireSession(req, env);
  if (!session) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const r = await getLicense(env, session.record.key, { withActivations: true });
  if (!r) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  // Unique device count from activations
  const unique = {};
  for (const a of (r.activations || [])) {
    const k = activationSeatKey(a.host);
    if (!unique[k] || a.ts > unique[k].ts) unique[k] = a;
  }
  const devices = Object.values(unique).sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  return jsonResp({
    ok: true,
    license: {
      key: r.key,
      customer: r.customer,
      email: r.email,
      tier: r.tier,
      seats: r.seats,
      expires: r.expires,
      issued: r.issued,
      seats_used: devices.length,
      devices,
    },
  }, 200, cors);
}

// ─────────────────────────────────────────────────────────────────────────
// Admin web dashboard (graperoot.dev/admin)
// Auth: POST ADMIN_TOKEN to /v1/admin/web-login → grp_admin_session cookie.
// ─────────────────────────────────────────────────────────────────────────
const ADMIN_SESSION_TTL_S = 365 * 24 * 3600;

async function adminWebLogin(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  const body = await req.json().catch(() => ({}));
  const token = (body.admin_token || "").trim();
  if (!token) return jsonResp({ ok: false, error: "missing admin_token" }, 400, cors);
  if (!safeEquals(token, env.ADMIN_TOKEN || "")) {
    await audit(env, req, "admin_web_login_bad_token");
    return jsonResp({ ok: false, error: "invalid admin token" }, 401, cors);
  }
  if (!env.SESSION_SECRET) return jsonResp({ ok: false, error: "session signing not configured" }, 500, cors);
  const now = Math.floor(Date.now() / 1000);
  const jwt = await signSession({ admin: true, iat: now, exp: now + ADMIN_SESSION_TTL_S }, env.SESSION_SECRET);
  const cookie = `grp_admin_session=${jwt}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${ADMIN_SESSION_TTL_S}`;
  await audit(env, req, "admin_web_login_ok");
  return jsonResp({ ok: true }, 200, { ...cors, "Set-Cookie": cookie });
}

async function adminWebLogout(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  const cookie = "grp_admin_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0";
  return jsonResp({ ok: true }, 200, { ...cors, "Set-Cookie": cookie });
}

async function requireAdminWeb(req, env) {
  const token = parseCookie(req, "grp_admin_session");
  if (!token || !env.SESSION_SECRET) return false;
  const payload = await verifySession(token, env.SESSION_SECRET);
  return payload?.admin === true;
}

async function adminWebMe(req, env) {
  const cors = corsHeaders(req, env);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  // Re-issue a fresh JWT on every check so the session rolls forward
  const now = Math.floor(Date.now() / 1000);
  const jwt = await signSession({ admin: true, iat: now, exp: now + ADMIN_SESSION_TTL_S }, env.SESSION_SECRET);
  const cookie = `grp_admin_session=${jwt}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${ADMIN_SESSION_TTL_S}`;
  return jsonResp({ ok: true, admin: true }, 200, { ...cors, "Set-Cookie": cookie });
}

async function adminWebListLicenses(req, env) {
  const cors = corsHeaders(req, env);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const url = new URL(req.url);
  const limit  = Math.min(parseInt(url.searchParams.get("limit") || "100", 10), 1000);
  const records = await listLicenses(env, { limit });
  return jsonResp({
    ok: true,
    licenses: records.map(r => ({
      key: r.key, customer: r.customer, email: r.email, tier: r.tier,
      seats: r.seats, seats_used: r.seats_used ?? new Set((r.activations || []).map(a => activationSeatKey(a.host))).size,
      expires: r.expires, issued: r.issued, revoked: !!r.revoked,
    })),
    cursor: null,
  }, 200, cors);
}

async function adminWebGetLicense(req, env) {
  const cors = corsHeaders(req, env);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const key = new URL(req.url).pathname.split("/").pop();
  const r = await getLicense(env, key, { withActivations: true });
  if (!r) return jsonResp({ ok: false, error: "not found" }, 404, cors);
  const unique = {};
  for (const a of (r.activations || [])) {
    const k = activationSeatKey(a.host);
    if (!unique[k] || a.ts > unique[k].ts) unique[k] = a;
  }
  return jsonResp({
    ok: true,
    license: {
      ...r,
      seats_used: Object.keys(unique).length,
      devices: Object.values(unique).sort((a, b) => (b.ts || "").localeCompare(a.ts || "")),
    },
  }, 200, cors);
}

async function adminWebUpdateLicense(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const parts = new URL(req.url).pathname.split("/");
  const key = parts[parts.length - 2];
  const body = await req.json().catch(() => ({}));
  const allowed = ["customer", "email", "tier", "seats", "expires", "revoked"];
  const changed = {};
  for (const f of allowed) {
    if (Object.prototype.hasOwnProperty.call(body, f)) changed[f] = body[f];
  }
  const r = await updateLicenseFields(env, key, changed);
  if (!r) return jsonResp({ ok: false, error: "not found" }, 404, cors);
  await audit(env, req, "admin_web_license_updated", { key, changed });
  return jsonResp({ ok: true, license: r }, 200, cors);
}

async function adminWebRevokeDevice(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const parts = new URL(req.url).pathname.split("/");
  const key = parts[parts.length - 2];
  const body = await req.json().catch(() => ({}));
  const host = body.host;
  if (!host) return jsonResp({ ok: false, error: "missing host" }, 400, cors);
  const existing = await getLicense(env, key, { withActivations: true });
  if (!existing) return jsonResp({ ok: false, error: "not found" }, 404, cors);
  const before = (existing.activations || []).length;
  const r = await removeActivation(env, key, host);
  if (!r) return jsonResp({ ok: false, error: "not found" }, 404, cors);
  await audit(env, req, "admin_web_device_revoked", { key, host });
  return jsonResp({ ok: true, removed: before - r.activations.length }, 200, cors);
}

async function adminWebStats(req, env) {
  const cors = corsHeaders(req, env);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  return jsonResp({
    ok: true,
    stats: await licenseStats(env),
  }, 200, cors);
}

async function adminWebAudit(req, env) {
  const cors = corsHeaders(req, env);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  if (!env.AUDIT_LOG) return jsonResp({ ok: true, entries: [] }, 200, cors);
  const url = new URL(req.url);
  const limit  = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 200);
  const filter = url.searchParams.get("action") || "";
  // Fetch only the most recent N keys to avoid scanning all 500 (was timing out the page)
  const list = await env.AUDIT_LOG.list({ prefix: "audit:", limit: Math.min(limit * 4, 200) });
  const sorted = [...list.keys].sort((a, b) => b.name.localeCompare(a.name)).slice(0, limit * 4);
  // Parallel KV gets — way faster than sequential
  const fetched = await Promise.all(sorted.map(async k => {
    try { return await env.AUDIT_LOG.get(k.name, { type: "json" }); } catch { return null; }
  }));
  const entries = fetched
    .filter(v => v && (!filter || v.action === filter))
    .slice(0, limit);
  return jsonResp({ ok: true, entries }, 200, cors);
}

async function adminWebIssue(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  if (!(await requireAdminWeb(req, env))) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const body = await req.json();
  const { customer, email, tier = "pro", seats = 3, expires = "perpetual" } = body;
  if (!customer || !email) return jsonResp({ ok: false, error: "customer and email required" }, 400, cors);
  const key = generateKey();
  const record = {
    key, customer, email, tier, seats, expires,
    issued: new Date().toISOString().slice(0, 10),
    revoked: false, activations: [],
  };
  await issueLicenseRecord(env, record, { source: "admin_web" });
  await audit(env, req, "admin_web_license_issued", { key, customer, email, tier, seats, expires });
  return jsonResp({ ok: true, license: record }, 200, cors);
}

async function dashboardRevokeDevice(req, env) {
  const cors = corsHeaders(req, env);
  if (!isOriginAllowed(req, env)) return jsonResp({ ok: false, error: "invalid origin" }, 403, cors);
  const session = await requireSession(req, env);
  if (!session) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const body = await req.json().catch(() => ({}));
  const host = body.host;
  if (!host) return jsonResp({ ok: false, error: "missing host" }, 400, cors);
  const current = await getLicense(env, session.record.key, { withActivations: true });
  if (!current) return jsonResp({ ok: false, error: "not authenticated" }, 401, cors);
  const before = (current.activations || []).length;
  const r = await removeActivation(env, current.key, host);
  await audit(env, req, "device_revoked", { key: current.key, host, removed: before - r.activations.length });
  return jsonResp({ ok: true, removed: before - r.activations.length, remaining_activations: r.activations.length }, 200, cors);
}

async function route(req, env) {
  const url = new URL(req.url);
  // CORS preflight for all /v1/dashboard/* and /v1/admin/web*  endpoints
  if (req.method === "OPTIONS" && (url.pathname.startsWith("/v1/dashboard/") || url.pathname.startsWith("/v1/admin/"))) {
    return new Response(null, { status: 204, headers: corsHeaders(req, env) });
  }
  try {
    // Health check — keeps internal Railway hostname out of shipped .py files
    if (url.pathname === "/ping") return new Response("ok", { status: 200 });

    // Public + bearer-admin
    if (url.pathname === "/v1/license/verify"         && req.method === "POST") return verifyLicense(req, env);
    if (url.pathname === "/v1/release/asset"          && req.method === "GET")  return streamAsset(req, env);
    if (url.pathname === "/v1/admin/issue"            && req.method === "POST") return issueLicense(req, env);
    if (url.pathname === "/v1/admin/revoke"           && req.method === "POST") return revokeLicense(req, env);
    if ((url.pathname === "/lemonsqueezy-webhook" || url.pathname === "/v1/lemonsqueezy/webhook") && req.method === "POST") {
      return handleLemonSqueezyWebhook(req, env);
    }

    // Customer dashboard
    if (url.pathname === "/v1/dashboard/login"        && req.method === "POST") return dashboardLogin(req, env);
    if (url.pathname === "/v1/dashboard/logout"       && req.method === "POST") return dashboardLogout(req, env);
    if (url.pathname === "/v1/dashboard/license"      && req.method === "GET")  return dashboardLicense(req, env);
    if (url.pathname === "/v1/dashboard/revoke-device" && req.method === "POST") return dashboardRevokeDevice(req, env);

    // Admin web dashboard (cookie-authed)
    if (url.pathname === "/v1/admin/web-login"        && req.method === "POST") return adminWebLogin(req, env);
    if (url.pathname === "/v1/admin/web-logout"       && req.method === "POST") return adminWebLogout(req, env);
    if (url.pathname === "/v1/admin/me"               && req.method === "GET")  return adminWebMe(req, env);
    if (url.pathname === "/v1/admin/licenses"         && req.method === "GET")  return adminWebListLicenses(req, env);
    if (url.pathname === "/v1/admin/stats"            && req.method === "GET")  return adminWebStats(req, env);
    if (url.pathname === "/v1/admin/audit"            && req.method === "GET")  return adminWebAudit(req, env);
    if (url.pathname === "/v1/admin/web-issue"        && req.method === "POST") return adminWebIssue(req, env);
    // /v1/admin/license/<KEY>            GET     detail
    // /v1/admin/license/<KEY>/update     POST    patch fields
    // /v1/admin/license/<KEY>/revoke-device  POST  free a seat
    {
      const m = url.pathname.match(/^\/v1\/admin\/license\/([A-Z0-9-]+)(?:\/(update|revoke-device))?$/);
      if (m) {
        if (req.method === "GET" && !m[2])                      return adminWebGetLicense(req, env);
        if (req.method === "POST" && m[2] === "update")         return adminWebUpdateLicense(req, env);
        if (req.method === "POST" && m[2] === "revoke-device")  return adminWebRevokeDevice(req, env);
      }
    }
    if (url.pathname === "/" || url.pathname === "/health") {
      return jsonResp({ ok: true, service: "graperoot-pro-license", ts: new Date().toISOString() });
    }
    return new Response("not found", { status: 404 });
  } catch (e) {
    return jsonResp({ error: String(e.message || e) }, 500);
  }
}

export default {
  async fetch(req, env) {
    const resp = await route(req, env);
    return withSecurityHeaders(resp);
  },
};
