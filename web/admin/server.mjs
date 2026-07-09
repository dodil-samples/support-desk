/*
 * Admin portal server — static host + API proxy for the ADMIN backend.
 *
 * The admin key lives HERE, in server env, and is injected into every proxied
 * call — it never reaches the browser (no settings UI, nothing in localStorage).
 * In a real deployment you'd additionally put SSO / an IdP in front of this app;
 * for the sample the perimeter is: keep this app's URL private + the key
 * server-side, while the backend enforces the key regardless.
 *
 * Zero dependencies (Node http/fs only). Env:
 *   BACKEND_URL  the admin backend's FQDN (required)
 *   ADMIN_KEY    admin key injected into every proxied call (required)
 *   PORT         listen port (default 8789; Ignite injects PORT)
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, extname, normalize } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_URL = process.env.BACKEND_URL || "";
const ADMIN_KEY = process.env.ADMIN_KEY || "";
const PORT = Number(process.env.PORT || 8789);

if (!BACKEND_URL) {
  console.error("BACKEND_URL is required — the admin backend's FQDN (see .env.example)");
  process.exit(1);
}
if (!ADMIN_KEY) {
  console.error("ADMIN_KEY is required — the admin backend is fail-closed without it");
  process.exit(1);
}

// Everything an agent does, including the public actions (an admin key
// satisfies any tier). Kept explicit so a typo'd action fails here, not there.
const ACTIONS = new Set([
  "list_tickets", "get_ticket", "stats", "add_message", "triage", "suggest_reply",
  "set_status", "assign", "rate", "export_tickets", "add_kb", "create_ticket",
  "create_key", "list_keys", "revoke_key",
  "add_agent", "update_agent", "remove_agent", "list_agents",
  "upsert_rule", "delete_rule", "list_rules",
  "list_kb", "get_kb", "remove_kb",
  "search_kb", "ticket_status", "submit_ticket",
]);

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon" };

function readBody(req) {
  return new Promise((resolve) => {
    let d = ""; req.on("data", (c) => (d += c));
    req.on("end", () => { try { resolve(JSON.parse(d || "{}")); } catch { resolve({}); } });
  });
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
    if (!ACTIONS.has(body.action)) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end(JSON.stringify({ ok: false, error: `unknown action ${JSON.stringify(body.action)}` }));
    }
    body.key = ADMIN_KEY;
    const r = await fetch(BACKEND_URL, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    }).catch((e) => { console.error("backend fetch failed:", e?.cause?.code || e?.cause?.message || e.message); return null; });
    const json = r ? await r.json().catch(() => ({ ok: false, error: "non-JSON response" }))
                   : { ok: false, error: "backend unreachable" };
    res.writeHead(r ? 200 : 502, { "content-type": "application/json" });
    return res.end(JSON.stringify(json));
  }
  return serveStatic(res, u.pathname);
}).listen(PORT, () => {
  console.log(`admin portal → backend ${BACKEND_URL}`);
  console.log(`inbox          http://localhost:${PORT}/`);
});
