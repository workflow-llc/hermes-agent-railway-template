# `v0.22.5-desktop` is digest-pinned to the Hermes Agent v0.22.5 stable build with the
# `HERMES_BOT_DESKTOP=1` image variant (upstream desktop variant of the v0.22.5 stable
# release, rc.3 candidate, published 2026-10-23). Rolled up from v0.22.2: v0.22.3
# (~620 PRs), v0.22.4 (13 PRs) and v0.22.5 (81 PRs) — v0.22.5 is the first release with
# pluggable delegation backends (`ctx.register_delegation_provider`), which the
# advanced-delegation plugin (profile_routes / model_map) requires; the plugin declares
# `requires_hermes: >=0.22.5` and v0.22.2 refuses to install it.
# Same Hermes code as `v0.22.5`, plus the Bot Desktop display stack that variant installs:
# tigervnc-standalone-server, the Xfce components (panel/wm/desktop/settings/terminal),
# dbus-x11, x11-utils/xkb, xauth and chromium (~233 MB). DEVS-1068 needs it because the
# plain image ships no Xvnc, so `tools/bot_desktop/runtime.status()` reports the stack
# missing and nothing can start. We pin the explicit version tag so a `stable` re-point
# cannot move this service silently. amd64 manifest digest, verified via Docker Hub.
FROM nousresearch/hermes-agent:v0.22.5-desktop@sha256:b7837e696eebce08495f6984a79a0a6a7ef696af746d407debff26d88473a389

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/hermes-railway-entrypoint

# Bot Screen viewer page (DEVS-1068): a plain-browser noVNC client served by the dashboard at
# /api/plugins/bot-viewer/page. Installed as a bundled dashboard plugin — the image's own
# plugins/ dir is on the same discovery path as kanban/achievements, so no volume surgery and
# no root-owned /data/.hermes/plugins. Static only: the page drives the gateway's existing
# display.observe / display.lease.* RPCs and the /api/display/ws bridge. See bot-viewer/README.md.
COPY bot-viewer /opt/hermes/plugins/bot-viewer

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
