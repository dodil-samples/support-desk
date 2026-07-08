"""Idempotent schema + storage bootstrap for the Support Desk.

Called once per cold start (cheap, guarded). Creates the bucket, the two warehouse
tables, and the KB vector collection. Re-running is safe: create calls that hit an
existing table/bucket are ignored.
"""

from __future__ import annotations

import os

from lib.k3 import K3, col, T_STRING, T_LONG, T_INT

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


def k3() -> K3:
    return K3(BUCKET)


def ensure() -> K3:
    c = k3()
    # Bucket + tables: provision once (cheap, idempotent).
    if not _state["base"]:
        c.ensure_bucket("Support desk: tickets, messages, KB")
        for name, cols in (("tickets", TICKETS_COLUMNS), ("messages", MESSAGES_COLUMNS),
                           ("api_keys", API_KEYS_COLUMNS)):
            try:
                c.create_table(name, cols, merge_keys=[cols[0]["name"]])
            except Exception:
                pass  # already exists
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
