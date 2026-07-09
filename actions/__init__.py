"""The action registry — one flat name → function map, grouped by domain file.

Tiers are declared in lib/gate.py (PUBLIC_ACTIONS / ADMIN_ONLY_ACTIONS); this
package only says what exists. actions/ = WHAT the product does, lib/ = HOW it
talks to the world.
"""

from __future__ import annotations

from lib import gate, identity

from . import admin, kb, tickets

ACTIONS = {
    # -- PUBLIC (anon-safe: customer-facing) — tickets.py / kb.py --
    "submit_ticket": tickets.submit_ticket, "ticket_status": tickets.ticket_status,
    "search_kb": kb.search_kb,
    # -- PUBLIC identity (lib/identity.py; my_tickets/reply_ticket need a session) --
    "auth_config": identity.auth_config, "request_code": identity.request_code,
    "verify_code": identity.verify_code, "whoami": identity.whoami,
    "my_tickets": tickets.my_tickets, "reply_ticket": tickets.reply_ticket,
    # -- PRIVATE, agent level (admin key or any staff identity) --
    "create_ticket": tickets.create_ticket, "add_message": tickets.add_message,
    "triage": tickets.retriage, "suggest_reply": kb.suggest_reply, "add_kb": kb.add_kb,
    "get_ticket": tickets.get_ticket, "list_tickets": tickets.list_tickets,
    "set_status": tickets.set_status, "assign": tickets.assign, "rate": tickets.rate,
    "stats": admin.stats, "export_tickets": tickets.export_tickets,
    "list_agents": admin.list_agents, "list_rules": admin.list_rules,
    "list_kb": kb.list_kb, "get_kb": kb.get_kb,
    # -- PRIVATE, admin level (ADMIN_ONLY_ACTIONS in lib/gate.py) --
    "add_agent": admin.add_agent, "update_agent": admin.update_agent,
    "remove_agent": admin.remove_agent,
    "upsert_rule": admin.upsert_rule, "delete_rule": admin.delete_rule,
    "remove_kb": kb.remove_kb,
    "create_key": gate.create_key, "list_keys": gate.list_keys, "revoke_key": gate.revoke_key,
}
