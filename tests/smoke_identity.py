"""Offline identity smoke: AUTH_MODE adapters, code flow, ownership, agent unlock.
Scenario from argv[1]: none | email | email_agent | oidc_cfg | header
(env is read at import time, so one process per scenario).
"""
import sys, os, threading, time

scenario = sys.argv[1]
os.environ["APP_ROLE"] = "public" if scenario != "email_agent" else "admin"
os.environ["AUTH_MODE"] = {"none": "none", "oidc_cfg": "oidc", "header": "header"}.get(scenario, "email")
os.environ["SESSION_SECRET"] = "s3cr3t-test"
os.environ["SEND_MODE"] = "test"
if scenario == "email_agent":
    os.environ["AGENT_DOMAINS"] = "dodil.io"
    os.environ["ADMIN_KEYS"] = "ak_test"  # admin backend fail-closed needs keys configured
if scenario == "oidc_cfg":
    os.environ["OIDC_ISSUER"] = "https://id.example.com/realms/x"
if scenario == "header":
    os.environ["PROXY_SECRET"] = "proxy-shared"

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import models
models.chat_json = lambda s, u, max_tokens=2000: {"category": "how_to", "priority": "low",
                                                  "sentiment": "neutral", "tags": []}

class FakeK3:
    """Tiny in-memory warehouse: enough SQL understanding for the identity flows."""
    def __init__(self):
        self.tables = {"login_codes": [], "verified_emails": [], "tickets": [], "messages": []}
        self.lock = threading.Lock()
    def upsert(self, table, rows):
        with self.lock: self.tables.setdefault(table, []).extend(rows)
    insert = upsert
    def put_object(self, *a, **k): pass
    def vector_search(self, *a, **k): return []
    def ensure_bucket(self, *a, **k): pass
    def create_table(self, *a, **k): pass
    def ensure_vector(self, *a, **k): pass
    def has_vector_collection(self): return True
    def execute(self, sql, freshness=None):
        with self.lock:
            s = sql.strip()
            if s.startswith("SELECT code_id"):
                email = s.split("email='")[1].split("'")[0]
                return [dict(r) for r in reversed(self.tables["login_codes"])
                        if r["email"] == email and not r["used"]][:5]
            if s.startswith("UPDATE login_codes"):
                cid = s.split("code_id='")[1].split("'")[0]
                for r in self.tables["login_codes"]:
                    if r["code_id"] == cid: r["used"] = 1
                return []
            if s.startswith("SELECT email FROM verified_emails"):
                wanted = {p.strip(" '") for p in s.split("(")[1].rstrip(")").split(",")}
                return [{"email": r["email"]} for r in self.tables["verified_emails"]
                        if r["email"] in wanted]
            if "FROM tickets WHERE lower(requester_email)" in s:
                email = s.split("requester_email)='")[1].split("'")[0]
                return [dict(r) for r in self.tables["tickets"] if r["requester_email"] == email]
            if "FROM tickets WHERE ticket_id=" in s:
                tid = s.split("ticket_id='")[1].split("'")[0]
                return [dict(r) for r in self.tables["tickets"] if r["ticket_id"] == tid]
            if s.startswith("UPDATE tickets"):
                return []
            if "FROM messages" in s or "FROM api_keys" in s:
                return []
            return []

import handler, bootstrap
fake = FakeK3()
bootstrap.ensure = lambda: fake
call = lambda p: handler.handler(p, None)

if scenario == "none":
    r = call({"action": "auth_config"})
    assert r["ok"] and r["result"]["mode"] == "none", r
    r = call({"action": "whoami"})
    assert r["ok"] and r["result"]["anonymous"], r
    r = call({"action": "my_tickets"})
    assert not r["ok"] and r.get("code") == 401, r
    r = call({"action": "request_code", "email": "a@b.c"})
    assert "not email-based" in r["result"]["error"], r
    print("NONE ok: anonymous flow intact, signed-in actions 401, code flow off")

elif scenario == "email":
    r = call({"action": "auth_config"})
    assert r["result"] == {"mode": "email", "ready": True, "send_mode": "test"}, r
    # bad email rejected
    r = call({"action": "request_code", "email": "nope"})
    assert "valid email" in r["result"]["error"], r
    # code flow
    r = call({"action": "request_code", "email": "Cust@Acme.IO"})
    code = r["result"]["demo_code"]; assert r["result"]["sent"] and len(code) == 6, r
    r = call({"action": "verify_code", "email": "cust@acme.io", "code": "000000"})
    assert "invalid or expired" in r["result"]["error"] or code == "000000", r
    r = call({"action": "verify_code", "email": "cust@acme.io", "code": code})
    sess = r["result"]["session"]
    assert sess.startswith("sd1.") and r["result"]["identity"]["role"] == "user", r
    # single-use
    r2 = call({"action": "verify_code", "email": "cust@acme.io", "code": code})
    assert "invalid or expired" in r2["result"]["error"], r2
    # whoami + verified_emails recorded
    r = call({"action": "whoami", "session": sess})
    assert r["result"]["identity"]["email"] == "cust@acme.io", r
    assert fake.tables["verified_emails"], "verify_code must record the proven email"
    # tampered session -> anonymous
    r = call({"action": "whoami", "session": sess[:-2] + "xx"})
    assert r["result"]["anonymous"], r
    # signed-in submit: identity fills the requester + verified True
    r = call({"action": "submit_ticket", "subject": "s", "body": "b", "session": sess})
    tid = r["result"]["ticket_id"]
    assert r["result"]["verified"] is True, r
    assert fake.tables["tickets"][-1]["requester_email"] == "cust@acme.io", fake.tables["tickets"][-1]
    # my_tickets lists it; anonymous cannot
    r = call({"action": "my_tickets", "session": sess})
    assert r["ok"] and r["result"]["count"] == 1, r
    # reply: ownership enforced
    r = call({"action": "reply_ticket", "ticket_id": tid, "body": "more info", "session": sess})
    assert r["ok"] and r["result"]["ticket_id"] == tid, r
    other = call({"action": "request_code", "email": "other@x.io"})
    osess = call({"action": "verify_code", "email": "other@x.io",
                  "code": other["result"]["demo_code"]})["result"]["session"]
    r = call({"action": "reply_ticket", "ticket_id": tid, "body": "hijack", "session": osess})
    assert "no ticket found" in r["result"]["error"], r
    # ticket_status works with session identity, no retyped email
    r = call({"action": "ticket_status", "ticket_id": tid, "session": sess})
    assert r["ok"] and r["result"]["ticket"]["ticket_id"] == tid, r
    time.sleep(0.3)  # let bg triage thread finish quietly
    print("EMAIL ok: code flow, single-use, sessions, ownership, verified submit")

elif scenario == "email_agent":
    # An AGENT_DOMAINS-matched identity is a bootstrap ADMIN — it must satisfy
    # the private tier (incl. admin-only actions) with no key.
    r = call({"action": "request_code", "email": "seemo@dodil.io"})
    sess = call({"action": "verify_code", "email": "seemo@dodil.io",
                 "code": r["result"]["demo_code"]})["result"]["session"]
    r = call({"action": "whoami", "session": sess})
    assert r["result"]["identity"]["role"] == "admin", r
    r = call({"action": "stats", "session": sess})
    assert r["ok"], r
    r = call({"action": "stats"})
    assert not r["ok"], r
    # customer identity must NOT unlock admin actions
    r2 = call({"action": "request_code", "email": "cust@gmail.com"})
    csess = call({"action": "verify_code", "email": "cust@gmail.com",
                  "code": r2["result"]["demo_code"]})["result"]["session"]
    r = call({"action": "stats", "session": csess})
    assert not r["ok"], r
    print("AGENT ok: agent identity unlocks admin tier, customer identity doesn't")

elif scenario == "oidc_cfg":
    r = call({"action": "auth_config"})
    assert r["result"]["mode"] == "oidc" and r["result"]["issuer"], r
    r = call({"action": "whoami"})  # no token -> anonymous, never an error
    assert r["result"]["anonymous"], r
    print("OIDC ok: config advertised, tokenless caller anonymous")

elif scenario == "header":
    r = call({"action": "whoami", "proxy_email": "u@corp.io", "proxy_secret": "proxy-shared"})
    assert r["result"]["identity"]["email"] == "u@corp.io", r
    r = call({"action": "whoami", "proxy_email": "u@corp.io", "proxy_secret": "WRONG"})
    assert r["result"]["anonymous"], r
    print("HEADER ok: proxy assertion honored, bad secret anonymous")
