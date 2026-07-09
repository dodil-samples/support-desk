"""Offline backend smoke: APP_ROLE gating, fail-closed admin, status enum, instant submit.
Scenario comes from argv[1] (public|admin|admin_keyed|all) since gate reads env at import.
"""
import sys, os, time, threading

scenario = sys.argv[1]
os.environ["APP_ROLE"] = {"admin_keyed": "admin"}.get(scenario, scenario)
if scenario == "admin_keyed":
    os.environ["ADMIN_KEYS"] = "ak_test"

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import models

model_calls = []
def fake_chat_json(system, user, max_tokens=2000):
    model_calls.append(user); time.sleep(0.5)
    return {"category": "billing", "priority": "high", "sentiment": "negative", "tags": []}
models.chat_json = fake_chat_json

class FakeK3:
    def __init__(self): self.calls = []; self.lock = threading.Lock()
    def upsert(self, table, rows):
        with self.lock: self.calls.append(("upsert", table))
    insert = upsert
    def put_object(self, key, body, content_type="text/plain"):
        with self.lock: self.calls.append(("put", key))
    def execute(self, sql, freshness=None):
        with self.lock: self.calls.append(("sql", sql))
        return []
    def vector_search(self, q, top_k=5, min_score=None):
        with self.lock: self.calls.append(("vector", q))
        return []
    def ensure_bucket(self, *a, **k): pass
    def create_table(self, *a, **k): pass
    def ensure_vector(self, *a, **k): pass
    def has_vector_collection(self): return True

import handler, bootstrap
fake = FakeK3()
bootstrap.ensure = lambda: fake

def call(payload):
    return handler.handler(payload, None)

if scenario == "public":
    r = call({"action": "list_tickets"})
    assert not r.get("ok") and "not served" in r["error"], r
    t0 = time.monotonic()
    r = call({"action": "submit_ticket", "subject": "s", "body": "b", "requester_email": "a@b.c"})
    dt = time.monotonic() - t0
    assert r["ok"] and r["result"]["ticket_id"].startswith("t_"), r
    assert "suggested_help" not in r["result"], "KB must not ride on submit"
    assert dt < 0.25, f"submit not instant: {dt:.2f}s"
    assert not any(c[0] == "vector" for c in fake.calls), "submit must not vector-search"
    time.sleep(0.8)
    assert model_calls, "background triage never ran"
    assert any(c[0] == "sql" and "UPDATE tickets" in c[1] for c in fake.calls), "triage UPDATE missing"
    print("PUBLIC ok: private blocked, submit instant + pure-write, bg triage landed")

elif scenario == "admin":
    r = call({"action": "stats"})
    assert not r.get("ok") and "fail-closed" in r["error"], r
    print("ADMIN (no keys) ok: fail-closed")

elif scenario == "admin_keyed":
    fake.execute_orig = fake.execute
    r = call({"action": "set_status", "ticket_id": "t_x", "status": "escalated", "key": "ak_test"})
    assert r["ok"] and "invalid status" in r["result"]["error"], r
    r = call({"action": "set_status", "ticket_id": "t_x", "status": "solved", "key": "ak_test"})
    assert r["ok"] and r["result"].get("status") == "solved", r
    r = call({"action": "list_tickets", "status": "closed", "key": "ak_test"})
    assert "invalid status" in r["result"]["error"], r
    r = call({"action": "stats", "key": "ak_wrong"})
    assert not r.get("ok"), r
    print("ADMIN (keyed) ok: enum enforced, wrong key rejected")

elif scenario == "all":
    r = call({"action": "stats"})
    assert r["ok"], r
    print("ALL ok: combined dev deploy stays open when unconfigured")
