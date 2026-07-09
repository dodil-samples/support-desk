/*
 * Customer portal — talks ONLY to its own origin (POST /api); the server proxies
 * the public backend and injects the project key. No configuration in the page.
 *
 * Submission is instant (the backend files the ticket and triages in the
 * background); KB suggestions are a SEPARATE call — debounced while typing, and
 * polled a few times after submit in case the collection is still indexing.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Mirrors STATUSES in handler.py — the backend rejects anything else.
const STATUS = {
  new:     { label: "New",         color: "var(--accent)" },
  open:    { label: "In progress", color: "var(--warn)" },
  pending: { label: "Waiting on you", color: "var(--serious)" },
  solved:  { label: "Resolved",    color: "var(--good)" },
};
const statusPill = (s) => {
  const st = STATUS[s] || { label: s, color: "var(--muted)" };
  return `<span class="pill" style="color:${st.color};border-color:color-mix(in srgb, ${st.color} 45%, transparent)">${esc(st.label)}</span>`;
};

async function api(action, payload = {}) {
  const res = await fetch("/api", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  return res.json().catch(() => ({ ok: false, error: "non-JSON response" }));
}

// ---------------------------------------------------------------- account / sign-in
// The session credential is an HttpOnly cookie owned by the portal server; this
// page only ever learns WHO is signed in (whoami), never the token itself.
let me = null; // {email, name, role} when signed in

function renderAccount() {
  $("acctBadge").innerHTML = me
    ? `${esc(me.email)} · <a href="#" id="aOut" style="color:inherit">sign out</a>` : "";
  $("mineCard").classList.toggle("hidden", !me);
  $("acctCard").classList.toggle("hidden", !!me || !authMode || authMode === "none");
  if (me) {
    $("tEmail").value = me.email;
    $("tEmail").disabled = true;
    loadMine();
    const out = $("aOut");
    if (out) out.addEventListener("click", async (e) => {
      e.preventDefault();
      await fetch("/auth/logout", { method: "POST" });
      me = null; $("tEmail").disabled = false; $("tEmail").value = "";
      renderAccount();
    });
  }
}

async function loadMine() {
  const r = await api("my_tickets");
  const rows = r.ok ? r.result.tickets || [] : [];
  $("mineOut").innerHTML = rows.length
    ? rows.map((t) => `<div class="kb-hit" style="cursor:pointer" data-tid="${esc(t.ticket_id)}">
        <div class="row" style="gap:8px"><strong>${esc(t.subject)}</strong> ${statusPill(t.status)}</div>
        <div class="muted" style="font-size:11px">${esc(t.ticket_id)} · opened ${esc((t.created_at || "").slice(0, 16).replace("T", " "))}</div>
      </div>`).join("")
    : `<span class="muted">No tickets yet — open one below.</span>`;
  for (const el of $("mineOut").querySelectorAll("[data-tid]")) {
    el.addEventListener("click", () => {
      $("sId").value = el.dataset.tid; $("sEmail").value = me.email;
      $("sBtn").click(); $("sId").scrollIntoView({ behavior: "smooth" });
    });
  }
}

let authMode = null;
async function initAuth() {
  const [conf, who] = await Promise.all([api("auth_config"), api("whoami")]);
  authMode = conf.ok ? conf.result.mode : "none";
  if (who.ok && !who.result.anonymous) me = who.result.identity;
  if (authMode === "oidc") {
    $("signinEmailStep").classList.add("hidden");
    $("oidcStep").classList.remove("hidden");
  }
  renderAccount();
}

$("aSend").addEventListener("click", async () => {
  const email = $("aEmail").value.trim();
  if (!email) return;
  $("aHint").textContent = "Sending…";
  const r = await api("request_code", { email });
  if (!r.ok || r.result?.error) { $("aHint").textContent = r.result?.error || r.error; return; }
  $("aHint").textContent = `Code sent to ${email} (valid ${r.result.expires_in_minutes} min).`;
  $("signinCodeStep").classList.remove("hidden");
  // SEND_MODE=test surfaces the code so the sample demos without a mail provider.
  if (r.result.demo_code) $("aCodeHint").textContent = `demo mode — your code is ${r.result.demo_code}`;
  $("aCode").focus();
});

$("aVerify").addEventListener("click", async () => {
  const r = await api("verify_code", { email: $("aEmail").value.trim(), code: $("aCode").value.trim() });
  if (!r.ok || r.result?.error) { $("aCodeHint").textContent = r.result?.error || r.error; return; }
  me = r.result.identity; // the session itself became an HttpOnly cookie server-side
  $("signinCodeStep").classList.add("hidden");
  renderAccount();
});

// ---------------------------------------------------------------- KB search
function renderHits(el, hits) {
  el.innerHTML = hits.length
    ? hits.map((h) => `<div class="kb-hit"><div>${esc((h.text || "").slice(0, 260))}</div>
        ${h.score != null ? `<div class="muted" style="font-size:11px;margin-top:4px">relevance ${Number(h.score).toFixed(2)}</div>` : ""}</div>`).join("")
    : `<span class="muted">No matching articles.</span>`;
}

/** One search_kb call; retries while empty (cold collection / still indexing). */
async function kbSearch(query, { tries = 1, delayMs = 1500 } = {}) {
  for (let i = 0; i < tries; i++) {
    const r = await api("search_kb", { query, top_k: 4 });
    const hits = r.ok ? r.result.results || [] : [];
    if (hits.length || i === tries - 1) return hits;
    await new Promise((ok) => setTimeout(ok, delayMs * (i + 1)));
  }
  return [];
}

$("kbBtn").addEventListener("click", async () => {
  const q = $("kbQ").value.trim();
  if (!q) return;
  $("kbOut").innerHTML = `<span class="muted">Searching…</span>`;
  renderHits($("kbOut"), await kbSearch(q));
});

// Deflection: suggest articles while the customer is still typing the ticket.
let deflectTimer = 0, deflectSeq = 0;
function deflectSoon() {
  clearTimeout(deflectTimer);
  deflectTimer = setTimeout(async () => {
    const text = `${$("tSubject").value} ${$("tBody").value}`.trim();
    if (text.length < 8) return;
    const seq = ++deflectSeq;
    const hits = await kbSearch(text);
    if (seq !== deflectSeq || !hits.length) return; // stale or nothing useful
    $("deflect").classList.remove("hidden");
    renderHits($("deflectOut"), hits);
  }, 500);
}
$("tSubject").addEventListener("input", deflectSoon);
$("tBody").addEventListener("input", deflectSoon);

// ---------------------------------------------------------------- submit (instant)
$("tSend").addEventListener("click", async () => {
  const subject = $("tSubject").value.trim(), body = $("tBody").value.trim(),
        email = $("tEmail").value.trim();
  if (!subject || !body || !email) { $("tHint").textContent = "Subject, description and email are required."; return; }
  const btn = $("tSend"); btn.disabled = true; $("tHint").textContent = "";
  const t0 = performance.now();
  const r = await api("submit_ticket", { subject, body, requester_email: email, channel: "web" });
  btn.disabled = false;
  const box = $("tConfirm"); box.classList.remove("hidden");
  if (!r.ok || !r.result?.ticket_id) {
    box.innerHTML = `<span class="err">${esc(r.error || "submission failed")}</span>`;
    return;
  }
  box.innerHTML = `✅ Ticket filed in ${Math.round(performance.now() - t0)} ms — keep this id:
    <div class="tid">${esc(r.result.ticket_id)}</div>
    <div class="muted" style="font-size:12px">${r.result.verified
      ? "Filed on your verified account — it's in “My tickets” above."
      : "Check progress below with this id and your email."}</div>`;
  $("sId").value = r.result.ticket_id; $("sEmail").value = email;
  if (me) loadMine();

  // Separate, best-effort call: suggested articles for what was just filed —
  // polls a few times in case the KB is still embedding.
  $("deflect").classList.remove("hidden");
  $("deflectOut").innerHTML = `<span class="muted">Looking for related articles…</span>`;
  renderHits($("deflectOut"), await kbSearch(`${subject}\n${body}`, { tries: 3 }));
});

// ---------------------------------------------------------------- status
$("sBtn").addEventListener("click", async () => {
  const out = $("sOut"); out.innerHTML = `<span class="muted">…</span>`;
  const r = await api("ticket_status", {
    ticket_id: $("sId").value.trim(), requester_email: $("sEmail").value.trim(),
  });
  if (!r.ok || r.result?.error) { out.innerHTML = `<span class="err">${esc(r.result?.error || r.error)}</span>`; return; }
  const t = r.result.ticket, msgs = r.result.messages || [];
  out.innerHTML = `
    <div class="row" style="gap:10px"><strong>${esc(t.subject)}</strong> ${statusPill(t.status)}</div>
    <div class="muted" style="font-size:12px">opened ${esc((t.created_at || "").slice(0, 16).replace("T", " "))}
      · last update ${esc((t.updated_at || "").slice(0, 16).replace("T", " "))}</div>
    <div class="timeline">${msgs.map((m) => `<div class="msg"><div class="who">${esc(m.role)}</div>${esc(m.snippet)}</div>`).join("")}</div>`;
  // Signed-in owners can reply right here (reply_ticket enforces ownership).
  $("replyBox").classList.toggle("hidden", !(me && me.email === $("sEmail").value.trim().toLowerCase()));
});

$("rSend").addEventListener("click", async () => {
  const body = $("rBody").value.trim();
  if (!body) return;
  $("rHint").textContent = "Sending…";
  const r = await api("reply_ticket", { ticket_id: $("sId").value.trim(), body });
  if (!r.ok || r.result?.error) { $("rHint").textContent = r.result?.error || r.error; return; }
  $("rHint").textContent = r.result.reopened ? "Sent — ticket reopened." : "Sent.";
  $("rBody").value = "";
  $("sBtn").click(); // refresh the timeline
});

initAuth();
