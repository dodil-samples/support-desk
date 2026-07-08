# Support Desk (Python) — a shared inbox on Ignite + K3

A Zendesk-style ticketing backend where **K3 is the entire datastore** and **Ignite**
is the compute + models. One Python function, action-routed.

- **Warehouse (SQL tables):** `tickets` (status, priority, SLA timestamps, CSAT) and
  `messages`. Every list/stat is a SQL query — no separate analytics DB.
- **Objects (S3):** full message bodies at `tickets/{id}/messages/{mid}.txt`, KB
  articles at `kb/{slug}.md`.
- **Vector:** a `kb` collection embeds KB articles for **suggested answers** and
  similar-ticket recall.
- **Models:** `kimi-k2.6` triages each ticket (category / priority / sentiment) and
  drafts replies grounded in KB hits.

## Architecture

```
invoke ──► handler.py (router on `action`)
              ├─ lib/auth.py     service account ─► bearer (OIDC client_credentials)
              ├─ lib/k3.py       objects · tables (_execute/insert/merge) · vector/search
              ├─ lib/models.py   /v1/chat/completions · /v1/embeddings
              └─ bootstrap.py    idempotent bucket + tables + kb collection
```

## Public vs private backend

The app is meant to be deployed with **anonymous invocation enabled** so its public
FQDN can be called with no Dodil credentials — a help-center widget, a website
contact form, or a CRM inbound-email webhook just `POST`s to it (see [`web/`](web/)).
An in-app **gate** ([`lib/gate.py`](lib/gate.py)) is therefore the trust boundary and
splits actions into two tiers:

- **PUBLIC** (customer-facing): `submit_ticket`, `ticket_status`, `search_kb`. A
  public caller can open a ticket, check *their own* ticket (id + the email they
  used), and search the KB — nothing else. Optionally gated by a **project key**.
- **PRIVATE** (admin key): the whole agent inbox — list/triage/reply/assign/stats/
  export — plus key management. Gated by an **admin key**.

Keys are provided in two ways, merged — env (`PUBLIC_KEYS` / `ADMIN_KEYS`,
comma-separated, provisioned by the IAM-authenticated operator at deploy) and the
`api_keys` table managed at runtime via `create_key` / `list_keys` / `revoke_key`
(the "user management" bullet). Keys travel in the request **body** (`key` field),
because the anon FQDN's CORS preflight only allows the `content-type` header.
**Graceful default:** a tier with no key configured (env + table both empty) is
open, so existing `dodil ignite invoke` calls keep working — set a key to lock a
tier down.

## Actions

| action | tier | payload | does |
|---|---|---|---|
| `submit_ticket` | 🟢 public | subject, body, requester_email, channel | open a ticket (triage + KB suggestions); returns ticket_id + a status token |
| `ticket_status` | 🟢 public | ticket_id, requester_email | your own ticket's status + messages (email must match) |
| `search_kb` | 🟢 public | query, top_k | semantic KB search |
| `create_ticket` | 🔒 admin | subject, body, requester_email, channel | new ticket → auto-triage → store first message → suggest KB (+ duplicate hints) |
| `add_message` | 🔒 admin | ticket_id, role, author, body | append message (S3 + row); agent reply sets `first_response_at` |
| `triage` | 🔒 admin | ticket_id | (re)classify category/priority/sentiment via LLM |
| `suggest_reply` | 🔒 admin | ticket_id | KB vector search → LLM-drafted grounded reply |
| `add_kb` | 🔒 admin | title, body | store + embed a KB article |
| `get_ticket` | 🔒 admin | ticket_id | ticket + its messages |
| `list_tickets` | 🔒 admin | status?/category?/assignee?/priority?/limit? | warehouse filter |
| `set_status` / `assign` / `rate` | 🔒 admin | ticket_id (+status/assignee/csat) | mutate ticket |
| `stats` | 🔒 admin | — | open-by-priority, by-category, volume/day, CSAT, SLA |
| `create_key` / `list_keys` / `revoke_key` | 🔒 admin | kind?, label? / — / revoke | manage project + admin keys |

## Deploy

```bash
# 1) a service account for the function (K3 write + model access)
SA=$(dodil auth service-account create support-desk-sa -o json)
SA_ID=$(echo "$SA" | jq -r '.data.id // .id')
SA_SECRET=$(echo "$SA" | jq -r '.data.secret // .secret')
dodil auth service-account grant-role "$SA_ID" k3-authorization-service k3.admin

# 2) deploy (compile-mode python)
dodil ignite app deploy support-desk --code ./support-desk --runtime python --tier small \
  --allow-unauthenticated \
  --env DODIL_SA_ID="$SA_ID" --env DODIL_SA_SECRET="$SA_SECRET" \
  --env ADMIN_KEYS="ak_choose_a_secret" --env PUBLIC_KEYS="pk_help_widget"
```

`--allow-unauthenticated` makes the public FQDN anonymously invokable (CORS-open) so
the static [dashboard](web/) and CRM inbound-email webhook can call it with no
credentials. **Set `ADMIN_KEYS` whenever you enable anonymous access** — otherwise
the agent inbox is open to anyone. The dashboard lives in [`web/`](web/).

## Invoke

```bash
dodil ignite invoke support-desk --payload '{"action":"create_ticket","subject":"Refund not received","body":"Charged twice for order #8891.","requester_email":"sam@acme.io","channel":"email"}'

dodil ignite invoke support-desk --payload '{"action":"add_kb","title":"Refund policy","body":"Duplicate charges are auto-refunded within 3 business days."}'

dodil ignite invoke support-desk --payload '{"action":"suggest_reply","ticket_id":"t_xxxxxxxxxxxx"}'

dodil ignite invoke support-desk --payload '{"action":"stats"}'
```

## Notes
- **Zero pip dependencies** — stdlib only (`urllib`), so it compiles/deploys cleanly.
- Fresh writes are read-your-writes on single-table reads (K3 defaults to
  `FRESHNESS_STRONG`); this app's reads are single-table, so no `compact` needed.
- Vector calls degrade to an empty result if the embedder is unavailable — tickets,
  messages, and stats keep working regardless.
