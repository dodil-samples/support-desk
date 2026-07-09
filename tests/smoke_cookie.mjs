// Portal cookie flow: verify_code -> HttpOnly cookie (token stripped from JSON),
// next /api call carries body.session, logout clears it.
import { createServer } from "node:http";
import { spawn } from "node:child_process";

const seen = [];
const stub = createServer(async (req, res) => {
  let d = ""; for await (const c of req) d += c;
  const body = JSON.parse(d); seen.push(body);
  const out = body.action === "verify_code"
    ? { ok: true, action: "verify_code", result: { session: "sd1.tok.sig", identity: { email: "a@b.c", role: "user" } } }
    : { ok: true, action: body.action, result: { echo_session: body.session || null } };
  res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(out));
}).listen(9711);

const srv = spawn("node", [new URL("../web/portal/server.mjs", import.meta.url).pathname], {
  env: { ...process.env, BACKEND_URL: "http://127.0.0.1:9711/", PUBLIC_KEY: "pk_t", PORT: "9712" },
  stdio: "inherit",
});
await new Promise((r) => setTimeout(r, 400));
const api = (body, cookie) => fetch("http://127.0.0.1:9712/api", {
  method: "POST", headers: { "content-type": "application/json", ...(cookie ? { cookie } : {}) },
  body: JSON.stringify(body),
});

let fails = 0;
const check = (name, cond) => { console.log((cond ? "ok  " : "FAIL") + " " + name); if (!cond) fails++; };

const v = await api({ action: "verify_code", email: "a@b.c", code: "123456" });
const setC = v.headers.get("set-cookie") || "";
const vj = await v.json();
check("verify_code sets HttpOnly cookie", setC.includes("sd_session=sd1.tok.sig") && setC.includes("HttpOnly"));
check("session token stripped from browser JSON", vj.result.session === undefined);

const w = await api({ action: "whoami" }, "sd_session=sd1.tok.sig");
const wj = await w.json();
check("cookie injected as body.session", wj.result.echo_session === "sd1.tok.sig");
check("backend saw session field", seen.some((b) => b.action === "whoami" && b.session === "sd1.tok.sig"));

const lo = await fetch("http://127.0.0.1:9712/auth/logout", { method: "POST" });
check("logout clears cookie", (lo.headers.get("set-cookie") || "").includes("Max-Age=0"));

srv.kill(); stub.close();
process.exit(fails ? 1 : 0);
