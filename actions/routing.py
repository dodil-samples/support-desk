"""The routing engine: rules decide, AI refines, AI agents work, humans catch.

Mirrors the Zendesk/Freshdesk pipeline — deterministic RULES are the guardrails
and AI is judgment *inside* them, never above them:

  1. Ordered `routing_rules` rows (rules-as-data, like the CRM sample's flows)
     match the triaged ticket per event: created | customer_reply | escalation.
     First match wins. A rule either names an agent (`assign_to`) or defines a
     pool (skill filter + whether AI agents are eligible).
  2. Within a pool: ROUTING=rules picks round-robin (longest since last
     assignment — Zendesk's semantics); ROUTING=ai asks the model to pick the
     best-fit agent from THAT POOL ONLY (skills + current open load), falling
     back to round-robin. ROUTING=off disables the engine.
  3. If the assignee is an AI agent it works the ticket: KB-grounded draft +
     self-assessed confidence. Confident → posts the reply (a real agent
     message; sets first_response_at, status pending). Not confident → it
     REROUTES: fires the `escalation` event, humans only.
  4. Loop safety (hard rules, not configurable): an AI agent gets ONE
     auto-touch per ticket; escalation pools never include AI agents; a
     customer reply on an AI-assigned ticket escalates to humans.

Every decision lands in `routing_log` (decided_by = rule:<id> | ai:<agent> |
manual:<who>, with a reason) — agents can always trace WHY a ticket is theirs.
Assignment of a human notifies them through the mail seam (lib/mailer.py).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from lib import agents, mailer, models

from . import kb
from .common import latest_customer_body, now, one, sql_str

ROUTING = os.getenv("ROUTING", "rules").strip().lower()  # off | rules | ai

AI_PICK_SYSTEM = (
    "You pick the best support agent for a ticket. Return ONLY compact JSON: "
    '{"agent_id": "<one of the offered ids>", "reason": "<short>"} . '
    "Prefer skill fit, then lowest open load."
)

AI_WORK_SYSTEM = (
    "You are support agent {name}. Draft a reply to the customer grounded ONLY in "
    "the provided KB context, and honestly self-assess. Return ONLY compact JSON: "
    '{"confident": true|false, "reply": "<the reply text>", "reason": "<why (not) confident>"} . '
    "Set confident=false when the KB does not actually cover the customer's issue."
)


# ------------------------------------------------------------------------- log + notify
def _log(k3, ticket_id: str, event: str, decided_by: str, assign_to: str, reason: str) -> None:
    k3.upsert("routing_log", [{
        "log_id": "r_" + uuid.uuid4().hex[:12], "ticket_id": ticket_id, "event": event,
        "decided_by": decided_by, "assign_to": assign_to, "reason": reason[:300], "ts": now(),
    }])


def _notify(agent: dict, ticket: dict) -> None:
    if agent.get("kind") != "human" or not agent.get("email"):
        return
    try:
        mailer.send_email(
            agent["email"], f"Ticket assigned: {ticket.get('subject', '')[:80]}",
            f"Ticket {ticket.get('ticket_id')} ({ticket.get('priority')}/{ticket.get('category')}) "
            f"was routed to you.")
    except Exception:
        pass  # notification is best-effort, never blocks routing


# ------------------------------------------------------------------------- rule matching
def _rules(k3, event: str) -> list[dict]:
    try:
        rows = k3.execute(
            f"SELECT * FROM routing_rules WHERE enabled=1 AND on_event={sql_str(event)} "
            f"ORDER BY position")
        return rows
    except Exception:
        return []


def _matches(rule: dict, ticket: dict) -> bool:
    for field in ("category", "priority", "channel"):
        want = str(rule.get(field) or "").strip()
        if want and want != str(ticket.get(field) or ""):
            return False
    return True


def _open_counts(k3) -> dict[str, int]:
    try:
        rows = k3.execute("SELECT assignee, COUNT(*) AS n FROM tickets "
                          "WHERE status <> 'solved' AND assignee <> '' GROUP BY assignee")
        return {str(r.get("assignee")): int(r.get("n", 0)) for r in rows}
    except Exception:
        return {}


def _round_robin(pool: list[dict]) -> dict:
    return min(pool, key=lambda a: str(a.get("last_assigned_at") or ""))


def _ai_pick(k3, ticket: dict, pool: list[dict]) -> tuple[dict, str] | None:
    """Model picks within the rule's pool — constrained choice, RR fallback."""
    loads = _open_counts(k3)
    offer = [{"agent_id": a["agent_id"], "kind": a["kind"], "skills": a.get("skills") or [],
              "open_tickets": loads.get(a["agent_id"], 0)} for a in pool]
    out = models.chat_json(AI_PICK_SYSTEM, json.dumps({
        "ticket": {k: ticket.get(k) for k in ("subject", "category", "priority", "sentiment")},
        "agents": offer})) or {}
    chosen = next((a for a in pool if a["agent_id"] == out.get("agent_id")), None)
    if chosen:
        return chosen, str(out.get("reason") or "model pick")
    return None


# --------------------------------------------------------------------------- the engine
def route_ticket(k3, ticket_id: str, event: str = "created") -> dict:
    """Run the engine for one ticket+event. Returns what happened (also logged)."""
    if ROUTING == "off":
        return {"routed": False, "reason": "ROUTING=off"}
    ticket = one(k3, f"SELECT * FROM tickets WHERE ticket_id={sql_str(ticket_id)}")
    if not ticket:
        return {"routed": False, "reason": "no such ticket"}

    rule = next((r for r in _rules(k3, event) if _matches(r, ticket)), None)
    if not rule:
        _log(k3, ticket_id, event, "engine", "", "no rule matched — left unassigned")
        return {"routed": False, "reason": "no rule matched"}
    rid = f"rule:{rule.get('rule_id')}"

    # Escalations are humans-only, ALWAYS (loop safety) — regardless of the rule.
    allow_ai = bool(rule.get("allow_ai")) and event != "escalation"

    if rule.get("assign_to"):
        chosen = agents.get(k3, str(rule["assign_to"]))
        if not chosen or not chosen.get("active") or (chosen["kind"] == "ai" and not allow_ai):
            chosen = None
            pool = agents.active(k3, kind="human", skill=str(rule.get("pool_skill") or "") or None)
            decided_by, reason = rid, "named agent unavailable — fell back to human pool"
        else:
            pool, decided_by, reason = [chosen], rid, "rule names this agent"
    else:
        pool = agents.active(k3, skill=str(rule.get("pool_skill") or "") or None)
        if not allow_ai:
            pool = [a for a in pool if a.get("kind") == "human"]
        chosen, decided_by, reason = None, rid, "pool round-robin"

    if not pool:
        _log(k3, ticket_id, event, rid, "", "rule matched but pool is empty — unassigned")
        return {"routed": False, "reason": "empty pool", "rule": rule.get("rule_id")}

    if not chosen:
        if ROUTING == "ai" and len(pool) > 1:
            picked = None
            try:
                picked = _ai_pick(k3, ticket, pool)
            except Exception:
                picked = None
            if picked:
                chosen, reason = picked[0], picked[1]
                decided_by = f"ai-pick({rule.get('rule_id')})"
        if not chosen:
            chosen = _round_robin(pool)

    aid = chosen["agent_id"]
    # Write-then-verify: concurrent UPDATEs to the same tickets row (e.g. the
    # customer replying while the background chain is mid-flight) are
    # last-writer-wins on the FULL row in the warehouse — a stale-snapshot
    # commit can silently erase this assignment. Read it back and retry once.
    for attempt in (1, 2):
        k3.execute(f"UPDATE tickets SET assignee={sql_str(aid)}, updated_at={sql_str(now())} "
                   f"WHERE ticket_id={sql_str(ticket_id)}")
        if one(k3, f"SELECT assignee FROM tickets WHERE ticket_id={sql_str(ticket_id)}"
               ).get("assignee") == aid:
            break
        time.sleep(1)  # let the competing write commit, then take the last word
    k3.execute(f"UPDATE agents SET last_assigned_at={sql_str(now())} "
               f"WHERE agent_id={sql_str(aid)}")
    agents.invalidate()
    _log(k3, ticket_id, event, decided_by, aid, reason)
    _notify(chosen, {**ticket, "ticket_id": ticket_id})

    if chosen.get("kind") == "ai":
        _ai_work(k3, ticket_id, chosen)
    return {"routed": True, "assignee": aid, "kind": chosen.get("kind"),
            "rule": rule.get("rule_id"), "decided_by": decided_by}


# ----------------------------------------------------------------------- the AI worker
def _ai_touches(k3, ticket_id: str, agent_id: str) -> int:
    rows = k3.execute(f"SELECT COUNT(*) AS n FROM messages WHERE ticket_id={sql_str(ticket_id)} "
                      f"AND role='agent' AND author={sql_str(agent_id)}")
    return int(rows[0].get("n", 0)) if rows else 0


def _ai_work(k3, ticket_id: str, agent: dict) -> None:
    """An AI agent answers its ticket — or reroutes it to humans with a reason."""
    aid = agent["agent_id"]
    if _ai_touches(k3, ticket_id, aid) >= 1:  # hard cap: one auto-touch per ticket
        escalate(k3, ticket_id, f"ai:{aid}", "auto-touch cap reached")
        return
    ticket = one(k3, f"SELECT subject, category FROM tickets WHERE ticket_id={sql_str(ticket_id)}")
    body = latest_customer_body(k3, ticket_id)
    hits = kb.kb_hits(k3, f"{ticket.get('subject','')}\n{body}")
    kb_context = "\n\n".join(f"[KB] {h['text'][:600]}" for h in hits) or "(no KB articles matched)"
    try:
        out = models.chat_json(
            AI_WORK_SYSTEM.replace("{name}", agent.get("name") or aid),
            f"Customer subject: {ticket.get('subject','')}\nCustomer message: {body}\n\n"
            f"Knowledge base context:\n{kb_context}",
            model=agent.get("model") or None) or {}
    except Exception as e:
        escalate(k3, ticket_id, f"ai:{aid}", f"model error: {type(e).__name__}")
        return
    # confidence_threshold: 0 = trust the model's self-assessment (default);
    # 1 = never confident, always escalate (useful to demo the escalation path).
    threshold = float(agent.get("confidence_threshold") or 0)
    confident = bool(out.get("confident")) and bool(str(out.get("reply") or "").strip())
    if not confident or threshold >= 1:
        escalate(k3, ticket_id, f"ai:{aid}", str(out.get("reason") or "not confident"))
        return
    from .tickets import agent_reply  # late import (tickets imports triage → routing)
    agent_reply(k3, ticket_id, author=aid, body=str(out["reply"]), to_status="pending")
    _log(k3, ticket_id, "ai_work", f"ai:{aid}", aid,
         f"answered from KB: {str(out.get('reason') or '')}")


def escalate(k3, ticket_id: str, decided_by: str, why: str) -> dict:
    """AI (or a cap) hands the ticket to humans — the `escalation` rules decide who."""
    _log(k3, ticket_id, "escalation", decided_by, "", f"escalating: {why}")
    return route_ticket(k3, ticket_id, event="escalation")


def on_customer_reply(k3, ticket_id: str) -> None:
    """Loop safety: a customer reply on an AI-assigned ticket goes to humans.
    Runs in the background — the customer's reply call never waits on routing."""
    def work():
        try:
            t = one(k3, f"SELECT assignee FROM tickets WHERE ticket_id={sql_str(ticket_id)}")
            a = agents.get(k3, str(t.get("assignee") or ""))
            if a and a.get("kind") == "ai":
                escalate(k3, ticket_id, f"ai:{a['agent_id']}", "customer replied — handing to a human")
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()
