# `v0.22.2-desktop` is digest-pinned to the Hermes Agent v0.22.2 stable build with the
# `HERMES_BOT_DESKTOP=1` image variant (upstream tag rc.2-v0.22.2, published 2026-10-15).
# Same Hermes code as `v0.22.2`, plus the Bot Desktop display stack that variant installs:
# tigervnc-standalone-server, the Xfce components (panel/wm/desktop/settings/terminal),
# dbus-x11, x11-utils/xkb, xauth and chromium (~233 MB). DEVS-1068 needs it because the
# plain image ships no Xvnc, so `tools/bot_desktop/runtime.status()` reports the stack
# missing and nothing can start. We pin the explicit version tag so a `stable` re-point
# cannot move this service silently. amd64 manifest digest, verified via Docker Hub.
FROM nousresearch/hermes-agent:v0.22.2-desktop@sha256:81d810941d05343bcd09fa57abfb4d158bffd57c1d85e04ade5e57c2c0794a25

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/hermes-railway-entrypoint

# Bot Screen viewer page (DEVS-1068): a plain-browser noVNC client served by the dashboard at
# /api/plugins/bot-viewer/page. Installed as a bundled dashboard plugin — the image's own
# plugins/ dir is on the same discovery path as kanban/achievements, so no volume surgery and
# no root-owned /data/.hermes/plugins. Static only: the page drives the gateway's existing
# display.observe / display.lease.* RPCs and the /api/display/ws bridge. See bot-viewer/README.md.
COPY bot-viewer /opt/hermes/plugins/bot-viewer

# Bot Desktop (DEVS-1068): the desktop is OFF by default and started on demand — the viewer
# page on load, the tool-boundary hook, or the display.start RPC the human-step flow calls via
# /data/.hermes/scripts/bot_screen_start.py (volume script, not in this image) — then stopped by
# bot_screen_stop.py when the task ends, with `bot_desktop.idle_stop_minutes: 10` as the fallback
# for anything that forgets. Those two config keys live in /data/.hermes/config.yaml (volume), so
# they need no rebuild; this layer only adds the code that enforces the cap.
#
# /opt/saga/browser-gate is on PYTHONPATH: its sitecustomize.py wraps Playwright's
# BrowserType.launch in EVERY python process, so a scratch `chromium.launch()` is refused while a
# headed browser is open or the pid budget is spent — the three-layer cap stops being advice.
COPY saga-patches/browser-gate /opt/saga/browser-gate

# The PYTHONPATH ENV below is NOT enough. The bundled launcher is not a console script: it does
# `unset PYTHONPATH PYTHONHOME; export PYTHONPATH="$REPO:$SITE"` before exec'ing python
# (/opt/hermes/.venv/bin/hermes:16-17 and the same lines in libexec/hermes*), so
# /opt/saga/browser-gate never reaches sys.path in the gateway, the dashboard or any `hermes`
# subcommand — and `sitecustomize` is then never imported, which would leave this gate inert while
# looking installed. `site` DOES import sitecustomize from site-packages, so the shim and the module
# it needs are installed into every interpreter in the image.
RUN set -eu; \
    installed=0; \
    for py in /opt/hermes/.venv/bin/python /opt/hermes/tools/python-*/bin/python3; do \
        [ -x "$py" ] || continue; \
        site="$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" || continue; \
        [ -d "$site" ] || continue; \
        install -m 0644 /opt/saga/browser-gate/saga_browser_gate.py "$site/saga_browser_gate.py"; \
        install -m 0644 /opt/saga/browser-gate/sitecustomize.py "$site/sitecustomize.py"; \
        echo "browser gate: installed into $site"; \
        installed=$((installed + 1)); \
    done; \
    [ "$installed" -gt 0 ] || { \
        echo "browser gate: no interpreter found — refusing to ship an inert gate" >&2; exit 1; }
COPY --chmod=0755 saga-patches/bot-desktop/bot_screen_stop.py /opt/saga/bin/bot_screen_stop.py

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

ENV PYTHONPATH=/opt/saga/browser-gate \
    HERMES_HOME=/data/.hermes \
    HERMES_WRITE_SAFE_ROOT=/data/.hermes \
    HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages \
    HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_GATEWAY_BOOTSTRAP_STATE=running

ENTRYPOINT ["/usr/local/bin/hermes-railway-entrypoint"]
CMD ["gateway", "run"]
