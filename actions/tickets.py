"""Ticket lifecycle: create/submit, customer self-service, agent work, export."""

from __future__ import annotations

import csv
import difflib
import io
import json
import uuid

from lib import identity

from . import kb, routing, triage
from .common import STATUSES, latest_customer_body, now, one, sql_str, store_message


def _new_ticket_row(tid: str, p: dict, tri: dict) -> dict:
    return {
        "ticket_id": tid, "subject": p.get("subject", "(no subject)"),
        "requester_email": p.get("requester_email", ""),
        "channel": p.get("channel", "web"), "status": "new",
        "priority": tri["priority"], "category": tri["category"], "sentiment": tri["sentiment"],
        "assignee": "", "csat": None, "created_at": now(), "updated_at": now(),
        "first_response_at": "", "solved_at": "", "tags_json": json.dumps(tri["tags"]),
    }


def create_ticket(k3, p: dict) -> dict:
    """PRIVATE: an agent files a ticket and wants the full picture back in one
    round-trip — so triage AND routing run inline here (slower, but the caller
    is staff)."""
    subject = p.get("subject", "(no subject)")
    body = p.get("body", "")
    tid = "t_" + uuid.uuid4().hex[:12]
    tri = triage.classify(subject, body)
    k3.upsert("tickets", [_new_ticket_row(tid, p, tri)])
    msg = store_message(k3, tid, p.get("requester_email", "customer"), "customer", body)
    routed = routing.route_ticket(k3, tid, event="created")
    suggestions = kb.kb_hits(k3, f"{subject}\n{body}")
    return {"ticket_id": tid, "triage": tri, "first_message_id": msg["message_id"],
            "routing": routed, "suggested_kb": suggestions,
            "possible_duplicates": _possible_duplicates(k3, subject)}


def submit_ticket(k3, p: dict) -> dict:
    """PUBLIC: a customer opens a ticket from a website form / CRM inbound email.

    Instant by design: a pure warehouse write (ticket row + first message) that
    returns as soon as it's durable. The enrichment CHAIN — LLM triage, then the
    routing engine (rules → round-robin/AI pick → maybe an AI agent answers) —
    runs in the background and updates the row when it lands. KB suggestions are
    NOT bundled here; the portal fires `search_kb` as its own call.

    Signed-in callers (any AUTH_MODE adapter) get their identity's email as the
    requester — proven, so the ticket counts as verified immediately.
    """
    ident = p.get("_identity")
    if ident and not p.get("requester_email"):
        p = {**p, "requester_email": ident["email"]}
    subject = p.get("subject", "(no subject)")
    body = p.get("body", "")
    tid = "t_" + uuid.uuid4().hex[:12]
    k3.upsert("tickets", [_new_ticket_row(tid, p, triage.TRIAGE_DEFAULTS)])
    store_message(k3, tid, p.get("requester_email", "customer"), "customer", body)
    triage.enrich_async(k3, tid, subject, body)
    verified = bool(ident and ident.get("verified")
                    and ident["email"] == str(p.get("requester_email", "")).strip().lower())
    if verified:
        identity.mark_verified(k3, ident["email"])
    return {
        "ticket_id": tid,
        "status": "new",
        "verified": verified,
        "message": "Thanks — your request was received. Use ticket_status with your "
                   "ticket_id and email to check progress.",
    }


def ticket_status(k3, p: dict) -> dict:
    """PUBLIC: a customer checks their own ticket by id + the email they used.

    The email must match the ticket's requester_email — so a public caller can
    only ever see the one ticket they opened, never anyone else's. A signed-in
    caller's identity email is used automatically (no need to retype it).
    """
    tid = p.get("ticket_id", "")
    ident = p.get("_identity")
    email = str(p.get("requester_email") or (ident or {}).get("email") or "").strip().lower()
    t = one(k3, "SELECT ticket_id, subject, status, priority, category, requester_email, "
                f"created_at, updated_at, solved_at FROM tickets WHERE ticket_id={sql_str(tid)}")
    if not t or str(t.get("requester_email", "")).strip().lower() != email or not email:
        return {"error": "no ticket found for that id + email"}
    msgs = k3.execute(
        f"SELECT role, snippet, created_at FROM messages WHERE ticket_id={sql_str(tid)} "
        f"ORDER BY created_at"
    )
    t.pop("requester_email", None)  # don't echo PII back
    return {"ticket": t, "messages": msgs}


def _require_identity(p: dict) -> dict | None:
    ident = p.get("_identity")
    if not ident or not ident.get("verified"):
        return None
    return ident


def my_tickets(k3, p: dict) -> dict:
    """PUBLIC + signed-in: every ticket belonging to the caller's PROVEN email.

    This listing is safe only because the email is verified by an identity
    adapter — the anonymous flow deliberately has no list-by-email (an address
    alone is guessable and would leak other people's ticket existence).
    """
    ident = _require_identity(p)
    if not ident:
        return {"error": "sign in to list your tickets (see auth_config)", "code": 401}
    esc = ident["email"].replace("'", "''")
    rows = k3.execute(
        f"SELECT ticket_id, subject, status, priority, category, created_at, updated_at "
        f"FROM tickets WHERE lower(requester_email)='{esc}' "
        f"ORDER BY created_at DESC LIMIT {int(p.get('limit', 50))}")
    return {"email": ident["email"], "count": len(rows), "tickets": rows}


def reply_ticket(k3, p: dict) -> dict:
    """PUBLIC + signed-in: the customer replies to THEIR OWN ticket from the portal.

    Ownership check against the proven identity email; a reply to a solved
    ticket reopens it, and a reply to an AI-assigned ticket escalates it to a
    human in the background (loop safety — see actions/routing.py).
    """
    ident = _require_identity(p)
    if not ident:
        return {"error": "sign in to reply to your ticket (see auth_config)", "code": 401}
    tid = str(p.get("ticket_id", ""))
    body = str(p.get("body", "")).strip()
    if not body:
        return {"error": "reply body is required"}
    t = one(k3, f"SELECT requester_email, status FROM tickets WHERE ticket_id={sql_str(tid)}")
    if not t or str(t.get("requester_email", "")).strip().lower() != ident["email"]:
        return {"error": "no ticket found for that id + your email"}
    msg = store_message(k3, tid, ident["email"], "customer", body)
    reopen = ", status='open'" if t.get("status") == "solved" else ""
    k3.execute(f"UPDATE tickets SET updated_at={sql_str(now())}{reopen} "
               f"WHERE ticket_id={sql_str(tid)}")
    routing.on_customer_reply(k3, tid)
    return {"ticket_id": tid, "message_id": msg["message_id"],
            "reopened": bool(reopen)}


def agent_reply(k3, tid: str, author: str, body: str, to_status: str = "open") -> dict:
    """One agent (human or AI) message + the bookkeeping every reply implies."""
    msg = store_message(k3, tid, author, "agent", body)
    k3.execute(
        f"UPDATE tickets SET status={sql_str(to_status)}, updated_at={sql_str(now())}, "
        "first_response_at=CASE WHEN first_response_at='' OR first_response_at IS NULL "
        f"THEN {sql_str(now())} ELSE first_response_at END "
        f"WHERE ticket_id={sql_str(tid)}")
    return msg


def add_message(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    role = p.get("role", "agent")
    if role == "agent":
        msg = agent_reply(k3, tid, p.get("author", "agent"), p.get("body", ""))
    else:
        msg = store_message(k3, tid, p.get("author", role), role, p.get("body", ""))
        k3.execute(f"UPDATE tickets SET updated_at={sql_str(now())} WHERE ticket_id={sql_str(tid)}")
        if role == "customer":
            routing.on_customer_reply(k3, tid)
    return {"message_id": msg["message_id"], "ticket_id": tid}


def retriage(k3, p: dict) -> dict:
    """PRIVATE `triage` action: (re)classify on demand — and re-route, since the
    classification the rules matched on may just have changed."""
    tid = p["ticket_id"]
    t = one(k3, f"SELECT subject FROM tickets WHERE ticket_id={sql_str(tid)}")
    tri = triage.classify(t.get("subject", ""), latest_customer_body(k3, tid))
    triage.apply(k3, tid, tri)
    routed = routing.route_ticket(k3, tid, event="created")
    return {"ticket_id": tid, "triage": tri, "routing": routed}


def get_ticket(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    ticket = one(k3, f"SELECT * FROM tickets WHERE ticket_id={sql_str(tid)}")
    if ticket:
        ticket["requester_verified"] = bool(
            identity.verified_emails(k3, [ticket.get("requester_email", "")]))
    msgs = k3.execute(
        f"SELECT message_id, author, role, snippet, created_at FROM messages "
        f"WHERE ticket_id={sql_str(tid)} ORDER BY created_at"
    )
    log = k3.execute(
        f"SELECT event, decided_by, assign_to, reason, ts FROM routing_log "
        f"WHERE ticket_id={sql_str(tid)} ORDER BY ts"
    )
    return {"ticket": ticket, "messages": msgs, "routing_log": log}


def list_tickets(k3, p: dict) -> dict:
    if p.get("status") and p["status"] not in STATUSES:
        return {"error": f"invalid status {p['status']!r} — expected one of {list(STATUSES)}"}
    where = []
    for f in ("status", "category", "assignee", "priority"):
        if p.get(f):
            where.append(f"{f}={sql_str(p[f])}")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = int(p.get("limit", 50))
    rows = k3.execute(
        f"SELECT ticket_id, subject, status, priority, category, sentiment, assignee, "
        f"requester_email, created_at FROM tickets{clause} "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )
    # Annotate each row with whether its requester proved their email — a spam
    # signal for agents. One IN query in Python, no JOIN (see bootstrap notes).
    proven = identity.verified_emails(k3, [r.get("requester_email", "") for r in rows])
    for r in rows:
        r["requester_verified"] = str(r.get("requester_email", "")).strip().lower() in proven
    return {"count": len(rows), "tickets": rows}


def set_status(k3, p: dict) -> dict:
    tid, status = p["ticket_id"], p["status"]
    if status not in STATUSES:
        return {"error": f"invalid status {status!r} — expected one of {list(STATUSES)}"}
    extra = f", solved_at={sql_str(now())}" if status == "solved" else ""
    k3.execute(f"UPDATE tickets SET status={sql_str(status)}, updated_at={sql_str(now())}"
               f"{extra} WHERE ticket_id={sql_str(tid)}")
    return {"ticket_id": tid, "status": status}


def assign(k3, p: dict) -> dict:
    """Manual assignment — logged in routing_log like every other decision."""
    tid = p["ticket_id"]
    who = str(p.get("_identity", {}).get("email") if p.get("_identity") else "") or "admin-key"
    k3.execute(f"UPDATE tickets SET assignee={sql_str(p['assignee'])}, "
               f"status='open', updated_at={sql_str(now())} WHERE ticket_id={sql_str(tid)}")
    routing._log(k3, tid, "manual", f"manual:{who}", p["assignee"], "assigned by hand")
    return {"ticket_id": tid, "assignee": p["assignee"]}


def rate(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    k3.execute(f"UPDATE tickets SET csat={int(p['csat'])}, updated_at={sql_str(now())} "
               f"WHERE ticket_id={sql_str(tid)}")
    return {"ticket_id": tid, "csat": int(p["csat"])}


def export_tickets(k3, p: dict) -> dict:
    """Export tickets as CSV (csv + io.StringIO) — same filters as list_tickets."""
    where = []
    for f in ("status", "category", "assignee", "priority"):
        if p.get(f):
            where.append(f"{f}={sql_str(p[f])}")
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
