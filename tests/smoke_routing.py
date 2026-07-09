"""Offline routing smoke: registry roles, rules, round-robin, AI worker, escalation.

Runs the whole engine in-process against an in-memory K3 fake and a stubbed
model. One process, sequential scenarios (env is fixed: APP_ROLE=all,
AUTH_MODE=email so identities work, ROUTING=rules).
"""
import os
import pathlib
import sys
import time

os.environ["APP_ROLE"] = "all"
os.environ["AUTH_MODE"] = "email"
os.environ["SESSION_SECRET"] = "s3cr3t-test"
os.environ["SEND_MODE"] = "test"
os.environ["ROUTING"] = "rules"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import models

# Stubbed model: triage says how_to; the AI worker is confident only when the
# KB context has real hits; the router pick is never exercised (ROUTING=rules).
def fake_chat_json(system, user, max_tokens=2000, model=None):
    if "triage assistant" in system:
        return {"category": "how_to", "priority": "normal", "sentiment": "neutral", "tags": []}
    if "self-assess" in system:
        if "(no KB articles matched)" in user:
            return {"confident": False, "reply": "", "reason": "kb does not cover this"}
        return {"confident": True, "reply": "Here's how: see the KB.", "reason": "kb covers it"}
    return {}
models.chat_json = fake_chat_json
models.chat = lambda msgs, max_tokens=2500, model=None: "draft"


class FakeK3:
    """In-memory warehouse understanding exactly the SQL shapes the app emits."""
    def __init__(self):
        self.tables = {t: [] for t in ("tickets", "messages", "api_keys", "login_codes",
                                       "verified_emails", "agents", "routing_rules", "routing_log")}
        self.kb_hits_on = False
    def upsert(self, table, rows):
        tbl = self.tables[table]
        keyfield = {"tickets": "ticket_id", "messages": "message_id", "agents": "agent_id",
                    "routing_rules": "rule_id", "routing_log": "log_id",
                    "login_codes": "code_id", "verified_emails": "email",
                    "api_keys": "key"}[table]
        for r in rows:
            tbl[:] = [x for x in tbl if x.get(keyfield) != r.get(keyfield)]
            tbl.append(dict(r))
    insert = upsert
    def put_object(self, *a, **k): pass
    def get_object(self, key):
        return b"customer body"
    def vector_search(self, q, top_k=5, min_score=None):
        return [{"text": "KB: how to export invoices", "key": "kb/x", "score": 0.9}] \
            if self.kb_hits_on else []
    def ensure_bucket(self, *a, **k): pass
    def create_table(self, *a, **k): pass
    def ensure_vector(self, *a, **k): pass
    def has_vector_collection(self): return True
    def execute(self, sql, freshness=None):
        s = " ".join(sql.split())
        def val(field):  # value of field='...' in the WHERE clause
            m = s.split(f"{field}='")
            return m[1].split("'")[0] if len(m) > 1 else None
        if s.startswith("UPDATE"):
            table = s.split()[1]
            keyfield = {"tickets": "ticket_id", "agents": "agent_id",
                        "routing_rules": "rule_id", "login_codes": "code_id"}[table]
            key = val(keyfield)
            sets = s.split(" SET ", 1)[1].rsplit(" WHERE ", 1)[0]
            for row in self.tables[table]:
                if row.get(keyfield) == key:
                    for part in self._split_sets(sets):
                        f, v = part.split("=", 1)
                        v = v.strip()
                        if v.startswith("'"):
                            row[f.strip()] = v.strip("'")
                        elif v.upper().startswith("CASE"):
                            if not row.get(f.strip()):
                                row[f.strip()] = v.split("THEN '")[1].split("'")[0]
                        else:
                            try: row[f.strip()] = float(v) if "." in v else int(v)
                            except ValueError: row[f.strip()] = v
            return []
        if "COUNT(*)" in s and "FROM routing_rules" in s:
            return [{"n": len(self.tables["routing_rules"])}]
        if "COUNT(*)" in s and "FROM messages" in s:
            tid, author = val("ticket_id"), val("author")
            n = sum(1 for m in self.tables["messages"]
                    if m["ticket_id"] == tid and m.get("role") == "agent"
                    and (author is None or m.get("author") == author))
            return [{"n": n}]
        if "FROM routing_rules" in s:
            ev = val("on_event")
            rows = [r for r in self.tables["routing_rules"]
                    if r.get("enabled") and (ev is None or r.get("on_event") == ev)]
            return sorted(rows, key=lambda r: r.get("position", 0))
        if "FROM agents" in s:
            return [dict(r) for r in self.tables["agents"]]
        if "FROM tickets WHERE ticket_id=" in s:
            tid = val("ticket_id")
            return [dict(r) for r in self.tables["tickets"] if r["ticket_id"] == tid]
        if "SELECT assignee, COUNT(*)" in s:
            out = {}
            for t in self.tables["tickets"]:
                if t.get("status") != "solved" and t.get("assignee"):
                    out[t["assignee"]] = out.get(t["assignee"], 0) + 1
            return [{"assignee": k, "n": v} for k, v in out.items()]
        if "FROM messages WHERE ticket_id=" in s:
            tid = val("ticket_id")
            rows = [dict(m) for m in self.tables["messages"] if m["ticket_id"] == tid]
            if "role='customer'" in s:
                rows = [m for m in rows if m.get("role") == "customer"]
            return rows
        if "FROM routing_log" in s:
            tid = val("ticket_id")
            return [dict(r) for r in self.tables["routing_log"]
                    if tid is None or r.get("ticket_id") == tid]
        if "FROM login_codes" in s:
            email = val("email")
            return [dict(r) for r in reversed(self.tables["login_codes"])
                    if r["email"] == email and not r["used"]][:5]
        if "FROM verified_emails" in s:
            return [{"email": r["email"]} for r in self.tables["verified_emails"]]
        if "FROM api_keys" in s:
            return []
        return []
    @staticmethod
    def _split_sets(sets):
        # split on commas not inside quotes/CASE
        out, depth, cur, q = [], 0, "", False
        for ch in sets:
            if ch == "'": q = not q
            if ch == "," and not q and depth == 0:
                out.append(cur); cur = ""; continue
            if not q and sets and ch == "C" and cur.endswith("CAS"): pass
            cur += ch
        out.append(cur)
        # merge CASE...END fragments split by commas inside (none in our SQL)
        return [p for p in out if "=" in p]


import handler, bootstrap
from lib import agents as registry
fake = FakeK3()
bootstrap.ensure = lambda: fake
bootstrap._seed_default_rules(fake)
call = lambda p: handler.handler(p, None)
KEY = None  # APP_ROLE=all + no keys -> open (dev deploy)

def sign_in(email):
    r = call({"action": "request_code", "email": email})
    return call({"action": "verify_code", "email": email,
                 "code": r["result"]["demo_code"]})["result"]["session"]

fails = 0
def check(name, cond, ctx=""):
    global fails
    print(("ok  " if cond else "FAIL") + " " + name + ("" if cond else f"  {ctx}"))
    if not cond: fails += 1

# --- 1. registry + role levels
r = call({"action": "add_agent", "kind": "human", "email": "boss@corp.io", "role": "admin"})
r = call({"action": "add_agent", "kind": "human", "email": "amal@corp.io", "role": "agent",
          "skills": ["billing"]})
r = call({"action": "add_agent", "kind": "human", "email": "omar@corp.io", "role": "agent"})
r = call({"action": "add_agent", "kind": "ai", "name": "KB Bot", "skills": ["how_to"]})
check("agents registered (2 agents, 1 admin, 1 ai)", r["ok"] and r["result"]["agent_id"] == "kb-bot", r)

boss, amal = sign_in("boss@corp.io"), sign_in("amal@corp.io")
r = call({"action": "whoami", "session": boss})
check("registry role: admin", r["result"]["identity"]["role"] == "admin", r)
r = call({"action": "whoami", "session": amal})
check("registry role: agent", r["result"]["identity"]["role"] == "agent", r)
r = call({"action": "list_tickets", "session": amal})
check("agent identity works tickets", r["ok"], r)
r = call({"action": "add_agent", "kind": "human", "email": "x@y.io", "session": amal})
check("agent CANNOT manage the desk", not r["ok"] and "admin role" in r["error"], r)
r = call({"action": "upsert_rule", "on_event": "created", "category": "how_to",
          "assign_to": "kb-bot", "allow_ai": True, "position": 1, "session": boss})
check("admin can add rules", r["ok"], r)

# --- 2. routing: rule -> AI agent answers (KB covers it)
fake.kb_hits_on = True
r = call({"action": "submit_ticket", "subject": "how to export invoices",
          "body": "how do I export?", "requester_email": "c1@x.io"})
tid = r["result"]["ticket_id"]
time.sleep(0.5)  # background chain: triage -> route -> ai_work
t = [x for x in fake.tables["tickets"] if x["ticket_id"] == tid][0]
check("rule routed how_to ticket to kb-bot", t.get("assignee") == "kb-bot", t)
check("AI answered: status pending + first_response_at",
      t.get("status") == "pending" and t.get("first_response_at"), t)
ai_msgs = [m for m in fake.tables["messages"] if m["ticket_id"] == tid and m["role"] == "agent"]
check("AI reply stored as agent message", len(ai_msgs) == 1 and ai_msgs[0]["author"] == "kb-bot", ai_msgs)
log = [l for l in fake.tables["routing_log"] if l["ticket_id"] == tid]
check("routing log has rule + ai_work entries",
      any(l["decided_by"].startswith("rule:") for l in log)
      and any(l["event"] == "ai_work" for l in log), log)

# --- 3. AI not confident -> escalates to humans (round-robin, never AI)
fake.kb_hits_on = False
r = call({"action": "submit_ticket", "subject": "how to fly to the moon",
          "body": "???", "requester_email": "c2@x.io"})
tid2 = r["result"]["ticket_id"]
time.sleep(0.5)
t2 = [x for x in fake.tables["tickets"] if x["ticket_id"] == tid2][0]
check("unconfident AI escalated to a human",
      t2.get("assignee") in ("boss@corp.io", "amal@corp.io", "omar@corp.io"), t2)
log2 = [l for l in fake.tables["routing_log"] if l["ticket_id"] == tid2]
check("escalation logged with the AI's reason",
      any(l["event"] == "escalation" and "kb does not cover" in l["reason"] for l in log2), log2)

# --- 4. round-robin spreads follow-up tickets across humans
assignees = set()
for i in range(3):
    r = call({"action": "submit_ticket", "subject": f"moon {i}", "body": "?",
              "requester_email": f"c{i+3}@x.io"})
    time.sleep(0.4)
    t = [x for x in fake.tables["tickets"] if x["ticket_id"] == r["result"]["ticket_id"]][0]
    assignees.add(t.get("assignee"))
check("round-robin rotates humans", len(assignees) >= 2, assignees)

# --- 5. customer reply on AI-assigned ticket escalates
sess = sign_in("c1@x.io")
r = call({"action": "reply_ticket", "ticket_id": tid, "body": "that didn't help", "session": sess})
time.sleep(0.4)
t = [x for x in fake.tables["tickets"] if x["ticket_id"] == tid][0]
check("customer reply pulled ticket off the AI", t.get("assignee") != "kb-bot", t)

# --- 6. manual assign is logged
r = call({"action": "assign", "ticket_id": tid2, "assignee": "amal@corp.io", "session": boss})
check("manual assign works + logged", r["ok"] and any(
    l["decided_by"].startswith("manual:") for l in fake.tables["routing_log"]
    if l["ticket_id"] == tid2), r)

print(f"\n{'ALL OK' if not fails else f'{fails} FAILURES'}")
sys.exit(1 if fails else 0)
