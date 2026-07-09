/* Smoke the portal + admin servers: allowlist, key injection, /inbound mapping, healthz. */
import { createServer } from "node:http";
import { spawn } from "node:child_process";

const received = [];
const stub = createServer((req, res) => {
  let d = ""; req.on("data", (c) => (d += c));
  req.on("end", () => {
    const body = JSON.parse(d || "{}");
    received.push(body);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, action: body.action, result: { echo: body } }));
  });
}).listen(9701);

const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));
const post = (port, path, body) =>
  fetch(`http://localhost:${port}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
    .then(async (r) => ({ status: r.status, json: await r.json().catch(() => null) }));

function start(script, env) {
  const p = spawn("node", [script], { env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
  p.stderr.on("data", (d) => process.stderr.write(`[${env.PORT}] ${d}`));
  return p;
}

const BASE = new URL("../web", import.meta.url).pathname;
let fails = 0;
const check = (name, cond, extra = "") => { console.log(`${cond ? "ok " : "FAIL"} ${name}${cond ? "" : " — " + extra}`); if (!cond) fails++; };

// ---- portal ----
const portal = start(`${BASE}/portal/server.mjs`, { BACKEND_URL: "http://localhost:9701/", PUBLIC_KEY: "pk_test", PORT: "9702" });
await wait(400);

let r = await fetch("http://localhost:9702/healthz");
check("portal /healthz", r.status === 200);

r = await post(9702, "/api", { action: "submit_ticket", subject: "s", body: "b", requester_email: "a@b.c" });
check("portal proxies submit_ticket", r.status === 200 && r.json.ok);
check("portal injects PUBLIC_KEY server-side", received.at(-1)?.key === "pk_test", JSON.stringify(received.at(-1)));

r = await post(9702, "/api", { action: "list_tickets" });
check("portal blocks private actions", r.status === 404, JSON.stringify(r));

r = await post(9702, "/inbound", { from: "sam@acme.io", subject: "Refund", text: "Charged twice." });
const inb = received.at(-1);
check("inbound → submit_ticket mapping", r.status === 200 && inb.action === "submit_ticket" &&
      inb.requester_email === "sam@acme.io" && inb.channel === "email", JSON.stringify(inb));

r = await fetch("http://localhost:9702/");
check("portal serves index.html", r.status === 200 && (await r.text()).includes("Help Center"));

portal.kill();

// ---- admin ----
const admin = start(`${BASE}/admin/server.mjs`, { BACKEND_URL: "http://localhost:9701/", ADMIN_KEY: "ak_test", PORT: "9703" });
await wait(400);

r = await post(9703, "/api", { action: "stats", key: "ak_attacker_supplied" });
check("admin proxies stats", r.status === 200 && r.json.ok);
check("admin overrides body key with env ADMIN_KEY", received.at(-1)?.key === "ak_test", JSON.stringify(received.at(-1)));

r = await post(9703, "/api", { action: "definitely_not_real" });
check("admin blocks unknown actions", r.status === 404);

r = await fetch("http://localhost:9703/");
check("admin serves index.html", r.status === 200 && (await r.text()).includes("Agent Inbox"));

admin.kill();

// ---- admin refuses to start without ADMIN_KEY ----
const bad = start(`${BASE}/admin/server.mjs`, { BACKEND_URL: "http://localhost:9701/", PORT: "9704" });
const code = await new Promise((ok) => bad.on("exit", ok));
check("admin exits without ADMIN_KEY", code === 1, `exit=${code}`);

stub.close();
process.exit(fails ? 1 : 0);
