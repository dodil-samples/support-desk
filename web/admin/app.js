/*
 * Agent inbox — talks ONLY to its own origin (POST /api); the server proxies the
 * admin backend and injects the admin key. No configuration in the page.
 *
 * Shell: left navigation + hash-routed views (#dashboard/#tickets/#agents/
 * #routing/#kb/#keys) — a tiny router, no framework.
 */
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- router
const NAV = document.querySelectorAll("#nav a[data-view]");
function show(view) {
  if (!document.getElementById("view-" + view)) view = "dashboard";
  document.querySelectorAll(".view").forEach((s) =>
    s.classList.toggle("active", s.id === "view-" + view));
  NAV.forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
}
NAV.forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); show(a.dataset.view); }));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Mirrors STATUSES in actions/common.py — the backend rejects anything else.
const STATUSES = ["new", "open", "pending", "solved"];
// Mirror the triage enums (TRIAGE_SYSTEM in actions/triage.py) — rules match on
// these, so the rule form offers exactly what triage can produce.
const CATEGORIES = ["billing", "bug", "how_to", "account", "feature_request", "other"];
const PRIORITIES = ["low", "normal", "high", "urgent"];
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

// ---------------------------------------------------------------- agents (human + AI)
let AGENTS = []; // cached registry for the dropdowns

function agentLabel(id) {
  const a = AGENTS.find((x) => x.agent_id === id);
  if (!a) return id;
  return a.kind === "ai" ? `🤖 ${a.name}` : a.name || id;
}

async function loadAgents() {
  const r = await api("list_agents");
  AGENTS = r.ok ? r.result.agents || [] : [];
  $("agents").innerHTML = `<tr><th>Agent</th><th>Kind</th><th>Role</th><th>Skills</th><th></th></tr>` +
    (AGENTS.length ? AGENTS.map((a) => `<tr${a.active ? "" : ' style="opacity:.45"'}>
      <td>${a.kind === "ai" ? "🤖 " : ""}${esc(a.name)} <span class="muted">${esc(a.email || a.agent_id)}</span></td>
      <td>${esc(a.kind)}</td><td>${esc(a.role || "—")}</td>
      <td class="muted">${esc((a.skills || []).join(", ") || "generalist")}</td>
      <td class="row" style="gap:4px">${a.active
        ? (a.kind === "human" ? `<button class="arole" data-a="${esc(a.agent_id)}"
             data-r="${a.role === "admin" ? "agent" : "admin"}">make ${a.role === "admin" ? "agent" : "admin"}</button>` : "")
          + `<button class="deact" data-a="${esc(a.agent_id)}">deactivate</button>`
        : `<button class="react" data-a="${esc(a.agent_id)}">reactivate</button>`}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="muted">No agents yet — routing leaves tickets unassigned until you add one.</td></tr>`);
  document.querySelectorAll("button.deact").forEach((b) =>
    b.addEventListener("click", async () => { await api("remove_agent", { agent_id: b.dataset.a }); loadAgents(); }));
  document.querySelectorAll("button.react").forEach((b) =>
    b.addEventListener("click", async () => { await api("update_agent", { agent_id: b.dataset.a, active: true }); loadAgents(); }));
  document.querySelectorAll("button.arole").forEach((b) =>
    b.addEventListener("click", async () => { await api("update_agent", { agent_id: b.dataset.a, role: b.dataset.r }); loadAgents(); }));
  // feed the dropdowns (assignees, rule targets, skills seen across agents)
  const opts = AGENTS.filter((a) => a.active).map((a) =>
    `<option value="${esc(a.agent_id)}">${a.kind === "ai" ? "🤖 " : ""}${esc(a.name)}</option>`).join("");
  $("dAssignee").innerHTML = `<option value="">unassigned</option>` + opts;
  $("rAssign").innerHTML = `<option value="">→ pool (round-robin)</option>` + opts;
  $("assigneeFilter").innerHTML = `<option value="">all agents</option>` + opts;
  const skills = [...new Set(AGENTS.flatMap((a) => a.skills || []))];
  $("rSkill").innerHTML = `<option value="">any skill</option>` +
    skills.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
}

$("aKind").addEventListener("change", () => {
  const ai = $("aKind").value === "ai";
  $("aEmail").classList.toggle("hidden", ai);
  $("aRole").classList.toggle("hidden", ai);
  $("aName").classList.toggle("hidden", !ai);
});

$("aAdd").addEventListener("click", async () => {
  const kind = $("aKind").value;
  const skills = $("aSkills").value.split(",").map((s) => s.trim()).filter(Boolean);
  const r = await api("add_agent", kind === "ai"
    ? { kind, name: $("aName").value.trim(), skills }
    : { kind, email: $("aEmail").value.trim(), role: $("aRole").value, skills });
  $("aHint").textContent = r.ok && !r.result?.error ? `added ${r.result.agent_id}` : (r.result?.error || r.error);
  loadAgents();
});

// ---------------------------------------------------------------- routing rules
async function loadRules() {
  const r = await api("list_rules");
  const rows = r.ok ? r.result.rules || [] : [];
  $("rules").innerHTML = `<tr><th>Pos</th><th>On</th><th>Match</th><th>Assign</th><th>AI?</th><th></th></tr>` +
    (rows.length ? rows.map((x) => `<tr>
      <td>${esc(x.position)}</td><td>${esc(x.on_event)}</td>
      <td class="muted">${esc([x.category, x.priority, x.channel].filter(Boolean).join(" · ") || "any")}</td>
      <td>${x.assign_to ? esc(agentLabel(x.assign_to)) : `pool${x.pool_skill ? ` (${esc(x.pool_skill)})` : ""}`}</td>
      <td>${x.allow_ai ? "yes" : "—"}</td>
      <td><button class="rdel" data-r="${esc(x.rule_id)}">disable</button></td>
    </tr>`).join("") : `<tr><td colspan="6" class="muted">No rules.</td></tr>`);
  document.querySelectorAll("button.rdel").forEach((b) =>
    b.addEventListener("click", async () => { await api("delete_rule", { rule_id: b.dataset.r }); loadRules(); }));
}

$("rAdd").addEventListener("click", async () => {
  const r = await api("upsert_rule", {
    position: Number($("rPos").value || 100), on_event: $("rEvent").value,
    category: $("rCategory").value, priority: $("rPriority").value,
    assign_to: $("rAssign").value, pool_skill: $("rSkill").value,
    allow_ai: $("rAllowAi").checked,
  });
  $("rHint").textContent = r.ok && !r.result?.error ? `rule ${r.result.rule_id}` : (r.result?.error || r.error);
  loadRules();
});

// ---------------------------------------------------------------- inbox
async function loadInbox() {
  const [statsR, listR] = await Promise.all([
    api("stats"),
    api("list_tickets", { status: $("statusFilter").value || undefined,
                          assignee: $("assigneeFilter").value || undefined, limit: 50 }),
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
  $("queue").innerHTML = `<tr><th>Subject</th><th>Status</th><th>Priority</th><th>Category</th><th>Assignee</th><th>Requester</th><th>Created</th></tr>` +
    (rows.length ? rows.map((t) => `<tr class="t" data-id="${esc(t.ticket_id)}">
      <td>${esc(t.subject)}</td>
      <td>${statusPill(t.status)}</td>
      <td><span class="dot" style="background:${PRIORITY[t.priority] || "var(--accent)"}"></span> ${esc(t.priority)}</td>
      <td>${esc(t.category)}</td>
      <td class="muted">${t.assignee ? esc(agentLabel(t.assignee)) : "—"}</td>
      <td class="muted">${esc(t.requester_email)}${t.requester_verified
        ? ` <span title="requester proved this email" style="color:var(--good)">✓</span>` : ""}</td>
      <td class="muted">${esc((t.created_at || "").slice(0, 16).replace("T", " "))}</td></tr>`).join("")
      : `<tr><td colspan="7" class="muted">No tickets for this filter.</td></tr>`);
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
  $("dMsgs").innerHTML = msgs.map((m) => `<div class="msg ${esc(m.role)}"><div class="who">${esc(m.role)} · ${esc(agentLabel(m.author))}
    · ${esc((m.created_at || "").slice(0, 16).replace("T", " "))}</div>${esc(m.snippet)}</div>`).join("")
    || `<span class="muted">No messages.</span>`;
  $("dStatus").value = STATUSES.includes(t.status) ? t.status : "new";
  $("dAssignee").value = t.assignee || "";
  $("dReply").value = ""; $("dHint").textContent = "";
  // Why is this ticket where it is? Every routing decision, in order.
  const log = r.result.routing_log || [];
  $("dRouting").innerHTML = log.length ? log.map((l) => `
    <div>· <strong>${esc(l.event)}</strong> → ${l.assign_to ? esc(agentLabel(l.assign_to)) : "unassigned"}
      <span class="muted">(${esc(l.decided_by)}${l.reason ? ` — ${esc(l.reason)}` : ""})</span></div>`).join("")
    : "no routing decisions yet";
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
  const status = $("dStatus").value, assignee = $("dAssignee").value;
  const calls = [api("set_status", { ticket_id: currentId, status })];
  if (assignee) calls.push(api("assign", { ticket_id: currentId, assignee }));
  const rs = await Promise.all(calls);
  const bad = rs.find((r) => !r.ok || r.result?.error);
  $("dHint").textContent = bad ? (bad.result?.error || bad.error) : "Updated.";
  openTicket(currentId); loadInbox();
});

// ---------------------------------------------------------------- knowledge base
async function loadKb() {
  const r = await api("list_kb");
  const rows = r.ok ? r.result.articles || [] : [];
  $("kb").innerHTML = `<tr><th>Article</th><th>Size</th><th></th></tr>` +
    (rows.length ? rows.map((a) => `<tr>
      <td><a href="#" class="kbv" data-k="${esc(a.key)}" style="color:inherit">${esc(a.key.replace(/^kb\//, ""))}</a></td>
      <td class="muted">${(a.size / 1024).toFixed(1)} kB</td>
      <td><button class="kbd" data-k="${esc(a.key)}">delete</button></td>
    </tr>`).join("") : `<tr><td colspan="3" class="muted">No articles — deflection and AI agents have nothing to answer from.</td></tr>`);
  document.querySelectorAll("a.kbv").forEach((a) =>
    a.addEventListener("click", async (e) => {
      e.preventDefault();
      const r = await api("get_kb", { kb_key: a.dataset.k });
      $("kbView").classList.remove("hidden");
      $("kbView").textContent = r.ok && !r.result?.error ? r.result.body : (r.result?.error || r.error);
    }));
  document.querySelectorAll("button.kbd").forEach((b) =>
    b.addEventListener("click", async () => {
      const r = await api("remove_kb", { kb_key: b.dataset.k });
      $("kbHint").textContent = r.ok && !r.result?.error ? `deleted ${b.dataset.k}` : (r.result?.error || r.error);
      loadKb();
    }));
}

$("kbAdd").addEventListener("click", async () => {
  const title = $("kbTitle").value.trim(), body = $("kbBody").value.trim();
  if (!title || !body) { $("kbHint").textContent = "title and body are required"; return; }
  $("kbAdd").disabled = true;
  const r = await api("add_kb", { title, body });
  $("kbAdd").disabled = false;
  $("kbHint").textContent = r.ok ? `published ${r.result.kb_key} — embedding ~30 s` : (r.error || "failed");
  if (r.ok) { $("kbTitle").value = ""; $("kbBody").value = ""; loadKb(); }
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
$("rCategory").innerHTML = `<option value="">any category</option>` +
  CATEGORIES.map((c) => `<option value="${c}">${c}</option>`).join("");
$("rPriority").innerHTML = `<option value="">any priority</option>` +
  PRIORITIES.map((p) => `<option value="${p}">${p}</option>`).join("");
$("statusFilter").addEventListener("change", loadInbox);
$("assigneeFilter").addEventListener("change", loadInbox);
$("refresh").addEventListener("click", () => { loadAgents().then(loadInbox); loadRules(); loadKb(); loadKeys(); });
show(location.hash.slice(1) || "dashboard");
loadAgents().then(loadInbox); loadRules(); loadKb(); loadKeys();
