"""LLM triage + the background enrichment chain (triage → route).

`submit_ticket` never waits for any of this: the ticket row exists with
TRIAGE_DEFAULTS and the chain runs on a daemon thread in the warm replica,
UPDATEing the row when the model answers and then handing the classified
ticket to the routing engine (actions/routing.py). Best-effort — if the
replica is recycled mid-flight the ticket keeps its defaults unassigned, and
the admin `triage` action re-runs classification (+ routing) on demand.
"""

from __future__ import annotations

import json
import threading

from lib import models

from .common import now, sql_str

TRIAGE_SYSTEM = (
    "You are a support-desk triage assistant. Classify the ticket and return ONLY "
    "compact JSON with keys: category (billing|bug|how_to|account|feature_request|other), "
    "priority (low|normal|high|urgent), sentiment (positive|neutral|negative), "
    "tags (array of <=4 short lowercase strings)."
)

# Placeholder classification a ticket carries until (background) triage lands —
# identical to classify()'s fallbacks, so consumers never see a special state.
TRIAGE_DEFAULTS = {"category": "other", "priority": "normal", "sentiment": "neutral", "tags": []}


def classify(subject: str, body: str) -> dict:
    out = models.chat_json(TRIAGE_SYSTEM, f"Subject: {subject}\n\nBody: {body}") or {}
    return {
        "category": out.get("category", "other"),
        "priority": out.get("priority", "normal"),
        "sentiment": out.get("sentiment", "neutral"),
        "tags": out.get("tags") or [],
    }


def apply(k3, ticket_id: str, tri: dict) -> None:
    k3.execute(
        f"UPDATE tickets SET category={sql_str(tri['category'])}, "
        f"priority={sql_str(tri['priority'])}, sentiment={sql_str(tri['sentiment'])}, "
        f"tags_json={sql_str(json.dumps(tri['tags']))}, updated_at={sql_str(now())} "
        f"WHERE ticket_id={sql_str(ticket_id)}"
    )


def enrich_async(k3, ticket_id: str, subject: str, body: str) -> None:
    """Background chain for a fresh public ticket: classify, then route.

    Routing runs AFTER triage on purpose — the rules match on triage's outputs
    (category/priority), the exact trigger→routing pipeline Zendesk runs.
    """
    def work():
        try:
            apply(k3, ticket_id, classify(subject, body))
        except Exception:
            pass  # keeps TRIAGE_DEFAULTS; routing still gets a shot below
        try:
            from . import routing  # late import — routing pulls in kb/models
            routing.route_ticket(k3, ticket_id, event="created")
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()
