# Support Desk — dashboard (public anonymous invocation)

A **pure static** dashboard: no build, no server required. Every call is an
anonymous `POST` straight to the app's public FQDN (anonymous access enabled,
permissive CORS):

```
POST https://support-desk-<org>.ignite.dodil.cloud/
content-type: application/json
{ "action": "search_kb", "query": "refund", "top_k": 5 }
```

## What it shows

- **Help center** (public) — `search_kb`, `submit_ticket`, `ticket_status`. Anyone can
  search the KB, open a ticket, and check *their own* ticket (id + the email used).
  Carries the project (public) key if one is configured.
- **Agent inbox** (private) — `stats` + `list_tickets`: open-by-priority, CSAT,
  first-response p90, SLA breaches, and the ticket queue. Requires an **admin key**;
  without it the app returns `401` and the inbox stays locked.

## Run it

Open `index.html`, or serve the folder (also enables the inbound-email webhook):

```bash
node collector.mjs            # dashboard on http://localhost:8788/
# or, dashboard only:
python3 -m http.server 8788   # then open http://localhost:8788/
```

Point it at your app and keys via **⚙ Settings**, or with query params:

```
index.html?app=https://support-desk-<org>.ignite.dodil.cloud/&pk=pk_xxx&ak=ak_xxx
```

## CRM inbound-email webhook (`collector.mjs`)

A CRM / email provider posting an inbound message (no browser) opens a ticket via
the public `submit_ticket`. `collector.mjs` normalises common webhook shapes:

```bash
APP_URL=https://support-desk-<org>.ignite.dodil.cloud/ \
PUBLIC_KEY=pk_xxx node collector.mjs

curl -X POST http://localhost:8788/inbound \
  -H 'content-type: application/json' \
  -d '{"from":"sam@acme.io","subject":"Refund not received","text":"Charged twice for #8891."}'
```

That's the "public backend → CRM email tracking" path: inbound email → public
ticket, no credentials on the caller's side.
