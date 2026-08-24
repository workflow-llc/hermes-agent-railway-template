FROM nousresearch/hermes-agent:v2026.8.19@sha256:f3cba6abf5ed80d47a271498d663ace5dda87f45000552afb8be8370a35df1b5

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/hermes-railway-entrypoint

ENV HERMES_HOME=/data/.hermes \
    HERMES_WRITE_SAFE_ROOT=/data/.hermes \
    HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages \
    HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_GATEWAY_BOOTSTRAP_STATE=running

ENTRYPOINT ["/usr/local/bin/hermes-railway-entrypoint"]
CMD ["gateway", "run"]
