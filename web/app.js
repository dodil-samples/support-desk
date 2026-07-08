/*
 * Support Desk dashboard — a pure static page that calls the app's public
 * (anonymous, CORS-open) FQDN directly. No build step, no server.
 *
 * Help center (search_kb / submit_ticket / ticket_status) is the public path and
 * needs at most a project key. The agent inbox (stats / list_tickets) is private
 * and requires an admin key. Keys ride in the JSON body (the anon FQDN's CORS
 * preflight only allows content-type). Config: ?app=&pk=&ak= → localStorage → below.
 */
const DEFAULTS = {
  url: "https://support-desk-cardinalai.ignite.dodil.cloud/",
  pk: "",
  ak: "",
};

const qs = new URLSearchParams(location.search);
const store = {
  get url() { return qs.get("app") || localStorage.getItem("sd_url") || DEFAULTS.url; },
  get pk()  { return qs.get("pk")  || localStorage.getItem("sd_pk")  || DEFAULTS.pk; },
  get ak()  { return qs.get("ak")  || localStorage.getItem("sd_ak")  || DEFAULTS.ak; },
  set(url, pk, ak) {
    localStorage.setItem("sd_url", url); localStorage.setItem("sd_pk", pk); localStorage.setItem("sd_ak", ak);
  },
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

async function invoke(action, payload = {}, key = "") {
  const body = { action, ...payload };
  if (key) body.key = key;
  const res = await fetch(store.url, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return res.json().catch(() => ({ ok: false, error: "non-JSON response" }));
}

// ------------------------------------------------------------------ public: KB search
$("kbBtn").addEventListener("click", async () => {
  const out = $("kbOut"); out.innerHTML = `<span class="muted">Searching…</span>`;
  const r = await invoke("search_kb", { query: $("kbQ").value.trim(), top_k: 5 }, store.pk);
  if (!r.ok) { out.innerHTML = `<span class="err">${esc(r.error)}</span>`; return; }
  const hits = r.result.results || [];
  out.innerHTML = hits.length
    ? hits.map((h) => `<div class="kb-hit"><div>${esc((h.text || "").slice(0, 220))}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">${esc(h.key || "")}${
          h.score != null ? ` · score ${Number(h.score).toFixed(3)}` : ""}</div></div>`).join("")
    : `<span class="muted">No KB matches (the embedder may still be indexing).</span>`;
});

// ------------------------------------------------------------------ public: submit ticket
$("tSend").addEventListener("click", async () => {
  const btn = $("tSend"); btn.disabled = true;
  const r = await invoke("submit_ticket", {
    subject: $("tSubject").value.trim(), body: $("tBody").value.trim(),
    requester_email: $("tEmail").value.trim(), channel: "web",
  }, store.pk);
  const out = $("tOut"); out.classList.remove("hidden");
  out.textContent = JSON.stringify(r, null, 2);
  btn.disabled = false;
  if (r.ok && r.result.ticket_id) { $("sId").value = r.result.ticket_id; $("sEmail").value = $("tEmail").value.trim(); }
});

// ------------------------------------------------------------------ public: ticket status
$("sBtn").addEventListener("click", async () => {
  const out = $("sOut"); out.classList.remove("hidden"); out.textContent = "…";
  const r = await invoke("ticket_status", {
    ticket_id: $("sId").value.trim(), requester_email: $("sEmail").value.trim(),
  }, store.pk);
  out.textContent = JSON.stringify(r, null, 2);
});

// ------------------------------------------------------------------ private: agent inbox
const PRIORITY = { urgent: "var(--crit)", high: "var(--serious)", normal: "var(--warn)", low: "var(--good)" };

async function loadInbox() {
  const err = $("inboxErr");
  const [statsR, listR] = await Promise.all([
    invoke("stats", {}, store.ak),
    invoke("list_tickets", { status: $("statusFilter").value || undefined, limit: 50 }, store.ak),
  ]);
  if (!statsR.ok) {
    err.classList.remove("hidden"); err.textContent = JSON.stringify(statsR, null, 2);
    $("stats").classList.add("hidden"); $("queueWrap").classList.add("hidden");
    return;
  }
  err.classList.add("hidden");
  const s = statsR.result;
  $("stats").classList.remove("hidden");
  const openTotal = (s.open_by_priority || []).reduce((a, r) => a + Number(r.n), 0);
  $("statTiles").innerHTML = [
    ["Open tickets", openTotal],
    ["Avg CSAT", s.csat && s.csat.mean != null ? s.csat.mean : "—"],
    ["First-response p90 (min)", s.first_response_minutes && s.first_response_minutes.p90 != null ? s.first_response_minutes.p90 : "—"],
    ["SLA breaching", s.sla ? s.sla.breaching : "—"],
  ].map(([l, v]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  const pr = s.open_by_priority || [];
  const max = Math.max(1, ...pr.map((r) => Number(r.n)));
  $("priorityBars").innerHTML = pr.length ? pr.map((r) => `
    <div class="bar-row" title="${esc(r.priority)}: ${r.n}">
      <span class="name"><span class="dot" style="background:${PRIORITY[r.priority] || "var(--accent)"}"></span>${esc(r.priority)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(Number(r.n) / max * 100).toFixed(1)}%"></div></div>
      <span class="val">${r.n}</span></div>`).join("") : `<span class="muted">No open tickets.</span>`;

  if (listR.ok) {
    $("queueWrap").classList.remove("hidden");
    const rows = listR.result.tickets || [];
    $("queue").innerHTML = `<tr><th>Subject</th><th>Status</th><th>Priority</th><th>Category</th><th>Requester</th><th>Created</th></tr>` +
      (rows.length ? rows.map((t) => `<tr>
        <td>${esc(t.subject)}</td>
        <td><span class="pill">${esc(t.status)}</span></td>
        <td><span class="dot" style="display:inline-block;background:${PRIORITY[t.priority] || "var(--accent)"}"></span> ${esc(t.priority)}</td>
        <td>${esc(t.category)}</td>
        <td class="muted">${esc(t.requester_email)}</td>
        <td class="muted">${esc((t.created_at || "").slice(0, 16).replace("T", " "))}</td></tr>`).join("")
        : `<tr><td colspan="6" class="muted">No tickets for this filter.</td></tr>`);
  }
}
$("loadInbox").addEventListener("click", loadInbox);
$("statusFilter").addEventListener("change", () => { if (store.ak) loadInbox(); });

// ------------------------------------------------------------------ settings
function syncAdminHint() {
  $("adminHint").textContent = store.ak ? "Admin key set — inbox unlocked." : "Set an admin key in ⚙ Settings to unlock.";
}
$("gear").addEventListener("click", () => $("settings").classList.toggle("open"));
$("cfgSave").addEventListener("click", () => {
  store.set($("cfgUrl").value.trim() || DEFAULTS.url, $("cfgPk").value.trim(), $("cfgAk").value.trim());
  $("settings").classList.remove("open"); syncAdminHint();
  if (store.ak) loadInbox();
});

// init
$("urlEcho").textContent = store.url;
$("cfgUrl").value = store.url; $("cfgPk").value = store.pk; $("cfgAk").value = store.ak;
syncAdminHint();
