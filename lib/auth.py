"""Service-account -> bearer token, shared by the K3 and Models clients.

The function authenticates every downstream call (K3 REST + Ignite Models) with one
Dodil service account: mint a short-lived access token via OIDC `client_credentials`,
cache it until just before expiry, and read the org id/name straight out of the JWT
so callers never pass them. Mirrors the research-agent (Atlas) auth module.

Env:
  DODIL_SA_ID / DODIL_SA_SECRET   service-account credentials (required)
  DODIL_OIDC_URL                  token endpoint (defaults to dev)
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

from . import http

OIDC_URL = os.getenv(
    "DODIL_OIDC_URL",
    "https://id.dev.dodil.io/realms/dodil/protocol/openid-connect/token",
)

_lock = threading.Lock()
_state = {"token": None, "exp": 0.0, "org_id": None, "org_name": None}


class NotConfigured(RuntimeError):
    pass


def _decode_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _org_from_claims(claims: dict) -> tuple[str | None, str | None]:
    orgs = claims.get("organization") or {}
    name = next(iter(orgs), None)
    if name:
        return orgs.get(name, {}).get("id"), name
    return claims.get("org_id"), claims.get("org_name")


def get_token() -> str:
    with _lock:
        now = time.time()
        if _state["token"] and now < _state["exp"] - 30:
            return _state["token"]

        sa_id = os.getenv("DODIL_SA_ID", "")
        sa_secret = os.getenv("DODIL_SA_SECRET", "")
        if not sa_id or not sa_secret:
            raise NotConfigured(
                "DODIL_SA_ID / DODIL_SA_SECRET are not set — the function needs a "
                "service account to call K3 and the model endpoint."
            )

        status, body = http.request_json(
            "POST",
            OIDC_URL,
            form_body={
                "client_id": sa_id,
                "client_secret": sa_secret,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        if status >= 300 or not isinstance(body, dict) or "access_token" not in body:
            raise NotConfigured(f"token request failed (HTTP {status}): {body}")
        token = body["access_token"]
        claims = _decode_claims(token)
        _state["token"] = token
        _state["exp"] = float(claims.get("exp", now + 300))
        _state["org_id"], _state["org_name"] = _org_from_claims(claims)
        return token


def org_id() -> str:
    get_token()
    return _state["org_id"] or ""


def org_name() -> str:
    get_token()
    return _state["org_name"] or ""


def is_configured() -> bool:
    return bool(os.getenv("DODIL_SA_ID") and os.getenv("DODIL_SA_SECRET"))
