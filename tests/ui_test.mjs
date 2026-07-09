// Full-browser UI test of the deployed portal + inbox (Playwright/Chromium).
// Covers: load, KB search, email sign-in (demo code), my-tickets, signed-in
// instant submit, status timeline, customer reply, sign-out, inbox verified badge.
// Needs the backends on AUTH_MODE=email + SEND_MODE=test (the demo-code flow).
//
//   PORTAL_URL=https://… INBOX_URL=https://… node web/ui_test.mjs
//
// (requires playwright: a local `npm i playwright` or a global `npm i -g playwright`)
import { createRequire } from "node:module";
import { execSync } from "node:child_process";

let chromium;
try {
  ({ chromium } = await import("playwright"));
} catch { // fall back to the global install
  const globalRoot = execSync("npm root -g").toString().trim();
  ({ chromium } = createRequire(globalRoot + "/")("playwright"));
}

const PORTAL = process.env.PORTAL_URL || "http://localhost:8788";
const INBOX = process.env.INBOX_URL || "http://localhost:8789";
const EMAIL = process.env.TEST_EMAIL || "uitest@acme.io";
const SHOT = (n) => `ui_${n}.png`;

let passed = 0, failed = 0;
const ok = (name, cond) => { console.log(`${cond ? "ok  " : "FAIL"} ${name}`); cond ? passed++ : failed++; };

const browser = await chromium.launch();
const page = await browser.newPage();
page.setDefaultTimeout(45000);
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

// ---- 1. load + auth-aware chrome
await page.goto(PORTAL, { waitUntil: "networkidle" });
ok("portal loads", (await page.title()).includes("Help Center"));
await page.locator("#acctCard").waitFor({ state: "visible" });
ok("sign-in card shown (AUTH_MODE=email advertised)", await page.locator("#aSend").isVisible());
ok("my-tickets hidden while anonymous", !(await page.locator("#mineCard").isVisible()));

// ---- 2. KB search
await page.fill("#kbQ", "duplicate charge refund");
await page.click("#kbBtn");
await page.locator("#kbOut .kb-hit, #kbOut .muted").first().waitFor();
const kbText = await page.locator("#kbOut").innerText();
ok("KB search returns content", kbText.trim().length > 0);

// ---- 3. passwordless sign-in via demo code
await page.fill("#aEmail", EMAIL);
await page.click("#aSend");
await page.locator("#signinCodeStep").waitFor({ state: "visible" });
const hint = await page.locator("#aCodeHint").innerText();
const code = (hint.match(/(\d{6})/) || [])[1];
ok("demo code surfaced in test mode", !!code);
await page.fill("#aCode", code || "000000");
await page.click("#aVerify");
await page.locator("#mineCard").waitFor({ state: "visible" });
ok("signed in — account badge shows email", (await page.locator("#acctBadge").innerText()).includes(EMAIL));
ok("email field prefilled + locked", await page.locator("#tEmail").isDisabled()
   && (await page.inputValue("#tEmail")) === EMAIL);
await page.screenshot({ path: SHOT("signed_in"), fullPage: true });

// ---- 4. signed-in instant submit
await page.fill("#tSubject", "UI test — cannot connect my agent");
await page.fill("#tBody", "Playwright end-to-end run: the MCP endpoint refuses my token since this morning.");
await page.click("#tSend");
await page.locator("#tConfirm .tid").waitFor();
const confirm = await page.locator("#tConfirm").innerText();
const tid = (confirm.match(/t_[0-9a-f]+/) || [])[0];
ok("submit confirms with ticket id", !!tid);
ok("submit acknowledges verified account", confirm.includes("My tickets"));
const ms = Number((confirm.match(/filed in (\d+)/) || [])[1] || 99999);
ok(`submit instant (${ms} ms)`, ms < 5000);

// ---- 5. my-tickets lists it; click-through renders timeline + reply box
await page.locator(`#mineOut [data-tid="${tid}"]`).waitFor();
ok("new ticket appears in My tickets", true);
await page.click(`#mineOut [data-tid="${tid}"]`);
await page.locator("#sOut .timeline .msg").first().waitFor();
ok("status timeline renders", (await page.locator("#sOut .timeline .msg").count()) >= 1);
await page.locator("#replyBox").waitFor({ state: "visible" });
ok("reply box visible for own ticket", true);

// ---- 6. customer reply from the portal
await page.fill("#rBody", "Adding info from the browser test: token id is tok_123.");
await page.click("#rSend");
await page.waitForFunction(() => /Sent/.test(document.getElementById("rHint").textContent));
await page.waitForFunction(() => document.querySelectorAll("#sOut .timeline .msg").length >= 2);
ok("reply lands in the timeline", true);
await page.screenshot({ path: SHOT("replied"), fullPage: true });

// ---- 7. sign out
await page.click("#aOut");
await page.locator("#acctCard").waitFor({ state: "visible" });
ok("sign-out restores anonymous chrome", !(await page.locator("#mineCard").isVisible())
   && !(await page.locator("#tEmail").isDisabled()));

// ---- 8. signed-out my_tickets is refused via UI path (direct api probe from page)
const anonProbe = await page.evaluate(async () => {
  const r = await fetch("/api", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ action: "my_tickets" }) });
  return r.json();
});
ok("anonymous my_tickets rejected (401)", anonProbe.ok === false && anonProbe.code === 401);

// ---- 9. agent inbox: queue renders with the verified badge
await page.goto(INBOX, { waitUntil: "networkidle" });
await page.locator("tr.t").first().waitFor();
const queue = await page.locator("#queue").innerText();
ok("inbox queue renders tickets", (await page.locator("tr.t").count()) >= 1);
ok("verified ✓ badge shown for proven requesters", queue.includes("✓"));

// ---- 10. staffing & routing panels (skipped when the desk has no agents yet)
const agentsText = await page.locator("#agents").innerText();
if (/No agents yet/.test(agentsText)) {
  console.log("skip agents/rules/routing checks — desk has no registered agents");
} else {
  ok("agents panel lists the registry (incl. AI 🤖)", agentsText.includes("🤖"));
  ok("rules panel lists rules (seeded catch-alls at least)",
     (await page.locator("#rules tr").count()) >= 2);
  ok("queue shows the assignee column", queue.toLowerCase().includes("assignee"));
  // newest ticket first — open it and expect a routing history trail
  await page.locator("tr.t").first().click();
  await page.waitForFunction(() =>
    document.getElementById("dRouting").textContent.trim().length > 0);
  const trail = await page.locator("#dRouting").innerText();
  ok("ticket detail shows WHY it was routed", /rule:|ai:|manual:|no routing/.test(trail));
}
await page.screenshot({ path: SHOT("inbox"), fullPage: true });

const realErrors = errors.filter((e) => !/favicon/.test(e));
ok("no console/page errors across the run", realErrors.length === 0);
if (realErrors.length) console.log("errors:", realErrors.slice(0, 5));

await browser.close();
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
