"""Idempotent schema + storage bootstrap for the Support Desk.

Called once per cold start (cheap, guarded). Creates the bucket, the two warehouse
tables, and the KB vector collection. Re-running is safe: create calls that hit an
existing table/bucket are ignored.
"""

from __future__ import annotations

import os

import datetime as _dt

from lib.k3 import K3, col, T_STRING, T_LONG, T_INT, T_DOUBLE

BUCKET = os.getenv("SUPPORT_BUCKET", "support-desk")
KB_COLLECTION = os.getenv("SUPPORT_KB_COLLECTION", "kb")

_state = {"base": False, "vector": False}

# One row per ticket — the queryable warehouse (status, priority, SLA timestamps).
TICKETS_COLUMNS = [
    col("ticket_id", T_STRING, nullable=False),
    col("subject", T_STRING),
    col("requester_email", T_STRING),
    col("channel", T_STRING),           # email | chat | web | api
    col("status", T_STRING),            # new | open | pending | solved
    col("priority", T_STRING),          # low | normal | high | urgent
    col("category", T_STRING),          # billing | bug | how_to | account | ...
    col("sentiment", T_STRING),         # positive | neutral | negative
    col("assignee", T_STRING),
    col("csat", T_INT),                 # 1..5, null until rated
    col("created_at", T_STRING),
    col("updated_at", T_STRING),
    col("first_response_at", T_STRING),
    col("solved_at", T_STRING),
    col("tags_json", T_STRING),
]

# One row per message (customer or agent). Full body lives in S3; snippet is inline.
MESSAGES_COLUMNS = [
    col("message_id", T_STRING, nullable=False),
    col("ticket_id", T_STRING, nullable=False),
    col("author", T_STRING),
    col("role", T_STRING),              # customer | agent | system
    col("body_key", T_STRING),          # S3 key of the full body
    col("snippet", T_STRING),
    col("created_at", T_STRING),
]

# One row per API key — the public/private gate's user-management store (see lib/gate.py).
# `kind` is public (project widget key) or admin; `disabled` soft-deletes on revoke.
API_KEYS_COLUMNS = [
    col("key", T_STRING, nullable=False),
    col("label", T_STRING),
    col("kind", T_STRING),              # public | admin
    col("created_at", T_STRING),
    col("disabled", T_INT),
]

# Passwordless sign-in codes (AUTH_MODE=email, lib/identity.py). Only HMACs are
# stored — a warehouse reader learns nothing usable. `used` makes them single-use.
LOGIN_CODES_COLUMNS = [
    col("code_id", T_STRING, nullable=False),
    col("email", T_STRING),
    col("code_hash", T_STRING),
    col("magic_hash", T_STRING),
    col("expires_at", T_STRING),
    col("used", T_INT),
    col("created_at", T_STRING),
]

# Emails whose owner proved control of them (verified via any AUTH_MODE adapter).
# Verification is a property of the EMAIL, not the ticket — tickets are annotated
# by lookup, so adding this feature needed no tickets-table migration.
VERIFIED_EMAILS_COLUMNS = [
    col("email", T_STRING, nullable=False),
    col("verified_at", T_STRING),
    col("mode", T_STRING),              # email | oidc | header
]

# The staff registry — HUMAN and AI agents in one table (lib/agents.py caches it).
# Humans: agent_id = email, role admin|agent. AI: agent_id = name slug, works
# tickets via actions/routing.py.
AGENTS_COLUMNS = [
    col("agent_id", T_STRING, nullable=False),
    col("kind", T_STRING),              # human | ai
    col("email", T_STRING),             # humans only
    col("name", T_STRING),
    col("role", T_STRING),              # humans: admin | agent
    col("skills_json", T_STRING),       # categories this agent handles ([] = generalist)
    col("active", T_INT),
    col("confidence_threshold", T_DOUBLE),  # ai: 1 = always escalate (demo the path)
    col("model", T_STRING),             # ai: optional model override
    col("last_assigned_at", T_STRING),  # round-robin bookkeeping
    col("created_at", T_STRING),
]

# Routing rules-as-data (the CRM sample's flows idiom): ordered, first match
# wins, '' matches anything. assign_to names an agent; otherwise pool_skill +
# allow_ai define the pool. Editing a campaign of rules is an UPSERT, not a deploy.
ROUTING_RULES_COLUMNS = [
    col("rule_id", T_STRING, nullable=False),
    col("position", T_INT),
    col("on_event", T_STRING),          # created | customer_reply | escalation
    col("category", T_STRING),
    col("priority", T_STRING),
    col("channel", T_STRING),
    col("assign_to", T_STRING),         # agent_id, '' = use the pool
    col("pool_skill", T_STRING),        # '' = all skills
    col("allow_ai", T_INT),             # may the pool include AI agents?
    col("enabled", T_INT),
    col("created_at", T_STRING),
]

# Why every ticket sits where it sits: rule:<id> | ai:<agent> | manual:<who>.
ROUTING_LOG_COLUMNS = [
    col("log_id", T_STRING, nullable=False),
    col("ticket_id", T_STRING),
    col("event", T_STRING),             # created | customer_reply | escalation | ai_work | manual
    col("decided_by", T_STRING),
    col("assign_to", T_STRING),
    col("reason", T_STRING),
    col("ts", T_STRING),
]

# Default rules seeded ONCE (only when the table is empty): a catch-all
# round-robin over active humans for fresh tickets, and the same for
# escalations — so routing works the moment the first agent is registered.
DEFAULT_RULES = [
    {"rule_id": "default-created", "position": 9999, "on_event": "created",
     "category": "", "priority": "", "channel": "", "assign_to": "",
     "pool_skill": "", "allow_ai": 0, "enabled": 1, "created_at": ""},
    {"rule_id": "default-escalation", "position": 9999, "on_event": "escalation",
     "category": "", "priority": "", "channel": "", "assign_to": "",
     "pool_skill": "", "allow_ai": 0, "enabled": 1, "created_at": ""},
]


def k3() -> K3:
    return K3(BUCKET)


def _seed_default_rules(c: K3) -> None:
    """Seed the catch-all routing rules exactly once (only when none exist) —
    an admin can edit/disable them like any other rule; re-seeding would undo that."""
    try:
        if c.execute("SELECT COUNT(*) AS n FROM routing_rules")[0].get("n", 0):
            return
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c.upsert("routing_rules", [{**r, "created_at": ts} for r in DEFAULT_RULES])
    except Exception:
        pass  # table cold — the next invocation retries


def ensure() -> K3:
    c = k3()
    # Bucket + tables: provision once (cheap, idempotent).
    if not _state["base"]:
        c.ensure_bucket("Support desk: tickets, messages, KB")
        for name, cols in (("tickets", TICKETS_COLUMNS), ("messages", MESSAGES_COLUMNS),
                           ("api_keys", API_KEYS_COLUMNS), ("login_codes", LOGIN_CODES_COLUMNS),
                           ("verified_emails", VERIFIED_EMAILS_COLUMNS),
                           ("agents", AGENTS_COLUMNS), ("routing_rules", ROUTING_RULES_COLUMNS),
                           ("routing_log", ROUTING_LOG_COLUMNS)):
            try:
                c.create_table(name, cols, merge_keys=[cols[0]["name"]])
            except Exception:
                pass  # already exists
        _seed_default_rules(c)
        _state["base"] = True
    # Vector engine provisions asynchronously and can lag a cold start, so keep
    # retrying every invocation until the KB collection actually exists.
    if not _state["vector"]:
        try:
            c.ensure_vector(KB_COLLECTION, template_id="text_embedding_index",
                            include_patterns=["kb/**"])
            _state["vector"] = c.has_vector_collection()
        except Exception:
            pass  # engine not ready yet — retry next invocation
    return c
