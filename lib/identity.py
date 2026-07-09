"""Identity seam — who is calling, independent of HOW they authenticated.

Everything downstream consumes one shape:

    {"email": str, "name": str, "role": "user"|"agent", "verified": bool, "mode": str}

and ``AUTH_MODE`` env picks the adapter that produces it:

  none    (default) no identity — today's anonymous flow. Zero config.
  email   built-in passwordless: request_code mails a 6-digit code + magic
          token (single-use, 15 min), verify_code mints an HMAC-signed session.
          Needs SESSION_SECRET (+ the mail seam, lib/mailer.py). Stdlib only.
  oidc    any OIDC identity provider (Keycloak, Auth0, Authentik, Supabase...).
          The caller presents the IdP's access token; we validate it against
          the issuer's `userinfo` endpoint (discovered, cached) — no JWT
          crypto dependency, and opaque tokens work too. Needs OIDC_ISSUER.
  header  any auth proxy (oauth2-proxy, Authelia, Cloudflare Access): the
          proxy terminates auth and asserts the identity it established with
          a shared secret. Needs PROXY_SECRET.

Role mapping is adapter-independent and comes from two places:
  1. the ``agents`` table (lib/agents.py) — the real staff registry, managed at
     runtime: a registered active human agent gets its row's role
     (``admin`` = manage agents/rules/keys, ``agent`` = work tickets);
  2. AGENT_EMAILS / AGENT_DOMAINS env — the BOOTSTRAP credential (like
     ADMIN_KEYS): matching emails are ``admin``, so the first operator can sign
     in and register everyone else before any agents rows exist.
Everyone else is a ``user``. The same sign-in system serves customers and
staff; nothing is customer-only by construction. Staff identities satisfy the
private tier in lib/gate.py (admins additionally pass the admin-only actions).

The session/token travels in the JSON body (field ``session``), same as API
keys — the anon FQDN's CORS preflight only allows content-type, and the portal
server injects it from an HttpOnly cookie so it never lives in browser JS.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import agents, http, mailer

AUTH_MODE = os.getenv("AUTH_MODE", "none").strip().lower()
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "168"))   # 7 days
CODE_TTL_MINUTES = int(os.getenv("CODE_TTL_MINUTES", "15"))      # industry norm

OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
PROXY_SECRET = os.getenv("PROXY_SECRET", "")

_AGENT_EMAILS = {e.strip().lower() for e in os.getenv("AGENT_EMAILS", "").split(",") if e.strip()}
_AGENT_DOMAINS = {d.strip().lower().lstrip("@") for d in os.getenv("AGENT_DOMAINS", "").split(",") if d.strip()}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_email(v) -> str:
    return str(v or "").strip().lower()


def role_for(k3, email: str) -> str:
    email = _norm_email(email)
    registered = agents.role_of_email(k3, email)  # the runtime staff registry wins
    if registered:
        return registered
    if email in _AGENT_EMAILS or email.split("@")[-1] in _AGENT_DOMAINS:
        return "admin"  # bootstrap operators — they register the real agents
    return "user"


def _identity(k3, email: str, name: str = "", verified: bool = True) -> dict:
    email = _norm_email(email)
    return {"email": email, "name": name or email.split("@")[0],
            "role": role_for(k3, email), "verified": verified, "mode": AUTH_MODE}


# ------------------------------------------------------------- signed sessions (email mode)
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest())


def mint_session(email: str, name: str = "") -> str:
    exp = int(time.time()) + SESSION_TTL_HOURS * 3600
    payload = json.dumps({"e": _norm_email(email), "n": name, "x": exp},
                         separators=(",", ":")).encode()
    return f"sd1.{_b64(payload)}.{_sign(payload)}"


def _check_session(k3, token: str) -> dict | None:
    try:
        prefix, body, sig = token.split(".")
        if prefix != "sd1":
            return None
        payload = _unb64(body)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        claims = json.loads(payload)
        if int(claims.get("x", 0)) < time.time():
            return None
        # Sessions carry the proven EMAIL only; the role is resolved per request
        # from the live registry, so promoting/demoting an agent applies instantly.
        return _identity(k3, claims["e"], claims.get("n", ""))
    except Exception:
        return None


def _code_hash(email: str, code: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), f"{_norm_email(email)}|{code}".encode(),
                    hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ OIDC userinfo adapter
# Validate the presented bearer by asking the ISSUER who it belongs to. One HTTP
# call instead of local JWT verification: no crypto dependency, works with opaque
# tokens, and revocation is honored. Userinfo is cached per-token for a minute;
# the ROLE is composed fresh each call from the live registry.
_oidc_lock = threading.Lock()
_oidc_conf: dict | None = None
_oidc_cache: dict[str, tuple[float, dict | None]] = {}


def _userinfo_endpoint() -> str:
    global _oidc_conf
    with _oidc_lock:
        if _oidc_conf is None:
            status, conf = http.request_json(
                "GET", f"{OIDC_ISSUER}/.well-known/openid-configuration", timeout=10)
            _oidc_conf = conf if status < 300 and isinstance(conf, dict) else {}
        return _oidc_conf.get("userinfo_endpoint", "")


def _oidc_userinfo(token: str) -> dict | None:
    cached = _oidc_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    info_out = None
    endpoint = _userinfo_endpoint()
    if endpoint:
        status, info = http.request_json(
            "GET", endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if status < 300 and isinstance(info, dict) and info.get("email"):
            info_out = {"email": info["email"], "name": info.get("name", ""),
                        "verified": bool(info.get("email_verified", True))}
    if len(_oidc_cache) > 512:  # bound the per-replica cache
        _oidc_cache.clear()
    _oidc_cache[token] = (time.time() + 60, info_out)
    return info_out


# --------------------------------------------------------------------------- the seam
def identify(k3, payload: dict) -> dict | None:
    """Resolve the caller's identity from the request payload, or None (anonymous).

    Never raises — an unusable credential is just anonymous, and the action
    layer decides what anonymous callers may do.
    """
    try:
        if AUTH_MODE == "email":
            token = str(payload.get("session") or "")
            return _check_session(k3, token) if token and SESSION_SECRET else None
        if AUTH_MODE == "oidc":
            token = str(payload.get("session") or payload.get("token") or "")
            info = _oidc_userinfo(token) if token and OIDC_ISSUER else None
            return _identity(k3, info["email"], info["name"], info["verified"]) if info else None
        if AUTH_MODE == "header":
            asserted = _norm_email(payload.get("proxy_email"))
            ok = PROXY_SECRET and hmac.compare_digest(
                str(payload.get("proxy_secret") or ""), PROXY_SECRET)
            return _identity(k3, asserted, str(payload.get("proxy_name") or "")) if ok and asserted else None
        return None  # AUTH_MODE=none
    except Exception:
        return None


def mark_verified(k3, email: str) -> None:
    """Record that this email's owner proved control of it (idempotent upsert)."""
    email = _norm_email(email)
    if email:
        k3.upsert("verified_emails", [{"email": email, "verified_at": _now(), "mode": AUTH_MODE}])


def verified_emails(k3, emails: list[str]) -> set[str]:
    """Which of these emails are proven? (One IN query; used to annotate tickets.)"""
    wanted = sorted({_norm_email(e) for e in emails if e})
    if not wanted:
        return set()
    quoted = ", ".join("'" + e.replace("'", "''") + "'" for e in wanted)
    try:
        rows = k3.execute(f"SELECT email FROM verified_emails WHERE email IN ({quoted})")
        return {_norm_email(r.get("email")) for r in rows}
    except Exception:
        return set()


# ------------------------------------------------------------------- actions (PUBLIC)
def auth_config(_k3, _p: dict) -> dict:
    """What sign-in looks like here — the portal shapes its UI from this."""
    out = {"mode": AUTH_MODE}
    if AUTH_MODE == "email":
        out["ready"] = bool(SESSION_SECRET)
        out["send_mode"] = mailer.SEND_MODE
    if AUTH_MODE == "oidc":
        out["ready"] = bool(OIDC_ISSUER)
        out["issuer"] = OIDC_ISSUER
    return out


def request_code(k3, p: dict) -> dict:
    """EMAIL MODE: start a passwordless sign-in — mail a 6-digit code + magic token.

    Single-use and short-lived (CODE_TTL_MINUTES); only HMACs of the secrets are
    stored, so the warehouse rows are useless to a reader. In SEND_MODE=test the
    code comes back in the response as ``demo_code`` — that is FOR DEMOS ONLY and
    is disabled the moment real mail is configured.
    """
    if AUTH_MODE != "email":
        return {"error": f"sign-in is not email-based here (AUTH_MODE={AUTH_MODE})"}
    if not SESSION_SECRET:
        return {"error": "email sign-in is not configured: set SESSION_SECRET"}
    email = _norm_email(p.get("email"))
    if "@" not in email:
        return {"error": "a valid email is required"}
    code = f"{secrets.randbelow(1_000_000):06d}"
    magic = secrets.token_urlsafe(24)
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=CODE_TTL_MINUTES)
    k3.upsert("login_codes", [{
        "code_id": "lc_" + secrets.token_hex(8), "email": email,
        "code_hash": _code_hash(email, code), "magic_hash": _code_hash(email, magic),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), "used": 0, "created_at": _now(),
    }])
    try:
        mailer.send_email(
            email, "Your sign-in code",
            f"Your support-desk sign-in code is {code} (valid {CODE_TTL_MINUTES} minutes).\n"
            f"Or use this one-time token: {magic}")
    except mailer.SendBlocked as e:
        return {"error": f"could not send the code: {e}"}
    out = {"sent": True, "email": email, "expires_in_minutes": CODE_TTL_MINUTES}
    if mailer.SEND_MODE == "test":
        out["demo_code"] = code  # test mode never mails — surface it so the flow is demoable
    return out


def verify_code(k3, p: dict) -> dict:
    """EMAIL MODE: exchange email + code (or magic token) for a signed session."""
    if AUTH_MODE != "email":
        return {"error": f"sign-in is not email-based here (AUTH_MODE={AUTH_MODE})"}
    email = _norm_email(p.get("email"))
    secret = str(p.get("code") or p.get("magic") or "").strip()
    if not email or not secret:
        return {"error": "email and code are required"}
    h = _code_hash(email, secret)
    esc = email.replace("'", "''")
    rows = k3.execute(
        f"SELECT code_id, code_hash, magic_hash, expires_at, used FROM login_codes "
        f"WHERE email='{esc}' AND used=0 ORDER BY created_at DESC LIMIT 5")
    # NB: the warehouse normalizes row-inserted timestamps to "YYYY-MM-DD HH:MM:SS"
    # (no T/Z) — normalize both sides or every code looks expired (space < 'T').
    norm = lambda s: str(s or "").replace("T", " ").rstrip("Z").strip()
    live = [r for r in rows
            if h in (r.get("code_hash"), r.get("magic_hash"))
            and norm(r.get("expires_at")) > norm(_now())]
    if not live:
        return {"error": "invalid or expired code — request a new one"}
    cid = str(live[0]["code_id"]).replace("'", "''")
    k3.execute(f"UPDATE login_codes SET used=1 WHERE code_id='{cid}'")  # single-use
    mark_verified(k3, email)
    ident = _identity(k3, email)
    return {"session": mint_session(email), "identity": ident,
            "expires_in_hours": SESSION_TTL_HOURS}


def whoami(k3, p: dict) -> dict:
    ident = p.get("_identity")
    if not ident:
        return {"anonymous": True, "mode": AUTH_MODE}
    if AUTH_MODE in ("oidc", "header") and ident.get("verified"):
        mark_verified(k3, ident["email"])  # IdP/proxy already proved the email
    return {"anonymous": False, "identity": ident}
