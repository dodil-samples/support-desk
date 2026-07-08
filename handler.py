"""Support Desk — one Ignite (Python) function, action-routed.

A Zendesk-style shared inbox where K3 is the whole backend: tickets + messages in
the SQL warehouse, full bodies + KB articles as objects, and a vector collection for
suggested answers. Ignite Models does triage and reply drafting.

Invoke with an `action`; e.g.
  {"action":"create_ticket","subject":"...","body":"...","requester_email":"..."}
  {"action":"suggest_reply","ticket_id":"..."}
  {"action":"stats"}
"""

from __future__ import annotations

import csv
import datetime as _dt
import difflib
import io
import json
import os
import re
import statistics
import sys
import uuid

# Make the app's own modules importable regardless of the launcher's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import gate, models
from lib.k3 import K3Error
import bootstrap


# --------------------------------------------------------------------------- utils
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _slug(text: str, fallback: str = "item") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or fallback)[:48]


def _sql_str(v) -> str:
    """Single-quote-escape a value for inline SQL (ids/enums only, not free text)."""
    return "'" + str(v).replace("'", "''") + "'"


# First-response SLA targets (hours) per priority — Zendesk-style.
SLA_FIRST_RESPONSE_HOURS = {"urgent": 1, "high": 4, "normal": 8, "low": 24}


def _parse_ts(s: str | None) -> _dt.datetime | None:
    # The warehouse hands back timestamps in two shapes: values written by our SQL
    # UPDATEs keep the ISO "2026-07-07T16:05:27Z" form, but values stored via row
    # INSERT come back normalized to "2026-07-07 16:04:58" (space, no Z). Accept both.
    if not s:
        return None
    norm = str(s).strip().replace("T", " ").rstrip("Z").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(norm, fmt).replace(tzinfo=_dt.timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile — robust for any n>=1 (statistics.quantiles
    needs n>=2 and picks a method; this is simpler for small support-desk samples)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _esc(v) -> str:
    return str(v).replace("'", "''")


TRIAGE_SYSTEM = (
    "You are a support-desk triage assistant. Classify the ticket and return ONLY "
    "compact JSON with keys: category (billing|bug|how_to|account|feature_request|other), "
    "priority (low|normal|high|urgent), sentiment (positive|neutral|negative), "
    "tags (array of <=4 short lowercase strings)."
)


def _triage(subject: str, body: str) -> dict:
    out = models.chat_json(TRIAGE_SYSTEM, f"Subject: {subject}\n\nBody: {body}") or {}
    return {
        "category": out.get("category", "other"),
        "priority": out.get("priority", "normal"),
        "sentiment": out.get("sentiment", "neutral"),
        "tags": out.get("tags") or [],
    }


def _store_message(k3, ticket_id: str, author: str, role: str, body: str) -> dict:
    mid = "m_" + uuid.uuid4().hex[:12]
    key = f"tickets/{ticket_id}/messages/{mid}.txt"
    k3.put_object(key, body)
    row = {
        "message_id": mid, "ticket_id": ticket_id, "author": author, "role": role,
        "body_key": key, "snippet": body.strip().replace("\n", " ")[:200], "created_at": _now(),
    }
    k3.upsert("messages", [row])
    return row


# ------------------------------------------------------------------------- actions
def create_ticket(k3, p: dict) -> dict:
    subject = p.get("subject", "(no subject)")
    body = p.get("body", "")
    tid = "t_" + uuid.uuid4().hex[:12]
    tri = _triage(subject, body)
    row = {
        "ticket_id": tid, "subject": subject, "requester_email": p.get("requester_email", ""),
        "channel": p.get("channel", "web"), "status": "new",
        "priority": tri["priority"], "category": tri["category"], "sentiment": tri["sentiment"],
        "assignee": "", "csat": None, "created_at": _now(), "updated_at": _now(),
        "first_response_at": "", "solved_at": "", "tags_json": json.dumps(tri["tags"]),
    }
    k3.upsert("tickets", [row])
    msg = _store_message(k3, tid, p.get("requester_email", "customer"), "customer", body)
    suggestions = _kb_hits(k3, f"{subject}\n{body}")
    return {"ticket_id": tid, "triage": tri, "first_message_id": msg["message_id"],
            "suggested_kb": suggestions, "possible_duplicates": _possible_duplicates(k3, subject)}


def submit_ticket(k3, p: dict) -> dict:
    """PUBLIC: a customer opens a ticket from a website form / CRM inbound email.

    Same pipeline as create_ticket (triage + store + KB suggestions) but returns
    only the customer-safe fields — no cross-ticket duplicate hints, no internal
    triage tags — plus a status token (ticket_id + email) to check it later.
    """
    res = create_ticket(k3, p)
    return {
        "ticket_id": res["ticket_id"],
        "status": "new",
        "message": "Thanks — your request was received. Use ticket_status with your "
                   "ticket_id and email to check progress.",
        "suggested_help": res.get("suggested_kb", []),
    }


def ticket_status(k3, p: dict) -> dict:
    """PUBLIC: a customer checks their own ticket by id + the email they used.

    The email must match the ticket's requester_email — so a public caller can
    only ever see the one ticket they opened, never anyone else's.
    """
    tid = p.get("ticket_id", "")
    email = str(p.get("requester_email", "")).strip().lower()
    t = _one(k3, "SELECT ticket_id, subject, status, priority, category, requester_email, "
                 f"created_at, updated_at, solved_at FROM tickets WHERE ticket_id={_sql_str(tid)}")
    if not t or str(t.get("requester_email", "")).strip().lower() != email or not email:
        return {"error": "no ticket found for that id + email"}
    msgs = k3.execute(
        f"SELECT role, snippet, created_at FROM messages WHERE ticket_id={_sql_str(tid)} "
        f"ORDER BY created_at"
    )
    t.pop("requester_email", None)  # don't echo PII back
    return {"ticket": t, "messages": msgs}


def add_message(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    role = p.get("role", "agent")
    msg = _store_message(k3, tid, p.get("author", role), role, p.get("body", ""))
    # First agent reply sets first_response_at; any message bumps updated_at + status.
    sets = [f"updated_at={_sql_str(_now())}"]
    if role == "agent":
        sets.append(f"status='open'")
        sets.append(
            "first_response_at=CASE WHEN first_response_at='' OR first_response_at IS NULL "
            f"THEN {_sql_str(_now())} ELSE first_response_at END"
        )
    k3.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE ticket_id={_sql_str(tid)}")
    return {"message_id": msg["message_id"], "ticket_id": tid}


def triage(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    t = _one(k3, f"SELECT subject, requester_email FROM tickets WHERE ticket_id={_sql_str(tid)}")
    body = _latest_customer_body(k3, tid)
    tri = _triage(t.get("subject", ""), body)
    k3.execute(
        f"UPDATE tickets SET category={_sql_str(tri['category'])}, "
        f"priority={_sql_str(tri['priority'])}, sentiment={_sql_str(tri['sentiment'])}, "
        f"tags_json={_sql_str(json.dumps(tri['tags']))}, updated_at={_sql_str(_now())} "
        f"WHERE ticket_id={_sql_str(tid)}"
    )
    return {"ticket_id": tid, "triage": tri}


def suggest_reply(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    t = _one(k3, f"SELECT subject FROM tickets WHERE ticket_id={_sql_str(tid)}")
    body = _latest_customer_body(k3, tid)
    hits = _kb_hits(k3, f"{t.get('subject','')}\n{body}")
    kb_context = "\n\n".join(f"[KB] {h['text'][:600]}" for h in hits) or "(no KB articles matched)"
    draft = models.chat([
        {"role": "system", "content":
            "You are a helpful support agent. Draft a concise, friendly reply that "
            "resolves the customer's issue. Ground it in the provided KB context; if "
            "the KB doesn't cover it, say what info you need. Do not invent policy."},
        {"role": "user", "content":
            f"Customer subject: {t.get('subject','')}\nCustomer message: {body}\n\n"
            f"Knowledge base context:\n{kb_context}"},
    ])
    return {"ticket_id": tid, "draft_reply": draft, "kb_used": hits}


def search_kb(k3, p: dict) -> dict:
    return {"query": p.get("query", ""),
            "results": k3.vector_search(p.get("query", ""), top_k=int(p.get("top_k", 5)))}


def add_kb(k3, p: dict) -> dict:
    title = p.get("title", "Untitled")
    key = f"kb/{_slug(title, 'article')}.md"
    k3.put_object(key, f"# {title}\n\n{p.get('body','')}", content_type="text/markdown")
    k3.trigger_ingest()  # index now rather than waiting for the periodic sync
    return {"kb_key": key, "note": "K3 is embedding it for suggested answers."}


def get_ticket(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    ticket = _one(k3, f"SELECT * FROM tickets WHERE ticket_id={_sql_str(tid)}")
    msgs = k3.execute(
        f"SELECT message_id, author, role, snippet, created_at FROM messages "
        f"WHERE ticket_id={_sql_str(tid)} ORDER BY created_at"
    )
    return {"ticket": ticket, "messages": msgs}


def list_tickets(k3, p: dict) -> dict:
    where = []
    for f in ("status", "category", "assignee", "priority"):
        if p.get(f):
            where.append(f"{f}={_sql_str(p[f])}")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = int(p.get("limit", 50))
    rows = k3.execute(
        f"SELECT ticket_id, subject, status, priority, category, sentiment, assignee, "
        f"requester_email, created_at FROM tickets{clause} "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )
    return {"count": len(rows), "tickets": rows}


def set_status(k3, p: dict) -> dict:
    tid, status = p["ticket_id"], p["status"]
    extra = f", solved_at={_sql_str(_now())}" if status == "solved" else ""
    k3.execute(f"UPDATE tickets SET status={_sql_str(status)}, updated_at={_sql_str(_now())}"
               f"{extra} WHERE ticket_id={_sql_str(tid)}")
    return {"ticket_id": tid, "status": status}


def assign(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    k3.execute(f"UPDATE tickets SET assignee={_sql_str(p['assignee'])}, "
               f"status='open', updated_at={_sql_str(_now())} WHERE ticket_id={_sql_str(tid)}")
    return {"ticket_id": tid, "assignee": p["assignee"]}


def rate(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    k3.execute(f"UPDATE tickets SET csat={int(p['csat'])}, updated_at={_sql_str(_now())} "
               f"WHERE ticket_id={_sql_str(tid)}")
    return {"ticket_id": tid, "csat": int(p["csat"])}


def stats(k3, p: dict) -> dict:
    open_by_priority = k3.execute(
        "SELECT priority, COUNT(*) AS n FROM tickets WHERE status <> 'solved' "
        "GROUP BY priority ORDER BY n DESC")
    by_category = k3.execute(
        "SELECT category, COUNT(*) AS n FROM tickets GROUP BY category ORDER BY n DESC LIMIT 10")
    volume = k3.execute(
        "SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM tickets "
        "GROUP BY day ORDER BY day DESC LIMIT 14")
    return {"open_by_priority": open_by_priority, "by_category": by_category,
            "volume_by_day": volume, "csat": _csat_stats(k3),
            "first_response_minutes": _response_time_stats(k3), "sla": _sla_report(k3)}


def _csat_stats(k3) -> dict:
    """CSAT distribution via the statistics module (mean/median/stdev), not just AVG()."""
    vals = [int(r["csat"]) for r in k3.execute("SELECT csat FROM tickets WHERE csat IS NOT NULL")
            if r.get("csat") is not None]
    if not vals:
        return {"rated": 0}
    return {"rated": len(vals), "mean": round(statistics.mean(vals), 2),
            "median": statistics.median(vals),
            "stdev": round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0}


def _response_time_stats(k3) -> dict:
    """First-response latency distribution: parse the timestamps and summarise with
    statistics.mean/median plus interpolated p90 — the metric a support lead lives by."""
    rows = k3.execute("SELECT created_at, first_response_at FROM tickets "
                      "WHERE first_response_at <> '' AND first_response_at IS NOT NULL")
    mins: list[float] = []
    for r in rows:
        c, f = _parse_ts(r.get("created_at")), _parse_ts(r.get("first_response_at"))
        if c and f and f >= c:
            mins.append((f - c).total_seconds() / 60.0)
    if not mins:
        return {"answered": 0}
    mins.sort()
    return {"answered": len(mins), "mean": round(statistics.mean(mins), 1),
            "median": round(statistics.median(mins), 1),
            "p90": round(_percentile(mins, 90), 1), "max": round(mins[-1], 1)}


def _sla_report(k3) -> dict:
    """Flag open, still-unanswered tickets that have breached (or are nearing) their
    per-priority first-response SLA — computed in Python with datetime deltas."""
    rows = k3.execute("SELECT priority, created_at FROM tickets WHERE status <> 'solved' "
                      "AND (first_response_at='' OR first_response_at IS NULL)")
    now = _dt.datetime.now(_dt.timezone.utc)
    breaching, at_risk = 0, 0
    for r in rows:
        created = _parse_ts(r.get("created_at"))
        if not created:
            continue
        target_h = SLA_FIRST_RESPONSE_HOURS.get(r.get("priority", "normal"), 8)
        age_h = (now - created).total_seconds() / 3600.0
        if age_h > target_h:
            breaching += 1
        elif age_h > 0.75 * target_h:
            at_risk += 1
    return {"targets_hours": SLA_FIRST_RESPONSE_HOURS, "open_unanswered": len(rows),
            "breaching": breaching, "at_risk": at_risk}


def export_tickets(k3, p: dict) -> dict:
    """Export tickets as CSV (csv + io.StringIO) — same filters as list_tickets."""
    where = []
    for f in ("status", "category", "assignee", "priority"):
        if p.get(f):
            where.append(f"{f}={_sql_str(p[f])}")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = int(p.get("limit", 500))
    cols = ["ticket_id", "subject", "status", "priority", "category", "sentiment", "assignee",
            "requester_email", "csat", "created_at", "updated_at", "first_response_at", "solved_at"]
    rows = k3.execute(f"SELECT {', '.join(cols)} FROM tickets{clause} "
                      f"ORDER BY created_at DESC LIMIT {limit}")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    return {"format": "csv", "row_count": len(rows), "columns": cols, "csv": buf.getvalue()}


# ------------------------------------------------------------------------ helpers 2
def _kb_hits(k3, text: str, top_k: int = 3) -> list[dict]:
    try:
        return k3.vector_search(text, top_k=top_k)
    except Exception:
        return []


def _possible_duplicates(k3, subject: str, limit: int = 5) -> list[dict]:
    """Fuzzy-match the new subject against recent open tickets (difflib) so an agent
    sees likely duplicates at creation time — no embeddings needed, works instantly."""
    if not subject:
        return []
    recent = k3.execute("SELECT ticket_id, subject FROM tickets WHERE status <> 'solved' "
                        "ORDER BY created_at DESC LIMIT 200")
    by_subject = {r["subject"]: r["ticket_id"] for r in recent if r.get("subject")}
    matches = difflib.get_close_matches(subject, list(by_subject.keys()), n=limit, cutoff=0.6)
    return [{"ticket_id": by_subject[m], "subject": m} for m in matches]


def _one(k3, sql: str) -> dict:
    rows = k3.execute(sql)
    return rows[0] if rows else {}


def _latest_customer_body(k3, tid: str) -> str:
    row = _one(k3, f"SELECT body_key FROM messages WHERE ticket_id={_sql_str(tid)} "
                   f"AND role='customer' ORDER BY created_at DESC LIMIT 1")
    if not row.get("body_key"):
        return ""
    try:
        return k3.get_object(row["body_key"]).decode("utf-8", "replace")
    except Exception:
        return ""


ACTIONS = {
    # -- PUBLIC (anon-safe: customer-facing) --
    "submit_ticket": submit_ticket, "ticket_status": ticket_status, "search_kb": search_kb,
    # -- PRIVATE (admin key: the agent inbox) --
    "create_ticket": create_ticket, "add_message": add_message, "triage": triage,
    "suggest_reply": suggest_reply, "add_kb": add_kb,
    "get_ticket": get_ticket, "list_tickets": list_tickets, "set_status": set_status,
    "assign": assign, "rate": rate, "stats": stats, "export_tickets": export_tickets,
    "create_key": gate.create_key, "list_keys": gate.list_keys, "revoke_key": gate.revoke_key,
}


# ------------------------------------------------------------------------- entrypoint
def handler(event, context):
    # event may arrive as a dict (decoded) or raw JSON bytes/str.
    if isinstance(event, (bytes, bytearray)):
        event = json.loads(event or b"{}")
    elif isinstance(event, str):
        event = json.loads(event or "{}")
    event = event or {}

    action = event.get("action")
    fn = ACTIONS.get(action)
    if not fn:
        return {"error": f"unknown action {action!r}", "actions": sorted(ACTIONS)}

    try:
        k3 = bootstrap.ensure()
        # Public/private gate: PUBLIC actions are anon-safe (optionally project-keyed),
        # PRIVATE actions need an admin key. Unconfigured tiers stay open (see gate.py).
        decision = gate.authorize(k3, action, event)
        if not decision["ok"]:
            return {"ok": False, "action": action, "error": decision["error"], "code": 401}
        return {"ok": True, "action": action, "result": fn(k3, event)}
    except K3Error as e:
        return {"ok": False, "action": action, "error": f"k3: {e}"}
    except Exception as e:  # noqa: BLE001 — surface a clean error to the caller
        return {"ok": False, "action": action, "error": f"{type(e).__name__}: {e}"}
