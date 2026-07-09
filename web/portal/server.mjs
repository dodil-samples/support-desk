/*
 * Customer portal server — static host + API proxy for the PUBLIC backend.
 *
 * The browser only ever calls its own origin (`POST /api`); this server forwards
 * to the public backend and injects the project key server-side. So the page
 * ships with zero configuration (no settings UI, no keys in localStorage) and
 * CORS never enters the picture. Also hosts the CRM inbound-email webhook.
 *
 * Sign-in (optional — mirrors the backend's AUTH_MODE):
 *   The session credential lives in an HttpOnly cookie set by THIS server, so
 *   it never exists in browser JS. Every /api call copies the cookie into
 *   `body.session` for the backend's identity seam (lib/identity.py).
 *   - email mode: the backend's verify_code returns a session token; we move
 *     it from the JSON into the cookie before the browser sees the response.
 *   - oidc mode: this server runs the standard authorization-code flow
 *     (GET /auth/login -> IdP -> GET /auth/callback) and stores the IdP's
 *     access token in the same cookie. Works with any OIDC issuer.
 *
 * Zero dependencies (Node http/fs only). Env:
 *   BACKEND_URL         the public backend's FQDN (required)
 *   PUBLIC_KEY          project key injected into every proxied call (optional)
 *   OIDC_ISSUER         OIDC mode: the issuer URL (its /.well-known is discovered)
 *   OIDC_CLIENT_ID      OIDC mode: this portal's client id at the IdP
 *   OIDC_CLIENT_SECRET  OIDC mode: client secret (omit for public/PKCE-less demo clients)
 *   PORT                listen port (default 8788; Ignite injects PORT)
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join, extname, normalize } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_URL = process.env.BACKEND_URL || "";
const PUBLIC_KEY = process.env.PUBLIC_KEY || "";
const OIDC_ISSUER = (process.env.OIDC_ISSUER || "").replace(/\/$/, "");
const OIDC_CLIENT_ID = process.env.OIDC_CLIENT_ID || "";
const OIDC_CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET || "";
const PORT = Number(process.env.PORT || 8788);
const COOKIE = "sd_session";
const WEEK = 7 * 24 * 3600;

if (!BACKEND_URL) {
  console.error("BACKEND_URL is required — the public backend's FQDN (see .env.example)");
  process.exit(1);
}

// The portal is the public surface: only the customer-safe actions pass, no
// matter what the browser asks for. Identity + signed-in actions included —
// the backend still enforces sign-in on my_tickets/reply_ticket itself.
const PUBLIC_ACTIONS = new Set([
  "submit_ticket", "ticket_status", "search_kb",
  "auth_config", "request_code", "verify_code", "whoami",
  "my_tickets", "reply_ticket",
]);

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon" };

function readBody(req) {
  return new Promise((resolve) => {
    let d = ""; req.on("data", (c) => (d += c));
    req.on("end", () => { try { resolve(JSON.parse(d || "{}")); } catch { resolve({}); } });
  });
}

function cookies(req) {
  return Object.fromEntries((req.headers.cookie || "").split(";").map((c) => {
    const i = c.indexOf("="); return i < 0 ? [c.trim(), ""] : [c.slice(0, i).trim(), c.slice(i + 1).trim()];
  }));
}

const setCookie = (v, maxAge = WEEK) =>
  `${COOKIE}=${v}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`;

async function forward(body) {
  if (PUBLIC_KEY) body.key = PUBLIC_KEY;
  const res = await fetch(BACKEND_URL, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }).catch((e) => { console.error("backend fetch failed:", e?.cause?.code || e?.cause?.message || e.message); return null; });
  if (!res) return { status: 502, json: { ok: false, error: "backend unreachable" } };
  const json = await res.json().catch(() => ({ ok: false, error: "non-JSON response" }));
  return { status: 200, json };
}

/** Map a range of inbound-email/webhook shapes to submit_ticket and forward it. */
function inboundToTicket(msg) {
  return {
    action: "submit_ticket",
    subject: msg.subject || msg.Subject || "(no subject)",
    body: msg.text || msg.body || msg["body-plain"] || msg.html || "",
    requester_email: msg.from || msg.From || msg.sender || msg.email || "",
    channel: "email",
  };
}

// ---------------------------------------------------------------- OIDC (code flow)
let oidcConf = null; // discovered once, cached for the process lifetime
async function oidc() {
  if (!OIDC_ISSUER) return null;
  if (!oidcConf) {
    oidcConf = await fetch(`${OIDC_ISSUER}/.well-known/openid-configuration`)
      .then((r) => r.json()).catch(() => null);
  }
  return oidcConf;
}

async function oidcLogin(req, res) {
  const conf = await oidc();
  if (!conf?.authorization_endpoint) { res.writeHead(500).end("OIDC issuer not reachable"); return; }
  const state = randomBytes(16).toString("hex");
  const redirect = `https://${req.headers.host}/auth/callback`;
  const u = new URL(conf.authorization_endpoint);
  u.search = new URLSearchParams({
    response_type: "code", client_id: OIDC_CLIENT_ID, redirect_uri: redirect,
    scope: "openid email profile", state,
  });
  res.writeHead(302, { location: u.toString(),
    "set-cookie": `sd_state=${state}; Path=/; HttpOnly; SameSite=Lax; Max-Age=600` });
  res.end();
}

async function oidcCallback(req, res, u) {
  const conf = await oidc();
  const code = u.searchParams.get("code");
  if (!conf?.token_endpoint || !code || u.searchParams.get("state") !== cookies(req).sd_state) {
    res.writeHead(400).end("bad OIDC callback (state/code)"); return;
  }
  const form = new URLSearchParams({
    grant_type: "authorization_code", code, client_id: OIDC_CLIENT_ID,
    redirect_uri: `https://${req.headers.host}/auth/callback`,
  });
  if (OIDC_CLIENT_SECRET) form.set("client_secret", OIDC_CLIENT_SECRET);
  const tok = await fetch(conf.token_endpoint, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: form,
  }).then((r) => r.json()).catch(() => null);
  if (!tok?.access_token) { res.writeHead(502).end("token exchange failed"); return; }
  res.writeHead(302, { location: "/",
    "set-cookie": [setCookie(tok.access_token, tok.expires_in || 3600),
                   "sd_state=; Path=/; Max-Age=0"] });
  res.end();
}

// --------------------------------------------------------------------------- static
async function serveStatic(res, urlPath) {
  const rel = normalize(urlPath === "/" ? "/index.html" : urlPath).replace(/^(\.\.[/\\])+/, "");
  const file = join(HERE, rel);
  if (!file.startsWith(HERE)) { res.writeHead(403).end("forbidden"); return; }
  try {
    const buf = await readFile(file);
    res.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream" });
    res.end(buf);
  } catch { res.writeHead(404).end("not found"); }
}

createServer(async (req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  if (u.pathname === "/healthz") { // Ignite BYOI readiness/liveness probe
    res.writeHead(200, { "content-type": "text/plain" });
    return res.end("ok");
  }
  if (u.pathname === "/auth/login" && OIDC_ISSUER) return oidcLogin(req, res);
  if (u.pathname === "/auth/callback" && OIDC_ISSUER) return oidcCallback(req, res, u);
  if (u.pathname === "/auth/logout" && req.method === "POST") {
    res.writeHead(200, { "content-type": "application/json", "set-cookie": setCookie("", 0) });
    return res.end(JSON.stringify({ ok: true }));
  }
  if (u.pathname === "/api" && req.method === "POST") {
    const body = await readBody(req);
    if (!PUBLIC_ACTIONS.has(body.action)) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end(JSON.stringify({ ok: false, error: `unknown action ${JSON.stringify(body.action)}` }));
    }
    const session = cookies(req)[COOKIE];
    if (session) body.session = session; // HttpOnly cookie -> backend identity seam
    const { status, json } = await forward(body);
    const headers = { "content-type": "application/json" };
    // email-mode sign-in: capture the minted session into the cookie and keep
    // the raw token out of the browser-visible JSON.
    if (body.action === "verify_code" && json?.ok && json?.result?.session) {
      headers["set-cookie"] = setCookie(json.result.session);
      delete json.result.session;
    }
    res.writeHead(status, headers);
    return res.end(JSON.stringify(json));
  }
  if (u.pathname === "/inbound" && req.method === "POST") {
    const { json } = await forward(inboundToTicket(await readBody(req)));
    res.writeHead(json.ok ? 200 : 502, { "content-type": "application/json" });
    return res.end(JSON.stringify(json));
  }
  return serveStatic(res, u.pathname);
}).listen(PORT, () => {
  console.log(`customer portal → backend ${BACKEND_URL}`);
  console.log(`portal          http://localhost:${PORT}/`);
  console.log(`inbound webhook POST http://localhost:${PORT}/inbound`);
  if (OIDC_ISSUER) console.log(`oidc sign-in    ${OIDC_ISSUER} (GET /auth/login)`);
});
