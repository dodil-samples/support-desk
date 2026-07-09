"""Shared helpers for the action modules: time, SQL quoting, enums, message store.

The rule that keeps this repo traceable: ``actions/`` is WHAT the product does
(one file per domain, mapping 1:1 to the actions table in the README), ``lib/``
is HOW it talks to things (K3, models, mail, identity, the gate).
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid

# Ticket lifecycle enum — the single source of truth. The portals' status
# dropdowns/pills and info.yaml mirror this list; set_status and the
# list_tickets filter reject anything outside it.
STATUSES = ("new", "open", "pending", "solved")

# First-response SLA targets (hours) per priority — Zendesk-style.
SLA_FIRST_RESPONSE_HOURS = {"urgent": 1, "high": 4, "normal": 8, "low": 24}


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str, fallback: str = "item") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or fallback)[:48]


def sql_str(v) -> str:
    """Single-quote-escape a value for inline SQL (ids/enums only, not free text)."""
    return "'" + str(v).replace("'", "''") + "'"


def parse_ts(s: str | None) -> _dt.datetime | None:
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


def norm_ts(s) -> str:
    """Normalize either timestamp shape for safe string comparison (see parse_ts)."""
    return str(s or "").replace("T", " ").rstrip("Z").strip()


def percentile(sorted_vals: list[float], pct: float) -> float:
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


def one(k3, sql: str) -> dict:
    rows = k3.execute(sql)
    return rows[0] if rows else {}


def store_message(k3, ticket_id: str, author: str, role: str, body: str) -> dict:
    mid = "m_" + uuid.uuid4().hex[:12]
    key = f"tickets/{ticket_id}/messages/{mid}.txt"
    k3.put_object(key, body)
    row = {
        "message_id": mid, "ticket_id": ticket_id, "author": author, "role": role,
        "body_key": key, "snippet": body.strip().replace("\n", " ")[:200], "created_at": now(),
    }
    k3.upsert("messages", [row])
    return row


def latest_customer_body(k3, tid: str) -> str:
    row = one(k3, f"SELECT body_key FROM messages WHERE ticket_id={sql_str(tid)} "
                  f"AND role='customer' ORDER BY created_at DESC LIMIT 1")
    if not row.get("body_key"):
        return ""
    try:
        return k3.get_object(row["body_key"]).decode("utf-8", "replace")
    except Exception:
        return ""
