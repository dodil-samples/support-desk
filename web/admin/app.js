/*
 * Agent inbox — talks ONLY to its own origin (POST /api); the server proxies the
 * admin backend and injects the admin key. No configuration in the page.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Mirrors STATUSES in handler.py — the backend rejects anything else.
const STATUSES = ["new", "open", "pending", "solved"];
const STATUS_COLOR = { new: "var(--accent)", open: "var(--warn)", pending: "var(--serious)", solved: "var(--good)" };
const PRIORITY = { urgent: "var(--crit)", high: "var(--serious)", normal: "var(--warn)", low: "var(--good)" };

const statusPill = (s) => `<span class="pill" style="color:${STATUS_COLOR[s] || "var(--muted)"};
  border-color:color-mix(in srgb, ${STATUS_COLOR[s] || "var(--muted)"} 45%, transparent)">${esc(s)}</span>`;

async function api(action, payload = {}) {
  const res = await fetch("/api", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  return res.json().catch(() => ({ ok: false, error: "non-JSON response" }));
}

function fail(msg) {
  $("topErr").classList.remove("hidden");
  $("topErr").textContent = typeof msg === "string" ? msg : JSON.stringify(msg, null, 2);
}

// ---------------------------------------------------------------- inbox
async function loadInbox() {
  const [statsR, listR] = await Promise.all([
    api("stats"),
    api("list_tickets", { status: $("statusFilter").value || undefined, limit: 50 }),
  ]);
  if (!statsR.ok) return fail(statsR);
  $("topErr").classList.add("hidden");

  const s = statsR.result;
  const openTotal = (s.open_by_priority || []).reduce((a, r) => a + Number(r.n), 0);
  $("statTiles").innerHTML = [
    ["Open tickets", openTotal],
    ["Avg CSAT", s.csat && s.csat.mean != null ? s.csat.mean : "—"],
    ["First-response p90 (min)", s.first_response_minutes?.p90 ?? "—"],
    ["SLA breaching", s.sla ? s.sla.breaching : "—"],
  ].map(([l, v]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  const pr = s.open_by_priority || [];
  const max = Math.max(1, ...pr.map((r) => Number(r.n)));
  $("priorityBars").innerHTML = pr.length ? pr.map((r) => `
    <div class="bar-row" title="${esc(r.priority)}: ${r.n}">
      <span class="name"><span class="dot" style="background:${PRIORITY[r.priority] || "var(--accent)"}"></span>${esc(r.priority)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(Number(r.n) / max * 100).toFixed(1)}%"></div></div>
      <span class="val">${r.n}</span></div>`).join("") : `<span class="muted">No open tickets.</span>`;

  if (!listR.ok || listR.result?.error) return fail(listR.result?.error || listR);
  const rows = listR.result.tickets || [];
  $("queue").innerHTML = `<tr><th>Subject</th><th>Status</th><th>Priority</th><th>Category</th><th>Requester</th><th>Created</th></tr>` +
    (rows.length ? rows.map((t) => `<tr class="t" data-id="${esc(t.ticket_id)}">
      <td>${esc(t.subject)}</td>
      <td>${statusPill(t.status)}</td>
      <td><span class="dot" style="background:${PRIORITY[t.priority] || "var(--accent)"}"></span> ${esc(t.priority)}</td>
      <td>${esc(t.category)}</td>
      <td class="muted">${esc(t.requester_email)}</td>
      <td class="muted">${esc((t.created_at || "").slice(0, 16).replace("T", " "))}</td></tr>`).join("")
      : `<tr><td colspan="6" class="muted">No tickets for this filter.</td></tr>`);
  document.querySelectorAll("tr.t").forEach((tr) =>
    tr.addEventListener("click", () => openTicket(tr.dataset.id)));
}

// ---------------------------------------------------------------- detail
let currentId = "";
async function openTicket(tid) {
  currentId = tid;
  $("detail").classList.remove("hidden");
  $("dId").textContent = tid;
  $("dMsgs").innerHTML = `<span class="muted">Loading…</span>`;
  const r = await api("get_ticket", { ticket_id: tid });
  if (!r.ok) return fail(r);
  const t = r.result.ticket || {}, msgs = r.result.messages || [];
  $("dSubject").textContent = t.subject || "";
  $("dPills").innerHTML = `${statusPill(t.status)} <span class="pill">${esc(t.priority)}</span>
    <span class="pill">${esc(t.category)}</span>${t.csat ? ` <span class="pill">CSAT ${esc(t.csat)}</span>` : ""}`;
  $("dMsgs").innerHTML = msgs.map((m) => `<div class="msg ${esc(m.role)}"><div class="who">${esc(m.role)} · ${esc(m.author)}
    · ${esc((m.created_at || "").slice(0, 16).replace("T", " "))}</div>${esc(m.snippet)}</div>`).join("")
    || `<span class="muted">No messages.</span>`;
  $("dStatus").value = STATUSES.includes(t.status) ? t.status : "new";
  $("dAssignee").value = t.assignee || "";
  $("dReply").value = ""; $("dHint").textContent = "";
}

$("dDraft").addEventListener("click", async () => {
  if (!currentId) return;
  $("dDraft").disabled = true; $("dHint").textContent = "Drafting from the KB…";
  const r = await api("suggest_reply", { ticket_id: currentId });
  $("dDraft").disabled = false;
  if (!r.ok) { $("dHint").textContent = r.error; return; }
  $("dReply").value = r.result.draft_reply || "";
  $("dHint").textContent = `grounded in ${(r.result.kb_used || []).length} KB article(s) — review before sending`;
});

$("dSend").addEventListener("click", async () => {
  const body = $("dReply").value.trim();
  if (!currentId || !body) return;
  $("dSend").disabled = true;
  const r = await api("add_message", { ticket_id: currentId, role: "agent", author: "agent", body });
  $("dSend").disabled = false;
  $("dHint").textContent = r.ok ? "Reply sent." : (r.error || "failed");
  if (r.ok) { openTicket(currentId); loadInbox(); }
});

$("dUpdate").addEventListener("click", async () => {
  if (!currentId) return;
  const status = $("dStatus").value, assignee = $("dAssignee").value.trim();
  const calls = [api("set_status", { ticket_id: currentId, status })];
  if (assignee) calls.push(api("assign", { ticket_id: currentId, assignee }));
  const rs = await Promise.all(calls);
  const bad = rs.find((r) => !r.ok || r.result?.error);
  $("dHint").textContent = bad ? (bad.result?.error || bad.error) : "Updated.";
  openTicket(currentId); loadInbox();
});

// ---------------------------------------------------------------- keys
async function loadKeys() {
  const r = await api("list_keys");
  if (!r.ok) { $("keys").innerHTML = `<tr><td class="err">${esc(r.error)}</td></tr>`; return; }
  const rows = r.result.keys || [];
  $("keys").innerHTML = `<tr><th>Key</th><th>Kind</th><th>Label</th><th>Created</th><th></th></tr>` +
    (rows.length ? rows.map((k) => `<tr${k.disabled ? ' style="opacity:.45"' : ""}>
      <td><code>${esc(k.key)}</code></td><td>${esc(k.kind)}</td><td>${esc(k.label)}</td>
      <td class="muted">${esc((k.created_at || "").slice(0, 16).replace("T", " "))}</td>
      <td>${k.disabled ? "revoked" : `<button data-k="${esc(k.key)}" class="revoke">revoke</button>`}</td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">No runtime keys (env keys are not listed).</td></tr>`);
  document.querySelectorAll("button.revoke").forEach((b) =>
    b.addEventListener("click", async () => { await api("revoke_key", { revoke: b.dataset.k }); loadKeys(); }));
}

$("kCreate").addEventListener("click", async () => {
  const r = await api("create_key", { kind: $("kKind").value, label: $("kLabel").value.trim() });
  $("kHint").textContent = r.ok ? `created ${r.result.key}` : (r.error || "failed");
  loadKeys();
});

// ---------------------------------------------------------------- init
$("statusFilter").innerHTML = `<option value="">all statuses</option>` +
  STATUSES.map((s) => `<option value="${s}">${s}</option>`).join("");
$("dStatus").innerHTML = STATUSES.map((s) => `<option value="${s}">${s}</option>`).join("");
$("statusFilter").addEventListener("change", loadInbox);
$("refresh").addEventListener("click", () => { loadInbox(); loadKeys(); });
loadInbox(); loadKeys();
