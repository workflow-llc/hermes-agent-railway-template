FROM nousresearch/hermes-agent:v2026.9.21@sha256:4c06bddbcdc164e7ab019c137326990767be79b0f8c90db73d9bce2f3d3e64aa

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/hermes-railway-entrypoint

# Operator patch hook: s6 cont-init.d runs as root before any supervised service
# starts. See saga-patches/slack-legacy-status.sh for what it does and why it can
# be deleted (restores the Slack assistant thread-status indicator that Hermes
# v0.21.3 drops by sending free-form text to agents.sessions.setStatus, which
# only accepts active|processing|suspended|closed).
COPY --chmod=0755 saga-patches/slack-legacy-status.sh /etc/cont-init.d/020-slack-legacy-status.sh

ENV HERMES_HOME=/data/.hermes \
    HERMES_WRITE_SAFE_ROOT=/data/.hermes \
    HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages \
    HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_GATEWAY_BOOTSTRAP_STATE=running

ENTRYPOINT ["/usr/local/bin/hermes-railway-entrypoint"]
CMD ["gateway", "run"]
