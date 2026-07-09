# Support Desk — two portals

The UI is split the way a real ticketing product is:

| | portal | backend it talks to | who |
|---|---|---|---|
| [`portal/`](portal/) | **Help center** — KB search + deflection, instant ticket submission, check-my-ticket, CRM inbound-email webhook | public backend (`APP_ROLE=public`) | customers, anonymous |
| [`admin/`](admin/) | **Agent inbox** — stats, queue, ticket detail with AI reply drafts, status/assign, key management | admin backend (`APP_ROLE=admin`) | agents, admin key |

Both are zero-build static pages, each served by its own zero-dependency node
server that proxies `POST /api` to its backend with the right key injected
**server-side** — configuration is entirely env (`.env.example` in each folder),
there are no URL or key fields in the UI, and no key ever reaches the browser.

Run locally: `cd portal && node server.mjs` / `cd admin && node server.mjs`
(after copying + editing each `.env.example`). Deploy on Ignite: each folder's
`Dockerfile` header has the one-liner.
