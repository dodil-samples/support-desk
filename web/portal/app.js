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
    <div class="muted" style="font-size:12px">Check progress below with this id and your email.</div>`;
  $("sId").value = r.result.ticket_id; $("sEmail").value = email;

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
});
