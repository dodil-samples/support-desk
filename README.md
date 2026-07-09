# Support Desk — a realistic ticketing system on Dodil (Ignite + K3)

A Zendesk-style support desk where **K3 is the entire datastore** (SQL warehouse +
object store + vector search) and **Ignite** is the compute + models. One
action-routed Python codebase deployed as **two backends** (public customer API,
private agent API) with a **portal in front of each** — four small apps in total,
the way a real ticketing product is shaped.

```
customers                                agents
   │                                        │
   ▼                                        ▼
support-desk-portal (help center)        support-desk-inbox (agent inbox)
   │  /api proxy, PUBLIC_KEY env           │  /api proxy, ADMIN_KEY env (key never in browser)
   ▼                                        ▼
support-desk-public                      support-desk-admin
  APP_ROLE=public                          APP_ROLE=admin (fail-closed: needs ADMIN_KEYS)
  submit_ticket · ticket_status ·          list/get/stats · add_message · triage ·
  search_kb — nothing else served          suggest_reply · set_status/assign/rate ·
   │                                       export · add_kb · key mgmt
   └────────────── one codebase ───────────┘
                       │
              handler.py — the ENTRYPOINT only: parse → identify → gate → dispatch
                 │
                 ├─ actions/   WHAT the product does (1 file per domain = README table)
                 │    ├─ tickets.py   submit/status/reply/my_tickets · agent work · export
                 │    ├─ routing.py   rules → round-robin/AI pick → AI agents work → escalate
                 │    ├─ triage.py    LLM classify + the background chain (triage → route)
                 │    ├─ kb.py        search_kb · add_kb · grounded reply drafting
                 │    ├─ admin.py     staff registry (human+AI) · routing rules · stats
                 │    └─ common.py    STATUSES enum · SLA targets · SQL/time helpers
                 ├─ lib/       HOW it talks to things
                 │    ├─ gate.py      public/private tiers + admin level + API keys
                 │    ├─ identity.py  WHO is calling — AUTH_MODE adapters (none/email/oidc/header)
                 │    ├─ agents.py    cached staff registry (the `agents` table)
                 │    ├─ mailer.py    send_email(to,subject,text) — provider-agnostic seam
                 │    ├─ auth.py      service account ─► bearer (OIDC client_credentials)
                 │    ├─ k3.py        objects · tables (_execute/insert/merge) · vector/search
                 │    └─ models.py    /v1/chat/completions · /v1/embeddings
                 ├─ bootstrap.py  idempotent bucket + 8 tables + kb collection + seed rules
                 └─ tests/       offline smoke suites + the live Playwright journey
```

> Customizing this with a coding agent? **[AGENTS.md](AGENTS.md)** is the
> protocol: the interview questions to ask, where each answer lands, and the
> verification steps.

Design principles this sample demonstrates:

- **Instant writes, background enrichment.** `submit_ticket` is a pure warehouse
  write (~0.5–1 s measured); LLM triage runs on a background thread and UPDATEs
  the row seconds later. Nothing slow ever sits on the customer path.
- **Separate public and private surfaces** — for the backend *and* the UI, like
  a real product. The private backend is fail-closed.
- **Config is env, not UI.** The portals have no URL/key fields; each portal's
  server proxies `/api` to its backend and injects the key server-side, so the
  admin key never reaches a browser.
- **State is an enum.** Ticket status is `new | open | pending | solved`
  (`STATUSES` in [`handler.py`](handler.py)), enforced by the backend, declared
  in [`info.yaml`](info.yaml), mirrored by both portals.
- **Identity is a seam, not a framework.** `AUTH_MODE` env plugs in any auth
  system (OIDC IdP, auth proxy) or the built-in passwordless email sign-in —
  see [Sign-in: plug in any auth](#sign-in-plug-in-any-auth-auth_mode). The
  default is `none`: everything below works with zero identity config.
- **Rules route, AI works, humans catch.** Staff is data (`agents` table —
  humans AND AI agents), routing rules are rows (first match wins, seeded
  catch-alls), AI agents answer from the KB or escalate to humans with a
  logged reason — see [Agents & routing](#agents--routing-humans-ai-rules).

---

## The recipe

Verified end-to-end on dev, 2026-07-09. Every step below was run exactly as
written (via the dodil CLI / MCP tools — the MCP tool names mirror the command
path, e.g. `ignite_app_deploy`, `auth_service-account_create`).

### 0. Prerequisites

- `dodil` CLI authenticated into your org (`dodil auth status`), with IAM rights
  to create service accounts.
- This repo cloned; commands run from the repo root.

### 1. Service account + roles

The functions authenticate to K3 **and** to Ignite Models with one service
account. It needs a role **per service** — K3 alone is not enough; without the
ignite role every model call fails `403 UMA authorization rejected`:

```bash
SA=$(dodil auth service-account create support-desk-sa -o json)
SA_ID=$(echo "$SA" | jq -r '.serviceAccountId')   # e.g. cli-support-desk-sa
SA_SECRET=$(echo "$SA" | jq -r '.secret')          # shown ONCE — save it

dodil auth service-account grant-role "$SA_ID" k3-authorization-service k3.admin
dodil auth service-account grant-role "$SA_ID" ignite-authorization-service ignite.model-user
```

`ignite.model-user` is exactly what this workload needs on the Ignite side: it
covers model invocation (chat + embeddings, token-billed) and nothing else — no
app, build, or secret access. Roles change per org, so check the live catalog
(`dodil auth service-account list-roles`) rather than trusting a doc; some
ignite roles (`ignite.app-viewer`, `ignite.app-developer`) are *conditionable*
and can be scoped to specific apps with `--on resource=<drn>`, but this SA
doesn't touch apps so it needs none of those.

`k3.admin` lets `bootstrap.py` auto-create the bucket; for least privilege,
pre-create the bucket yourself (`dodil k3 bucket create support-desk`) and grant
`k3.editor` instead. (K3 roles are org-wide today — not conditionable yet.)

Pick the keys for the two tiers now:

```bash
ADMIN_KEY="ak_$(openssl rand -hex 12)"   # gates the private backend (required — it's fail-closed)
PUBLIC_KEY="pk_help_widget"              # optional, non-secret widget key
```

### 2. Deploy the two backends (same code, different `APP_ROLE`)

```bash
dodil ignite app deploy support-desk-public --code . --runtime python --tier small \
  --allow-unauthenticated \
  --env DODIL_SA_ID="$SA_ID" --env DODIL_SA_SECRET="$SA_SECRET" \
  --env APP_ROLE=public --env PUBLIC_KEYS="$PUBLIC_KEY"

dodil ignite app deploy support-desk-admin --code . --runtime python --tier small \
  --allow-unauthenticated \
  --env DODIL_SA_ID="$SA_ID" --env DODIL_SA_SECRET="$SA_SECRET" \
  --env APP_ROLE=admin --env ADMIN_KEYS="$ADMIN_KEY"
```

Each deploy takes ~20–40 s and prints the app's FQDN
(`support-desk-public-<org>.ignite.dodil.cloud`, `…-admin-…`). Both share the
service account and the K3 bucket — the split is the served action set + key
policy, not the data.

### 3. Deploy the two portals (BYOI static host + proxy)

```bash
ORG=<your-org>

dodil ignite app deploy support-desk-portal --code ./web/portal --dockerfile-path Dockerfile \
  --allow-unauthenticated --tier small --health-path /healthz \
  --env BACKEND_URL="https://support-desk-public-$ORG.ignite.dodil.cloud/" \
  --env PUBLIC_KEY="$PUBLIC_KEY"

dodil ignite app deploy support-desk-inbox --code ./web/admin --dockerfile-path Dockerfile \
  --allow-unauthenticated --tier small --health-path /healthz \
  --env BACKEND_URL="https://support-desk-admin-$ORG.ignite.dodil.cloud/" \
  --env ADMIN_KEY="$ADMIN_KEY"
```

Image builds take ~40–60 s. ⚠️ **Freshly minted backend FQDNs can take a couple
of minutes to become resolvable from inside the cluster** — if a portal logs
`backend fetch failed: ENOTFOUND` (`dodil ignite app logs <org>:support-desk-inbox`),
just wait and retry; it clears on its own.

### 4. Verify

```bash
PORTAL=https://support-desk-portal-$ORG.ignite.dodil.cloud
INBOX=https://support-desk-inbox-$ORG.ignite.dodil.cloud

# instant submission (measured: 12–15 s on the very first call while bootstrap
# provisions bucket/tables on a cold replica, then 0.5–1 s):
curl -X POST $PORTAL/api -H 'content-type: application/json' -d \
  '{"action":"submit_ticket","subject":"Charged twice for order #8891","body":"Billed $129 twice, please refund.","requester_email":"sam@acme.io","channel":"web"}'
# → {"ok":true,...,"ticket_id":"t_…","status":"new"}  — instantly, no triage on the path

# background triage lands within ~5 s (moonshot-v1-auto):
curl -X POST $INBOX/api -H 'content-type: application/json' -d \
  '{"action":"get_ticket","ticket_id":"t_…"}'
# → category=billing, priority=urgent/high, sentiment=negative, tags=[…]

# the public backend refuses private actions even WITH a valid key:
curl -X POST https://support-desk-public-$ORG.ignite.dodil.cloud/ \
  -H 'content-type: application/json' -d '{"action":"list_tickets","key":"'$ADMIN_KEY'"}'
# → "action 'list_tickets' is not served by this deployment (APP_ROLE=public)"

# the admin backend is fail-closed / rejects bad keys:
curl -X POST https://support-desk-admin-$ORG.ignite.dodil.cloud/ \
  -H 'content-type: application/json' -d '{"action":"stats"}'
# → 401 "admin key required for this action"

# status is an enum:
curl -X POST $INBOX/api -H 'content-type: application/json' -d \
  '{"action":"set_status","ticket_id":"t_…","status":"escalated"}'
# → "invalid status 'escalated' — expected one of ['new', 'open', 'pending', 'solved']"
```

Then open `$PORTAL` (help center) and `$INBOX` (agent inbox) in a browser.

### 5. Seed the knowledge base

```bash
curl -X POST $INBOX/api -H 'content-type: application/json' -d \
  '{"action":"add_kb","title":"Refund policy","body":"Duplicate charges are auto-refunded within 3 business days."}'
```

Embedding takes ~20–30 s, then `search_kb` returns semantic hits (measured
score ≈0.84 for a matching query; ~2 s warm, up to ~25 s on the very first
query while the fresh vector collection loads). The portal's deflection search,
the inbox's ✨ *Draft with AI* (`suggest_reply`), and the AI agents' answers are
all grounded in these articles.

Day-to-day KB management lives in the inbox's **Knowledge base panel**
(publish / read / delete — `add_kb`/`list_kb`/`get_kb` are staff actions,
`remove_kb` is admin-only). Under the hood an article is just an object under
`kb/` in the bucket with an ingest rule embedding `kb/**` — so you can also
bulk-load files without touching the app at all:

```bash
dodil k3 object create support-desk kb/sso-setup.md --file ./docs/sso-setup.md
# any file the ingest pipeline can read; the periodic sync (or add_kb's
# trigger) embeds it into the same collection
```

### 6. Optional: turn on sign-in (verified end-to-end with Playwright)

Redeploy the backends with the built-in passwordless email adapter — no other
infrastructure needed (`SEND_MODE=test` logs the codes and surfaces them as
`demo_code`, so the demo needs no mail provider):

```bash
SESSION_SECRET=$(openssl rand -hex 24)
# add to BOTH backend deploys in step 2 (and redeploy):
#   --env AUTH_MODE=email --env SESSION_SECRET=$SESSION_SECRET \
#   --env SEND_MODE=test  --env AGENT_DOMAINS=yourcompany.com
```

The portal now grows a **Your account** card (email → code → signed in), a
**My tickets** list, and in-portal replies; the inbox marks proven requesters
with ✓. Signed-in flow measured live: code verify <1 s, submit still ~0.7 s.
The whole browser journey is scripted in [`tests/ui_test.mjs`](tests/ui_test.mjs)
(Playwright — 23 checks, run live against this deployment):

```bash
PORTAL_URL=$PORTAL INBOX_URL=$INBOX node tests/ui_test.mjs
```
See [Sign-in: plug in any auth](#sign-in-plug-in-any-auth-auth_mode) for the
OIDC / auth-proxy adapters and how mail leaves in production.

### 7. Staff the desk: agents + routing rules (verified live)

Register humans and AI agents, then map categories to them — all rows, no
redeploys (the inbox has panels for all of this; curls shown for the recipe):

```bash
J='content-type: application/json'
# two humans (one with a billing skill) and one AI agent for how-to questions
curl -X POST $INBOX/api -H "$J" -d '{"action":"add_agent","kind":"human","email":"amal@acme-support.io","name":"Amal","role":"agent","skills":["billing"]}'
curl -X POST $INBOX/api -H "$J" -d '{"action":"add_agent","kind":"human","email":"tarek@acme-support.io","name":"Tarek","role":"agent"}'
curl -X POST $INBOX/api -H "$J" -d '{"action":"add_agent","kind":"ai","name":"KB Bot","skills":["how_to"]}'
# rules: how_to → the AI agent; billing → the billing-skilled human pool
curl -X POST $INBOX/api -H "$J" -d '{"action":"upsert_rule","position":1,"on_event":"created","category":"how_to","assign_to":"kb-bot","allow_ai":true}'
curl -X POST $INBOX/api -H "$J" -d '{"action":"upsert_rule","position":2,"on_event":"created","category":"billing","pool_skill":"billing"}'
```

Then watch the machine run (all three measured live on this deployment):

- *"How do I export my invoices to CSV?"* → triage `how_to` → rule 1 → **KB Bot
  answers from the KB** within ~10 s (a real agent message; status `pending`,
  `first_response_at` set).
- *"I was charged twice this month"* → triage `billing` → rule 2 → **Amal**
  (skill pool, round-robin), who gets a notification email through the mail seam.
- *"How do I connect my telescope?"* → rule 1 → KB Bot → self-assesses that the
  KB doesn't cover telescopes → **escalates** → default escalation rule →
  **Tarek**. The inbox ticket detail shows the whole trail under *Routing
  history*.

### Teardown

```bash
for app in support-desk-public support-desk-admin support-desk-portal support-desk-inbox; do
  dodil ignite app delete $app; done
dodil k3 bucket delete support-desk
dodil auth service-account list          # find the SA's uuid
dodil auth service-account delete <uuid> # don't leave a dangling credential
```

---

## Sign-in: plug in any auth (`AUTH_MODE`)

Identity is one seam ([`lib/identity.py`](lib/identity.py)): every action just
consumes `{email, name, role, verified}`, and `AUTH_MODE` picks the adapter
that produces it. This is how the sample stays **fast-install on any stack** —
swapping auth systems is an env change, never a code change.

| `AUTH_MODE` | plugs into | env | how it works |
|---|---|---|---|
| `none` (default) | nothing | — | today's anonymous flow: submit + `ticket_id`+email status checks. Zero config. |
| `email` | nothing — built-in | `SESSION_SECRET`, `SEND_MODE` (+`MAIL_WEBHOOK_URL`) | passwordless: `request_code` mails a 6-digit code + one-time token (single-use, 15 min, only HMACs stored); `verify_code` mints a signed stateless session (7 days). Stdlib only. |
| `oidc` | **any OIDC IdP** — Keycloak, Auth0, Authentik, Supabase, Clerk… | `OIDC_ISSUER` | caller presents the IdP's access token (`session` field); the backend validates it against the issuer's discovered **`userinfo` endpoint** (cached 60 s) — no JWT crypto dependency, opaque tokens work, revocation honored. The portal server runs the standard code flow (`/auth/login` → `/auth/callback`) with `OIDC_ISSUER` + `OIDC_CLIENT_ID`(+`_SECRET`). |
| `header` | **any auth proxy** — oauth2-proxy, Authelia, Cloudflare Access | `PROXY_SECRET` | the proxy terminates auth and asserts the user it established (`proxy_email` + `proxy_secret` on the body) — the forward-auth pattern; the app never speaks the IdP's protocol at all. |

What a signed-in identity unlocks, in ANY mode:

- **`my_tickets`** — list everything for the caller's email. Exists *only* for
  verified identities: the anonymous API deliberately has no list-by-email,
  because an unproven address would leak other people's ticket existence.
- **`reply_ticket`** — reply to (and reopen) your own ticket from the portal.
- **Verified tickets** — a signed-in submit records the proof; agents see ✓ in
  the inbox (`requester_verified` on `list_tickets`/`get_ticket`). Verification
  is a property of the *email* (`verified_emails` table), not the ticket row —
  so the feature needed no schema migration.
- **Agent role = admin access.** An email matching `AGENT_EMAILS`/`AGENT_DOMAINS`
  gets `role=agent`, which satisfies the admin tier exactly like an admin key —
  the same sign-in system serves customers *and* staff; nothing is
  customer-only by construction. API keys remain as the machine credential.

The session travels in the JSON body (`session` field) like the API keys (the
anon FQDN's CORS preflight only allows `content-type`); the portal server keeps
it in an **HttpOnly cookie** and injects it per request — it never exists in
browser JS. Outbound mail is its own seam ([`lib/mailer.py`](lib/mailer.py)):
`SEND_MODE=test` logs codes (and returns `demo_code` — demos with zero mail
infra), `SEND_MODE=webhook` POSTs `{to,subject,text}` to `MAIL_WEBHOOK_URL`;
swapping in SES/SendGrid is one function, like the CRM sample.

## Agents & routing (humans, AI, rules)

The industry pipeline — Zendesk-style triggers→routing with Freshdesk-style
assignment modes — as data on K3:

- **One staff registry, two kinds.** `agents` rows are `human` (agent_id = the
  email the identity seam looks roles up by; role `admin` manages the desk,
  `agent` works tickets) or `ai` (an internal actor with skills, an optional
  model override, and a `confidence_threshold` where 1 = always escalate —
  handy for demoing the path). Manage them from the inbox Agents panel.
- **Rules are rows, first match wins.** Per event (`created` /
  `customer_reply` / `escalation`), match on the triage enums
  (category/priority/channel, '' = any), then either name an agent or define a
  pool (skill filter + whether AI agents are eligible). Bootstrap seeds
  catch-all round-robin rules at position 9999 so routing works the moment the
  first agent exists. `ROUTING=off|rules|ai` env: in `ai` mode the model picks
  the best fit *within the rule's pool* (skills + open load) — rules are the
  guardrails, AI is judgment inside them, never above them.
- **AI agents actually work tickets.** Assigned an AI agent, the ticket gets a
  KB-grounded draft + honest self-assessment: confident → a real agent reply
  (sets `first_response_at` — it IS a first response; status `pending`); not
  confident → **escalation** with the model's reason. Loop safety is hard-coded:
  one AI auto-touch per ticket, escalation pools never include AI, and a
  customer reply on an AI-assigned ticket goes to humans.
- **Every decision is audited** in `routing_log`
  (`rule:<id>` / `ai:<agent>` / `manual:<who>` + reason) and rendered as
  *Routing history* in the inbox — agents always see WHY a ticket is theirs.
- **Assignment notifies the human** through the mail seam (best-effort).
- **Round-robin = longest-since-last-assigned** (Zendesk's semantics), tracked
  on the agent row.

Everything here runs in the background enrichment chain (triage → route → maybe
AI answer) — the customer's submit stays a sub-second pure write.

## Actions

| action | tier | payload | does |
|---|---|---|---|
| `submit_ticket` | 🟢 public | subject, body, requester_email, channel | pure warehouse write, returns instantly with ticket_id + status token; LLM triage runs in the background (KB suggestions are the portal's separate `search_kb` call). Signed-in callers get `verified: true` |
| `ticket_status` | 🟢 public | ticket_id, requester_email | your own ticket's status + messages (email must match — or comes from your session; PII not echoed) |
| `search_kb` | 🟢 public | query, top_k | semantic KB search |
| `auth_config` | 🟢 public | — | which AUTH_MODE is live (the portal shapes its UI from this) |
| `request_code` / `verify_code` | 🟢 public | email / email, code | passwordless sign-in (AUTH_MODE=email): single-use 15-min code → signed session |
| `whoami` | 🟢 public | session? | resolved identity, or `{anonymous: true}` |
| `my_tickets` | 🟣 signed-in | session | all tickets for the caller's **proven** email |
| `reply_ticket` | 🟣 signed-in | session, ticket_id, body | reply to your own ticket; reopens it if solved |
| `create_ticket` | 🔒 admin | subject, body, requester_email, channel | agent-filed ticket with **inline** triage (staff can afford the wait) + KB + duplicate hints |
| `add_message` | 🔒 admin | ticket_id, role, author, body | append message (S3 + row); agent reply sets `first_response_at` |
| `triage` | 🔒 admin | ticket_id | (re)classify category/priority/sentiment via LLM |
| `suggest_reply` | 🔒 admin | ticket_id | KB vector search → LLM-drafted grounded reply |
| `add_kb` / `list_kb` / `get_kb` | 🔒 staff | title, body / — / kb_key | publish, list, read KB articles (objects under `kb/`) |
| `remove_kb` | 🔴 admin-only | kb_key | delete an article (index drops it on the next sync) |
| `get_ticket` | 🔒 admin | ticket_id | ticket + its messages |
| `list_tickets` | 🔒 admin | status?/category?/assignee?/priority?/limit? | warehouse filter (status must be in the enum) |
| `set_status` / `assign` / `rate` | 🔒 admin | ticket_id (+status/assignee/csat) | mutate ticket (`status` must be in the enum) |
| `stats` | 🔒 admin | — | open-by-priority, by-category, volume/day, CSAT, first-response p90, SLA |
| `export_tickets` | 🔒 admin | same filters as list | CSV export |
| `list_agents` / `list_rules` | 🔒 staff | — | the registry and the routing rules (read-only for agents) |
| `add_agent` / `update_agent` / `remove_agent` | 🔴 admin-only | kind, email/name, role, skills… | manage the staff registry (human + AI) |
| `upsert_rule` / `delete_rule` | 🔴 admin-only | position, on_event, matchers, assign_to/pool | manage routing rules |
| `create_key` / `list_keys` / `revoke_key` | 🔴 admin-only | kind?, label? / — / revoke | manage project + admin keys at runtime |

Keys and sessions ride in the request **body** (`key` / `session` fields); the
portals inject them server-side so browsers never carry them. Runtime keys (the
`api_keys` table) merge with the env keys. The private tier has two levels:
🔒 **staff** actions accept the admin key or ANY staff identity (role
agent/admin); 🔴 **admin-only** actions (`ADMIN_ONLY_ACTIONS` in
[`lib/gate.py`](lib/gate.py)) additionally require the admin level — a plain
agent gets `"admin role required"`. All the other 🔒 rows above are staff-level.

## Performance (measured on dev)

| path | latency |
|---|---|
| `submit_ticket` (warm) | **0.5–1 s** |
| first call on a cold replica (bootstrap provisions bucket/tables) | 12–15 s, once |
| background triage lands (`moonshot-v1-auto`) | ≤5 s after submit |
| `search_kb` warm / first query on idle collection | ~2 s / up to ~25 s |
| `suggest_reply` (draft grounded in KB) | ~4–8 s |
| routing decision (rules + round-robin) lands | with triage, ≤10 s after submit |
| AI agent's KB-grounded answer (or escalation) | ~5–10 s after routing |

Env knobs: **`MODEL_NAME`** (default `moonshot-v1-auto`; a reasoning model
like `kimi-k2.6` writes richer drafts but takes ~25–35 s *per call* — with
background triage that only slows admin actions, never the public form),
**`ROUTING`** (`rules` default | `ai` adds a model pick within rule pools |
`off`) and **`K3_VECTOR_TIMEOUT`** (default `20` s per attempt, tried twice;
`5`–`8` is a friendlier ceiling now that the portal polls instead of waiting).
For a demo-snappy public backend keep a warm replica: `--reserved 1` (billed
continuously).

## Customizing (it's meant to be forked)

- **Theme/branding:** each portal is a single static page with CSS design
  tokens in its `:root` block (`web/*/index.html`) — colors, fonts, dark mode —
  plus a `<title>` and header. No build step; edit, redeploy the portal.
- **Enums:** status lives in `actions/common.py`, triage categories/priorities
  in the `actions/triage.py` prompt; the UIs and `info.yaml` mirror them
  (labeled mirrors — grep the old value when you change one).
- **Seams:** identity adapters (`lib/identity.py`), mail provider
  (`lib/mailer.py`), models (`lib/models.py`) — extend the seam, don't inline
  providers elsewhere.
- **With a coding agent:** [`AGENTS.md`](AGENTS.md) is a question-driven
  customization protocol — the agent interviews you (auth mode, staffing,
  rules, branding, models), applies the answers to the right files/envs, and
  runs the verification suites.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| model calls fail `403 UMA authorization rejected` | the SA lacks the **ignite** role — grant `ignite-authorization-service ignite.model-user`. Running replicas cache their bearer ~5 min; wait for expiry (or redeploy) before judging the fix. |
| `grant-role` fails `batch assignment failed … role not found` | the SA holds a role that no longer exists in the org's catalog (renamed/removed in an IAM migration) — the CLI's read-modify-write re-asserts it and the whole batch is rejected. `revoke-role` the stale role first, then grant. Check the live catalog with `list-roles`. |
| portal logs `backend fetch failed: ENOTFOUND` | fresh backend FQDN not yet resolvable in-cluster; wait a couple of minutes. |
| admin backend returns "fail-closed: configure ADMIN_KEYS" | by design — the `APP_ROLE=admin` deployment refuses to run open. |
| `search_kb` empty right after `add_kb` | embedding takes ~20–30 s; the portal polls for exactly this reason. |
| tickets stuck on `other/normal` | background triage is failing — check the model-403 row above, then `dodil ignite app logs <org>:support-desk-public`. |
| bootstrap "already exists" but inserts fail | a pre-existing `tickets`/`messages`/`api_keys` table with a different schema; drop it (`dodil k3 table delete <t> -b support-desk`) and re-invoke. |
| sign-in codes always "invalid or expired" | remember the warehouse normalizes row-written timestamps to `YYYY-MM-DD HH:MM:SS` (no `T`/`Z`) — compare normalized values, never raw ISO strings (see `verify_code` in [`lib/identity.py`](lib/identity.py); we hit exactly this live). |
| signed in but `my_tickets` 401s | the two backends must share the same `SESSION_SECRET` (each deploy replaces the whole env map — redeploy both). |
| routing_log says assigned but the ticket shows unassigned | concurrent UPDATEs to one row are last-writer-wins on the FULL row — a stale-snapshot commit (e.g. a customer reply racing the background chain) can erase another column's write. The engine write-verifies assignments and retries once (`route_ticket` in [`actions/routing.py`](actions/routing.py)); do the same for any concurrent row writes you add. |
| ticket stuck on an AI agent with no reply | the AI answers once, then must escalate — check `routing_log` for its reason, and confirm an `escalation` rule with a non-empty human pool exists (bootstrap seeds one at position 9999). |

## Notes

- **Zero pip dependencies** — stdlib only (`urllib`); the portals' servers are
  zero-dependency Node (`node:http`). Nothing to build anywhere.
- Provisioning is automatic and idempotent (`bootstrap.py`): bucket, `tickets` /
  `messages` / `api_keys` tables (merge-keyed), `kb` vector collection.
- Background triage rides a daemon thread in the warm replica — best-effort by
  design; a recycled replica just leaves the ticket on its neutral defaults and
  the admin `triage` action re-runs it. For guaranteed delivery you'd queue it.
- Local dev: deploy once with `APP_ROLE` unset (= `all`, both tiers on one app,
  open-when-unconfigured), and run the portals locally (`node server.mjs` after
  copying each folder's `.env.example`).
- Fresh writes are read-your-writes on single-table reads (K3 defaults to
  `FRESHNESS_STRONG`); this app's reads are single-table, so no `compact` needed.
- Vector calls degrade to empty results if the embedder is unavailable — tickets,
  messages, and stats keep working regardless.
