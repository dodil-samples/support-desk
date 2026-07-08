# Static dashboard host for Ignite (BYOI / image deployment mode).
#
# Serves this folder over HTTP on $PORT (Ignite injects PORT, default 80) using the
# zero-dependency collector, which also answers GET /healthz for the readiness probe
# and forwards the CRM inbound-email webhook. No build step, no npm install.
#
# Deploy (own FQDN, anonymous):
#   dodil ignite app deploy support-desk-ui --code ./web --dockerfile-path Dockerfile \
#     --allow-unauthenticated --tier small \
#     --env APP_URL=https://support-desk-<org>.ignite.dodil.cloud/
FROM node:22-alpine
WORKDIR /app
COPY . /app
# Ignite runs pods non-root with a read-only root FS; write only under $HOME.
# The UID must be NUMERIC: Kubernetes' runAsNonRoot can't verify a named user
# (`node`) is non-root and fails with CreateContainerConfigError. `node` = UID 1000.
ENV HOME=/tmp/ignite
USER 1000
# Bind $PORT (Ignite default 80; non-root :80 is permitted on the dev clusters).
CMD ["node", "collector.mjs"]
