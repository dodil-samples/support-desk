# Admin portal for Ignite (BYOI / image deployment mode).
#
# Static host + /api proxy to the ADMIN backend (server.mjs) — the admin key
# stays in server env and never reaches the browser. Answers GET /healthz for
# the readiness probe. No build step, no npm install.
#
# Deploy (own FQDN; anonymous ingress, but every proxied call carries ADMIN_KEY
# and the backend enforces it — front with SSO for real deployments):
#   dodil ignite app deploy support-desk-inbox --code ./web/admin --dockerfile-path Dockerfile \
#     --allow-unauthenticated --tier small \
#     --env BACKEND_URL=https://support-desk-admin-<org>.ignite.dodil.cloud/ \
#     --env ADMIN_KEY=ak_choose_a_secret
FROM node:22-alpine
WORKDIR /app
COPY . /app
# Ignite runs pods non-root with a read-only root FS; write only under $HOME.
# The UID must be NUMERIC: Kubernetes' runAsNonRoot can't verify a named user
# (`node`) is non-root and fails with CreateContainerConfigError. `node` = UID 1000.
ENV HOME=/tmp/ignite
USER 1000
# Bind $PORT (Ignite default 80; non-root :80 is permitted on the dev clusters).
CMD ["node", "server.mjs"]
