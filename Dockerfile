# `v0.22.2` is digest-pinned to the Hermes Agent v0.22.2 stable build (upstream tag
# rc.2-v0.22.2, published 2026-10-15). The stable pipeline also publishes this release
# as `stable`/`latest`, but we pin the explicit version tag so a `stable` re-point
# cannot move this service silently. amd64 manifest digest, verified via Docker Hub.
FROM nousresearch/hermes-agent:v0.22.2@sha256:a11574b7fe8e1631d7d88d556677dac693bc058abf558cdeb899155688c1bf05

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/hermes-railway-entrypoint

# Operator patch hook: s6 cont-init.d runs as root before any supervised service
# starts. See saga-patches/slack-legacy-status.sh for what it does and why it can
# be deleted (restores the Slack assistant thread-status indicator that Hermes
# v0.21.3 drops by sending free-form text to agents.sessions.setStatus, which
# only accepts active|processing|suspended|closed).
COPY --chmod=0755 saga-patches/slack-legacy-status.sh /etc/cont-init.d/020-slack-legacy-status.sh

# Operator patch hook: restores the named-profile gateway slots that Hermes
# v0.21.4's boot reconciler registers DOWN (while the root gateway refuses to
# multiplex inside an s6 container), so the secondary profile's bot survives a
# container restart. Also strips the `--replace` flag from the rendered s6 run
# script, which the host-singleton guard refuses. See saga-patches/code-review-gateway.sh.
COPY --chmod=0755 saga-patches/code-review-gateway.sh /etc/cont-init.d/021-code-review-gateway.sh

ENV HERMES_HOME=/data/.hermes \
    HERMES_WRITE_SAFE_ROOT=/data/.hermes \
    HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages \
    HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_GATEWAY_BOOTSTRAP_STATE=running

ENTRYPOINT ["/usr/local/bin/hermes-railway-entrypoint"]
CMD ["gateway", "run"]
