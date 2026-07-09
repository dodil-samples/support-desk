# Customer portal (public)

The help-center a customer sees: KB search with as-you-type article deflection,
instant ticket submission, and "check my ticket". Static page + a zero-dependency
node server that (a) serves the files, (b) proxies `POST /api` to the **public
backend** with the project key injected server-side, and (c) hosts the CRM
inbound-email webhook (`POST /inbound`).

All configuration is env — there are no URL/key fields in the UI:

```bash
cp .env.example .env   # edit BACKEND_URL (+ PUBLIC_KEY)
set -a; source .env; set +a
node server.mjs        # → http://localhost:8788/
```

Design notes:

- **Submit is instant** — one `submit_ticket` call, which is a pure warehouse
  write (triage runs server-side in the background). The confirmation shows the
  round-trip time and the ticket id.
- **KB suggestions are a separate call** — debounced `search_kb` while the
  customer types, and after submit a best-effort lookup that polls up to 3× in
  case the collection is still indexing. Neither ever blocks submission.
- **Status is an enum** — `new | open | pending | solved`, mirrored from
  `handler.py` `STATUSES` and rendered as friendly labels/pills.
- **Sign-in is optional and adapter-driven** — the page asks `auth_config` and
  only then shows the "Your account" card: email+code (AUTH_MODE=email) or a
  redirect to your IdP (AUTH_MODE=oidc, this server runs the code flow). The
  session lives in an **HttpOnly cookie** set by this server and is injected
  into each proxied call — browser JS never sees a token. Signed-in customers
  get **My tickets** and in-portal replies.

Deploy on Ignite (BYOI): see the [Dockerfile](Dockerfile) header.

Inbound email webhook (CRM / email provider → ticket, no browser):

```bash
curl -X POST http://localhost:8788/inbound \
  -H 'content-type: application/json' \
  -d '{"from":"sam@acme.io","subject":"Refund not received","text":"Charged twice for #8891."}'
```
