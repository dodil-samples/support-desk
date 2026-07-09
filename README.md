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
              handler.py (router on `action`)
                 ├─ lib/gate.py    public/private tiers + APP_ROLE + API keys
                 ├─ lib/auth.py    service account ─► bearer (OIDC client_credentials)
                 ├─ lib/k3.py      objects · tables (_execute/insert/merge) · vector/search
                 ├─ lib/models.py  /v1/chat/completions · /v1/embeddings
                 └─ bootstrap.py   idempotent bucket + tables + kb collection
```

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
query while the fresh vector collection loads). The portal's deflection search
and the inbox's ✨ *Draft with AI* (`suggest_reply`, grounded in KB hits) both
work from this.

### Teardown

```bash
for app in support-desk-public support-desk-admin support-desk-portal support-desk-inbox; do
  dodil ignite app delete $app; done
dodil k3 bucket delete support-desk
dodil auth service-account list          # find the SA's uuid
dodil auth service-account delete <uuid> # don't leave a dangling credential
```

---

## Actions

| action | tier | payload | does |
|---|---|---|---|
| `submit_ticket` | 🟢 public | subject, body, requester_email, channel | pure warehouse write, returns instantly with ticket_id + status token; LLM triage runs in the background (KB suggestions are the portal's separate `search_kb` call) |
| `ticket_status` | 🟢 public | ticket_id, requester_email | your own ticket's status + messages (email must match; PII not echoed) |
| `search_kb` | 🟢 public | query, top_k | semantic KB search |
| `create_ticket` | 🔒 admin | subject, body, requester_email, channel | agent-filed ticket with **inline** triage (staff can afford the wait) + KB + duplicate hints |
| `add_message` | 🔒 admin | ticket_id, role, author, body | append message (S3 + row); agent reply sets `first_response_at` |
| `triage` | 🔒 admin | ticket_id | (re)classify category/priority/sentiment via LLM |
| `suggest_reply` | 🔒 admin | ticket_id | KB vector search → LLM-drafted grounded reply |
| `add_kb` | 🔒 admin | title, body | store + embed a KB article |
| `get_ticket` | 🔒 admin | ticket_id | ticket + its messages |
| `list_tickets` | 🔒 admin | status?/category?/assignee?/priority?/limit? | warehouse filter (status must be in the enum) |
| `set_status` / `assign` / `rate` | 🔒 admin | ticket_id (+status/assignee/csat) | mutate ticket (`status` must be in the enum) |
| `stats` | 🔒 admin | — | open-by-priority, by-category, volume/day, CSAT, first-response p90, SLA |
| `export_tickets` | 🔒 admin | same filters as list | CSV export |
| `create_key` / `list_keys` / `revoke_key` | 🔒 admin | kind?, label? / — / revoke | manage project + admin keys at runtime |

Keys ride in the request **body** (`key` field); the portals inject them
server-side so browsers never carry them. Runtime keys (the `api_keys` table)
merge with the env keys.

## Performance (measured on dev)

| path | latency |
|---|---|
| `submit_ticket` (warm) | **0.5–1 s** |
| first call on a cold replica (bootstrap provisions bucket/tables) | 12–15 s, once |
| background triage lands (`moonshot-v1-auto`) | ≤5 s after submit |
| `search_kb` warm / first query on idle collection | ~2 s / up to ~25 s |
| `suggest_reply` (draft grounded in KB) | ~4–8 s |

Two env knobs: **`MODEL_NAME`** (default `moonshot-v1-auto`; a reasoning model
like `kimi-k2.6` writes richer drafts but takes ~25–35 s *per call* — with
background triage that only slows admin actions, never the public form) and
**`K3_VECTOR_TIMEOUT`** (default `20` s per attempt, tried twice; `5`–`8` is a
friendlier ceiling now that the portal polls instead of waiting). For a
demo-snappy public backend keep a warm replica: `--reserved 1` (billed
continuously).

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
