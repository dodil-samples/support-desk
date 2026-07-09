# AGENTS.md — how to customize & deploy this sample with a coding agent

You are (probably) a coding agent asked to turn this sample into someone's
actual support desk. Don't guess what they want — **interview first, then
build**. This file is the protocol: the questions to ask, what each answer
changes, and how to verify you didn't break the machine.

## Ground rules (violating these breaks the sample's promises)

1. **Zero pip/npm dependencies.** Backend is stdlib-only Python (compile-mode
   Ignite needs no requirements.txt); portals are zero-dep Node (`node:http`).
   If a change needs a library, propose it explicitly — don't slip it in.
2. **Enums have one home each and mirrors are labeled.** Ticket status lives in
   `actions/common.py` (`STATUSES`); triage categories/priorities live in the
   prompt in `actions/triage.py`. The UIs mirror them (`web/*/app.js`,
   `info.yaml`) — when you change an enum, grep for the old values and update
   every mirror in the same commit.
3. **Seams stay seams.** Identity = `lib/identity.py` (`AUTH_MODE` adapters),
   mail = `lib/mailer.py` (`send_email`), models = `lib/models.py`. Extend by
   adding an adapter/implementation, never by inlining provider code elsewhere.
4. **`actions/` = what the product does; `lib/` = how it talks to things.**
   New behavior goes in the right layer or a new `actions/<domain>.py`
   registered in `actions/__init__.py` (and tiered in `lib/gate.py`).
5. **Everything is env-configured.** No URLs/keys/flags in UI or code.

## The interview — ask these before touching anything

**Deployment & identity**
- Which org, and what should the four apps be called? (defaults:
  `support-desk-public/-admin/-portal/-inbox`)
- Sign-in mode? `none` (anonymous), `email` (built-in passwordless — then: real
  mail via `MAIL_WEBHOOK_URL`, or demo `SEND_MODE=test`?), `oidc` (get the
  issuer + client id/secret), or `header` (behind which auth proxy?).
- Who are the first admins? (`AGENT_EMAILS`/`AGENT_DOMAINS` — the bootstrap
  credential; real staff are registered in the inbox afterwards.)

**Staffing & routing**
- Which humans (email, role admin|agent, skills) and which AI agents (name,
  skills, always-escalate demo mode?) should exist on day one?
- Routing style: rules only (`ROUTING=rules`), AI-assisted pick within pools
  (`ROUTING=ai`), or off? Which category/priority → team mappings beyond the
  seeded catch-alls?
- Should AI agents answer customers directly (default) or only draft for
  humans? (The latter = don't create AI agents; agents use ✨ Draft with AI.)

**Product shape**
- Do the triage categories fit the business? (Changing them = the enum edit in
  `actions/triage.py` + mirrors, see ground rule 2.)
- SLA targets per priority? (`SLA_FIRST_RESPONSE_HOURS` in `actions/common.py`.)
- Branding: portal/inbox titles, colors (CSS design tokens in each
  `web/*/index.html` `:root` block — no build step), help-center copy.
- KB seed articles? (Each is one `add_kb` call; embedding takes ~20–30 s.)

**Models & cost**
- Default model OK? (`MODEL_NAME`, default `moonshot-v1-auto` ~2 s/call.
  Reasoning models cost ~30 s/call — only for `suggest_reply` quality.)
  Per-AI-agent override lives on the agent row (`model`).

## Where each answer lands

| answer | change |
|---|---|
| app names / org | deploy commands in README step 2–3 (`--env` stays the same) |
| sign-in mode | backend env (`AUTH_MODE`, + `SESSION_SECRET`/`OIDC_ISSUER`/`PROXY_SECRET`), portal env for oidc (`OIDC_*`) |
| real mail | `SEND_MODE=webhook` + `MAIL_WEBHOOK_URL`, or swap `send_email` in `lib/mailer.py` (SES/SendGrid ≈ 10 lines) |
| first admins | `AGENT_EMAILS`/`AGENT_DOMAINS` env on both backends |
| staff & AI agents | `add_agent` calls (or the inbox Agents panel) — not env, not code |
| routing mappings | `upsert_rule` calls (or the inbox Rules panel) — rules are rows |
| categories / SLA | `actions/triage.py` prompt + `actions/common.py` + mirrors (`web/admin/app.js` CATEGORIES/PRIORITIES, `info.yaml`) |
| branding | `web/portal/index.html` + `web/admin/index.html` (`<title>`, header, `:root` tokens) |
| status enum | `actions/common.py` STATUSES + mirrors (both `app.js`, `info.yaml`) |

## Verify before you hand over

```bash
cd tests
for s in public admin admin_keyed all;            do python3 smoke_backend.py  $s; done
for s in none email email_agent oidc_cfg header;  do python3 smoke_identity.py $s; done
python3 smoke_routing.py          # registry, rules, round-robin, AI worker, escalation
node smoke_servers.mjs            # both portal proxies (allowlists, key injection)
node smoke_cookie.mjs             # HttpOnly session cookie flow
# against the live deployment (needs AUTH_MODE=email + SEND_MODE=test):
PORTAL_URL=https://… INBOX_URL=https://… node ui_test.mjs
```

If you changed enums, also re-run the live verification curls in README step 4
— the backend must reject values outside the new enum.

## Things users ask for that are already there

- "Instant submit" — yes, by design; don't add anything to the submit path.
  Background work goes in `actions/triage.py::enrich_async`'s chain.
- "Notify agents on assignment" — `actions/routing.py::_notify` via the mail seam.
- "Why did this ticket go there?" — `routing_log`, rendered in the inbox detail.
- "Verified requesters" — identity seam + `verified_emails`; ✓ in the inbox.
- "Agents can't manage the desk" — `ADMIN_ONLY_ACTIONS` in `lib/gate.py`.
