"""Outbound mail seam — one function, provider-agnostic (mirrors the CRM sample).

Everything above this file calls ``send_email(to, subject, text)`` and never
knows which provider is behind it. Swap providers by editing this one function
(SES/SendGrid are each a ~10-line body) or, with zero code changes, point
``MAIL_WEBHOOK_URL`` at anything that accepts a JSON POST (a Zapier hook, a tiny
relay function, your existing notification service).

Env:
  SEND_MODE         test | webhook   (default test)
  MAIL_WEBHOOK_URL  where webhook mode POSTs {to, subject, text}
  MAIL_FROM         informational from-address forwarded to the webhook
"""

from __future__ import annotations

import os

from . import http

SEND_MODE = os.getenv("SEND_MODE", "test").strip().lower()
MAIL_WEBHOOK_URL = os.getenv("MAIL_WEBHOOK_URL", "")
MAIL_FROM = os.getenv("MAIL_FROM", "support@example.com")


class SendBlocked(RuntimeError):
    """Raised when mail cannot leave in the current mode/config."""


def send_email(to: str, subject: str, text: str) -> str:
    """Send one message; returns a provider message id (or a hold marker).

    ``test`` mode never sends — it logs the message and reports it as held, so
    the whole sample installs and demos with no mail provider configured
    (login codes surface via the API's demo field instead; see identity.py).
    """
    if SEND_MODE == "test":
        print(f"[mail:test] to={to} subject={subject!r} text={text[:200]!r}")
        return "held:test-mode"
    if SEND_MODE == "webhook":
        if not MAIL_WEBHOOK_URL:
            raise SendBlocked("SEND_MODE=webhook but MAIL_WEBHOOK_URL is not set")
        status, body = http.request_json(
            "POST", MAIL_WEBHOOK_URL,
            json_body={"to": to, "from": MAIL_FROM, "subject": subject, "text": text},
            timeout=15,
        )
        if status >= 300:
            raise SendBlocked(f"mail webhook -> HTTP {status}: {str(body)[:200]}")
        return str((body or {}).get("id", "sent")) if isinstance(body, dict) else "sent"
    raise SendBlocked(f"unknown SEND_MODE {SEND_MODE!r} (expected test|webhook)")
