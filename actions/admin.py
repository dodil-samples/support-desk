"""Desk administration: the staff registry (human + AI agents), routing rules,
and the reporting an admin lives by. Mutations here are ADMIN_ONLY_ACTIONS in
lib/gate.py — a plain agent can read, only admins (or the admin key) can change.
"""

from __future__ import annotations

import json
import statistics
import uuid

from lib import agents as registry

from .common import (SLA_FIRST_RESPONSE_HOURS, now, parse_ts, percentile, slug, sql_str)

AGENT_KINDS = ("human", "ai")
AGENT_ROLES = ("admin", "agent")


# ------------------------------------------------------------------- staff registry
def add_agent(k3, p: dict) -> dict:
    """Register a HUMAN (email + role) or an AI agent (name + skills).

    Human: agent_id IS the (lowercased) email — the identity seam looks their
    role up by it. AI: agent_id is a slug of the name; it never signs in.
    """
    kind = str(p.get("kind") or "human")
    if kind not in AGENT_KINDS:
        return {"error": f"invalid kind {kind!r} — expected one of {list(AGENT_KINDS)}"}
    if kind == "human":
        email = str(p.get("email") or "").strip().lower()
        if "@" not in email:
            return {"error": "a human agent needs a valid email"}
        role = str(p.get("role") or "agent")
        if role not in AGENT_ROLES:
            return {"error": f"invalid role {role!r} — expected one of {list(AGENT_ROLES)}"}
        agent_id = email
    else:
        agent_id = slug(str(p.get("name") or ""), "")
        if not agent_id:
            return {"error": "an AI agent needs a name"}
        email, role = "", ""
    row = {
        "agent_id": agent_id, "kind": kind, "email": email,
        "name": str(p.get("name") or agent_id.split("@")[0]), "role": role,
        "skills_json": json.dumps(p.get("skills") or []),
        "active": 1,
        "confidence_threshold": float(p.get("confidence_threshold") or 0),
        "model": str(p.get("model") or ""),
        "last_assigned_at": "", "created_at": now(),
    }
    k3.upsert("agents", [row])
    registry.invalidate()
    return {"agent_id": agent_id, "kind": kind, "role": role or None}


def update_agent(k3, p: dict) -> dict:
    """Flip role/active/skills on an existing agent (partial update via SQL)."""
    aid = str(p.get("agent_id") or "").strip().lower()
    if not registry.get(k3, aid):
        return {"error": f"no agent {aid!r}"}
    sets = []
    if "role" in p:
        if p["role"] not in AGENT_ROLES:
            return {"error": f"invalid role {p['role']!r}"}
        sets.append(f"role={sql_str(p['role'])}")
    if "active" in p:
        sets.append(f"active={1 if p['active'] else 0}")
    if "skills" in p:
        sets.append(f"skills_json={sql_str(json.dumps(p['skills'] or []))}")
    if "confidence_threshold" in p:
        sets.append(f"confidence_threshold={float(p['confidence_threshold'])}")
    if not sets:
        return {"error": "nothing to update (role / active / skills / confidence_threshold)"}
    k3.execute(f"UPDATE agents SET {', '.join(sets)} WHERE agent_id={sql_str(aid)}")
    registry.invalidate()
    return {"agent_id": aid, "updated": len(sets)}


def remove_agent(k3, p: dict) -> dict:
    """Deactivate (soft-delete): history keeps pointing at a real row."""
    aid = str(p.get("agent_id") or "").strip().lower()
    k3.execute(f"UPDATE agents SET active=0 WHERE agent_id={sql_str(aid)}")
    registry.invalidate()
    return {"agent_id": aid, "active": False}


def list_agents(k3, _p: dict) -> dict:
    rows = [{k: v for k, v in a.items() if k != "skills_json"} for a in registry.all_agents(k3)]
    return {"count": len(rows), "agents": rows}


# ------------------------------------------------------------------- routing rules
def upsert_rule(k3, p: dict) -> dict:
    """Create or edit one routing rule — rules are ROWS, editing is not a deploy.

    Matching fields (category/priority/channel) are exact-or-any ('' = any);
    `assign_to` names an agent, or leave it '' and set pool_skill/allow_ai to
    define the pool. Lower `position` wins; the seeded catch-alls sit at 9999.
    """
    rid = str(p.get("rule_id") or ("rr_" + uuid.uuid4().hex[:8]))
    row = {
        "rule_id": rid,
        "position": int(p.get("position", 100)),
        "on_event": str(p.get("on_event") or "created"),
        "category": str(p.get("category") or ""),
        "priority": str(p.get("priority") or ""),
        "channel": str(p.get("channel") or ""),
        "assign_to": str(p.get("assign_to") or "").strip().lower(),
        "pool_skill": str(p.get("pool_skill") or ""),
        "allow_ai": 1 if p.get("allow_ai") else 0,
        "enabled": 0 if p.get("enabled") is False else 1,
        "created_at": now(),
    }
    if row["on_event"] not in ("created", "customer_reply", "escalation"):
        return {"error": "on_event must be created | customer_reply | escalation"}
    k3.upsert("routing_rules", [row])
    return {"rule_id": rid, "position": row["position"], "on_event": row["on_event"]}


def delete_rule(k3, p: dict) -> dict:
    rid = str(p.get("rule_id") or "")
    k3.execute(f"UPDATE routing_rules SET enabled=0 WHERE rule_id={sql_str(rid)}")
    return {"rule_id": rid, "enabled": False}


def list_rules(k3, _p: dict) -> dict:
    rows = k3.execute("SELECT * FROM routing_rules WHERE enabled=1 ORDER BY on_event, position")
    return {"count": len(rows), "rules": rows}


# ----------------------------------------------------------------------- reporting
def stats(k3, p: dict) -> dict:
    open_by_priority = k3.execute(
        "SELECT priority, COUNT(*) AS n FROM tickets WHERE status <> 'solved' "
        "GROUP BY priority ORDER BY n DESC")
    by_category = k3.execute(
        "SELECT category, COUNT(*) AS n FROM tickets GROUP BY category ORDER BY n DESC LIMIT 10")
    volume = k3.execute(
        "SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM tickets "
        "GROUP BY day ORDER BY day DESC LIMIT 14")
    by_assignee = k3.execute(
        "SELECT assignee, COUNT(*) AS n FROM tickets WHERE status <> 'solved' AND assignee <> '' "
        "GROUP BY assignee ORDER BY n DESC LIMIT 20")
    return {"open_by_priority": open_by_priority, "by_category": by_category,
            "volume_by_day": volume, "open_by_assignee": by_assignee,
            "csat": _csat_stats(k3),
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
    statistics.mean/median plus interpolated p90 — the metric a support lead lives by.
    An AI agent's reply counts: it IS a first response (that's the deflection win)."""
    rows = k3.execute("SELECT created_at, first_response_at FROM tickets "
                      "WHERE first_response_at <> '' AND first_response_at IS NOT NULL")
    mins: list[float] = []
    for r in rows:
        c, f = parse_ts(r.get("created_at")), parse_ts(r.get("first_response_at"))
        if c and f and f >= c:
            mins.append((f - c).total_seconds() / 60.0)
    if not mins:
        return {"answered": 0}
    mins.sort()
    return {"answered": len(mins), "mean": round(statistics.mean(mins), 1),
            "median": round(statistics.median(mins), 1),
            "p90": round(percentile(mins, 90), 1), "max": round(mins[-1], 1)}


def _sla_report(k3) -> dict:
    """Flag open, still-unanswered tickets that have breached (or are nearing) their
    per-priority first-response SLA — computed in Python with datetime deltas."""
    import datetime as _dt
    rows = k3.execute("SELECT priority, created_at FROM tickets WHERE status <> 'solved' "
                      "AND (first_response_at='' OR first_response_at IS NULL)")
    now_dt = _dt.datetime.now(_dt.timezone.utc)
    breaching, at_risk = 0, 0
    for r in rows:
        created = parse_ts(r.get("created_at"))
        if not created:
            continue
        target_h = SLA_FIRST_RESPONSE_HOURS.get(r.get("priority", "normal"), 8)
        age_h = (now_dt - created).total_seconds() / 3600.0
        if age_h > target_h:
            breaching += 1
        elif age_h > 0.75 * target_h:
            at_risk += 1
    return {"targets_hours": SLA_FIRST_RESPONSE_HOURS, "open_unanswered": len(rows),
            "breaching": breaching, "at_risk": at_risk}
