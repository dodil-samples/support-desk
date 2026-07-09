/*
 * Customer portal server — static host + API proxy for the PUBLIC backend.
 *
 * The browser only ever calls its own origin (`POST /api`); this server forwards
 * to the public backend and injects the project key server-side. So the page
 * ships with zero configuration (no settings UI, no keys in localStorage) and
 * CORS never enters the picture. Also hosts the CRM inbound-email webhook.
 *
 * Zero dependencies (Node http/fs only). Env:
 *   BACKEND_URL  the public backend's FQDN (required)
 *   PUBLIC_KEY   project key injected into every proxied call (optional)
 *   PORT         listen port (default 8788; Ignite injects PORT)
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, extname, normalize } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_URL = process.env.BACKEND_URL || "";
const PUBLIC_KEY = process.env.PUBLIC_KEY || "";
const PORT = Number(process.env.PORT || 8788);

if (!BACKEND_URL) {
  console.error("BACKEND_URL is required — the public backend's FQDN (see .env.example)");
  process.exit(1);
}

// The portal is the public surface: only the customer-safe actions pass, no
// matter what the browser asks for.
const PUBLIC_ACTIONS = new Set(["submit_ticket", "ticket_status", "search_kb"]);

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon" };

function readBody(req) {
  return new Promise((resolve) => {
    let d = ""; req.on("data", (c) => (d += c));
    req.on("end", () => { try { resolve(JSON.parse(d || "{}")); } catch { resolve({}); } });
  });
}

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
  if (u.pathname === "/api" && req.method === "POST") {
    const body = await readBody(req);
    if (!PUBLIC_ACTIONS.has(body.action)) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end(JSON.stringify({ ok: false, error: `unknown action ${JSON.stringify(body.action)}` }));
    }
    const { status, json } = await forward(body);
    res.writeHead(status, { "content-type": "application/json" });
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
});
