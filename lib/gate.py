"""Public / private access gate + lightweight API-key management.

The app is deployed with ``--allow-unauthenticated`` so its public FQDN is
anonymously invokable (CORS-open) — a help-center widget, a website contact form,
or a CRM inbound-email webhook can POST straight to it with no Dodil credentials.
That makes an in-app gate the real trust boundary, so we split every action into
two tiers:

  PUBLIC   — customer-facing: submit a ticket, check your own ticket, search the
             KB. Safe to expose anonymously; optionally gated by a non-secret
             *project key* embedded in the widget. This is the "public backend".
  PRIVATE  — the agent inbox: list/triage/reply/assign/stats + key management.
             Gated by an *admin key*. This is the "private backend".

Keys travel in the JSON body (field ``key``), because the anon FQDN's CORS
preflight only allows the ``content-type`` request header — a browser cannot send
a custom header cross-origin.

Keys come from two places, merged:
  * env — ADMIN_KEYS / PUBLIC_KEYS (comma-separated). Provisioned by the
          IAM-authenticated operator at deploy time; the bootstrap credential.
  * K3  — the ``api_keys`` table, managed at runtime via create_key/list_keys/
          revoke_key (all PRIVATE actions). This is the "user management".

Graceful default: if a tier has NO keys configured (env empty AND table empty),
that tier is OPEN — so existing ``dodil ignite invoke`` calls keep working, and
you lock a tier down simply by configuring a key for it.
"""

from __future__ import annotations

import datetime as _dt
import os
import uuid

# Actions any anonymous caller may run. Everything else is PRIVATE.
PUBLIC_ACTIONS = {"submit_ticket", "ticket_status", "search_kb"}


def is_public_action(action: str) -> bool:
    return action in PUBLIC_ACTIONS


def _env_keys(name: str) -> set[str]:
    return {s.strip() for s in os.getenv(name, "").split(",") if s.strip()}


_ENV_ADMIN = _env_keys("ADMIN_KEYS")
_ENV_PUBLIC = _env_keys("PUBLIC_KEYS")

# One read per cold start, refreshed after a create/revoke on THIS replica. None =
# not loaded yet. Env keys are immediate + globally consistent; a create_key /
# revoke_key done via the table converges on other warm replicas within one
# cold-start cycle (they reload on their next start). Use env keys for anything
# that must flip instantly fleet-wide.
_table_admin: set[str] | None = None
_table_public: set[str] | None = None


def invalidate_key_cache() -> None:
    global _table_admin, _table_public
    _table_admin = None
    _table_public = None


def _load_table_keys(k3) -> None:
    global _table_admin, _table_public
    if _table_admin is not None and _table_public is not None:
        return
    _table_admin, _table_public = set(), set()
    try:
        rows = k3.execute("SELECT key, kind FROM api_keys WHERE disabled = 0")
        for r in rows:
            key = str(r.get("key") or "")
            if not key:
                continue
            (_table_admin if str(r.get("kind")) == "admin" else _table_public).add(key)
    except Exception:
        # Table missing / not yet compacted — treat as no dynamic keys this round.
        pass


def _resolve_keys(k3) -> tuple[set[str], set[str], bool, bool]:
    _load_table_keys(k3)
    admin = set(_ENV_ADMIN) | (_table_admin or set())
    public = set(_ENV_PUBLIC) | (_table_public or set())
    return admin, public, bool(admin), bool(public)


def authorize(k3, action: str, payload: dict) -> dict:
    """Decide whether ``action`` may run given the key on the payload.

    Admin keys are a superset of public — an admin key satisfies any tier.
    Returns ``{"ok": bool, "role": str, "error": str?}``.
    """
    provided = str(payload.get("key") or payload.get("admin_key") or payload.get("public_key") or "")
    admin, public, admin_configured, public_configured = _resolve_keys(k3)
    is_admin = bool(provided) and provided in admin
    is_public = bool(provided) and provided in public
    role = "admin" if is_admin else "public" if is_public else "anon"

    if is_public_action(action):
        if (not public_configured) or is_public or is_admin:
            return {"ok": True, "role": role}
        return {"ok": False, "role": role, "error": "a valid project key is required (payload.key)"}
    # PRIVATE
    if (not admin_configured) or is_admin:
        return {"ok": True, "role": role}
    return {"ok": False, "role": role, "error": "admin key required for this action (payload.key)"}


# --------------------------------------------------------------------- key mgmt (PRIVATE)
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mint(kind: str) -> str:
    return f"{'ak' if kind == 'admin' else 'pk'}_{uuid.uuid4().hex}"


def create_key(k3, p: dict) -> dict:
    """Create a new project (public) or admin key."""
    kind = "admin" if p.get("kind") == "admin" else "public"
    key = _mint(kind)
    k3.upsert("api_keys", [{
        "key": key, "label": str(p.get("label") or ""), "kind": kind,
        "created_at": _now(), "disabled": 0,
    }])
    invalidate_key_cache()
    return {"key": key, "kind": kind, "label": p.get("label") or ""}


def list_keys(k3, _p: dict) -> dict:
    rows = k3.execute(
        "SELECT key, label, kind, created_at, disabled FROM api_keys "
        "ORDER BY created_at DESC LIMIT 200"
    )
    return {"keys": rows, "count": len(rows)}


def revoke_key(k3, p: dict) -> dict:
    key = str(p.get("revoke") or p.get("target_key") or "")
    if not key:
        return {"error": "provide `revoke` (or `target_key`) — the key to revoke"}
    k3.execute(f"UPDATE api_keys SET disabled = 1 WHERE key = '{key.replace(chr(39), chr(39) * 2)}'")
    invalidate_key_cache()
    return {"revoked": key}
