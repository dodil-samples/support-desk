# Admin portal (private) — the agent inbox

Stats tiles, priority breakdown, the ticket queue with a status-enum filter, a
ticket detail pane (message timeline, AI-drafted replies via `suggest_reply`,
send reply, set status / assign), and API-key management.

Static page + a zero-dependency node server that proxies `POST /api` to the
**admin backend** with the admin key injected server-side — the key never
reaches the browser (nothing in localStorage, no settings UI). All configuration
is env:

```bash
cp .env.example .env   # edit BACKEND_URL + ADMIN_KEY
set -a; source .env; set +a
node server.mjs        # → http://localhost:8789/
```

The status dropdowns mirror the backend enum (`new | open | pending | solved`,
`STATUSES` in `handler.py`) — the backend rejects anything else, so the two
can't drift silently.

Deploy on Ignite (BYOI): see the [Dockerfile](Dockerfile) header. The sample
keeps the perimeter simple (key held server-side, backend enforces it); a real
deployment would put SSO/IdP in front of this app's FQDN.
