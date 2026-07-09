"""Support Desk — one Ignite (Python) function, action-routed.

A Zendesk-style shared inbox where K3 is the whole backend: tickets + messages in
the SQL warehouse, full bodies + KB articles as objects, and a vector collection
for suggested answers. Ignite Models does triage, routing and reply drafting.

This file is only the ENTRYPOINT (Ignite compile mode requires it at the root):
parse the event, resolve who is calling, gate the action, dispatch. The product
lives in actions/ (one file per domain — the README actions table maps 1:1);
the plumbing lives in lib/ (K3, models, mail, identity, gate).

Invoke with an `action`; e.g.
  {"action":"submit_ticket","subject":"...","body":"...","requester_email":"..."}
  {"action":"suggest_reply","ticket_id":"..."}
  {"action":"stats"}
"""

from __future__ import annotations

import json
import os
import sys

# Make the app's own modules importable regardless of the launcher's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from actions import ACTIONS
from lib import gate, identity
from lib.k3 import K3Error
import bootstrap


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
    if not gate.exposes(action):
        return {"ok": False, "action": action, "code": 404,
                "error": f"action {action!r} is not served by this deployment "
                         f"(APP_ROLE={gate.APP_ROLE})"}

    try:
        k3 = bootstrap.ensure()
        # Who is calling? (AUTH_MODE adapter — anonymous is a valid answer.)
        ident = identity.identify(k3, event)
        event["_identity"] = ident
        # Public/private gate: PUBLIC actions are anon-safe (optionally
        # project-keyed); PRIVATE actions need the admin key or a staff identity,
        # and ADMIN_ONLY actions need the admin level. Unconfigured tiers stay
        # open (see lib/gate.py).
        decision = gate.authorize(k3, action, event, ident)
        if not decision["ok"]:
            return {"ok": False, "action": action, "error": decision["error"], "code": 401}
        result = fn(k3, event)
        code = result.pop("code", None) if isinstance(result, dict) else None
        if isinstance(result, dict) and "error" in result and code:
            return {"ok": False, "action": action, "error": result["error"], "code": code}
        return {"ok": True, "action": action, "result": result}
    except K3Error as e:
        return {"ok": False, "action": action, "error": f"k3: {e}"}
    except Exception as e:  # noqa: BLE001 — surface a clean error to the caller
        return {"ok": False, "action": action, "error": f"{type(e).__name__}: {e}"}
