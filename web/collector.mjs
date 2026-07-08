/*
 * Optional collector — turns inbound CRM / email events into public tickets, and
 * statically serves the dashboard, so `node web/collector.mjs` gives you both.
 *
 * The dashboard talks to the anon FQDN directly; this shim exists for the one
 * server-to-server case: a CRM / email provider posting an inbound message webhook
 * (no browser, no JS) that should open a ticket. It normalises common webhook
 * shapes to the app's public submit_ticket.
 *
 * Zero dependencies (Node http/fs only). Env:
 *   APP_URL     the app's anon FQDN (default: the cardinalai dev deployment)
 *   PUBLIC_KEY  project key added to submissions (optional)
 *   PORT        listen port (default 8788)
 *
 * Inbound email webhook:
 *   curl -X POST localhost:8788/inbound \
 *     -d '{"from":"sam@acme.io","subject":"Refund","text":"Charged twice."}'
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, extname, normalize } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_URL = process.env.APP_URL || "https://support-desk-cardinalai.ignite.dodil.cloud/";
const PUBLIC_KEY = process.env.PUBLIC_KEY || "";
const PORT = Number(process.env.PORT || 8788);

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon" };

function readBody(req) {
  return new Promise((resolve) => {
    let d = ""; req.on("data", (c) => (d += c));
    req.on("end", () => { try { resolve(JSON.parse(d || "{}")); } catch { resolve({}); } });
  });
}

/** Map a range of inbound-email/webhook shapes to submit_ticket and forward it. */
async function submit(msg) {
  const body = {
    action: "submit_ticket",
    subject: msg.subject || msg.Subject || "(no subject)",
    body: msg.text || msg.body || msg["body-plain"] || msg.html || "",
    requester_email: msg.from || msg.From || msg.sender || msg.email || "",
    channel: "email",
  };
  if (PUBLIC_KEY) body.key = PUBLIC_KEY;
  const res = await fetch(APP_URL, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return res.json().catch(() => ({ ok: false, error: "non-JSON response" }));
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
  if (u.pathname === "/inbound" && req.method === "POST") {
    const result = await submit(await readBody(req));
    res.writeHead(result.ok ? 200 : 502, { "content-type": "application/json" });
    return res.end(JSON.stringify(result));
  }
  return serveStatic(res, u.pathname);
}).listen(PORT, () => {
  console.log(`collector → app ${APP_URL}`);
  console.log(`dashboard      http://localhost:${PORT}/`);
  console.log(`inbound webhook POST http://localhost:${PORT}/inbound`);
});
