"""Agent registry cache — the `agents` table, one read per cold start.

One registry holds BOTH kinds of agent:
  human  — signs in via lib/identity.py; `agent_id` IS their email; has a role
           (admin = manage agents/rules/keys, agent = work tickets) and skills.
  ai     — an internal actor (never signs in): allowed skills, a confidence
           threshold, an optional model override. Routing can assign it tickets
           and it answers them (actions/routing.py).

Same caching contract as the API keys in lib/gate.py: env-free, loaded once per
replica, invalidated on CRUD from THIS replica, converges elsewhere within a
cold-start cycle.
"""

from __future__ import annotations

import json

_cache: list[dict] | None = None


def invalidate() -> None:
    global _cache
    _cache = None


def all_agents(k3) -> list[dict]:
    global _cache
    if _cache is None:
        try:
            rows = k3.execute("SELECT * FROM agents ORDER BY created_at")
        except Exception:
            rows = []  # table missing/cold — no registered agents this round
        for r in rows:
            try:
                r["skills"] = json.loads(r.get("skills_json") or "[]")
            except ValueError:
                r["skills"] = []
        _cache = rows
    return _cache


def active(k3, kind: str | None = None, skill: str | None = None) -> list[dict]:
    """Active agents, optionally filtered by kind (human|ai) and skill.
    An agent with NO skills listed is a generalist and matches any skill."""
    out = []
    for a in all_agents(k3):
        if not a.get("active"):
            continue
        if kind and a.get("kind") != kind:
            continue
        if skill and a.get("skills") and skill not in a["skills"]:
            continue
        out.append(a)
    return out


def get(k3, agent_id: str) -> dict | None:
    for a in all_agents(k3):
        if a.get("agent_id") == agent_id:
            return a
    return None


def role_of_email(k3, email: str) -> str | None:
    """The registered role for a human agent's email, or None if unregistered."""
    a = get(k3, str(email or "").strip().lower())
    if a and a.get("kind") == "human" and a.get("active"):
        return a.get("role") or "agent"
    return None
