#!/bin/sh
set -eu

dashboard_username="${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:-${ADMIN_USERNAME:-admin}}"
dashboard_password="${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:-${ADMIN_PASSWORD:-}}"

if [ -z "$dashboard_password" ]; then
    dashboard_password="$(python -c 'import secrets; print(secrets.token_urlsafe(16))')"
    echo "Generated admin password: $dashboard_password"
fi

dashboard_secret="${HERMES_DASHBOARD_BASIC_AUTH_SECRET:-}"
if [ -z "$dashboard_secret" ]; then
    dashboard_secret="$(
        ADMIN_PASSWORD="$dashboard_password" python -c \
            'import base64, hashlib, os; print(base64.b64encode(hashlib.sha256(("hermes-dashboard-session:" + os.environ["ADMIN_PASSWORD"]).encode()).digest()).decode())'
    )"
fi

export HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-${PORT:-8080}}"
export HERMES_DASHBOARD_BASIC_AUTH_USERNAME="$dashboard_username"
export HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="$dashboard_password"
export HERMES_DASHBOARD_BASIC_AUTH_SECRET="$dashboard_secret"

# --- Bot Desktop Chromium flags: browser.chromium_args -> AGENT_BROWSER_ARGS -------------------
# AGENT_BROWSER_ARGS is agent-browser's documented launch-flag env ("--args <args> ... (or
# AGENT_BROWSER_ARGS)"), and tools/browser_tool_session.py::_apply_chromium_sandbox_args leaves it
# untouched when it is already set. That makes it the one knob that reaches the agent's headed
# Chromium without patching the image's copy of Hermes. The sandbox-bypass flags are ALWAYS
# included: they are what lets Chromium start as uid 10000 in a container, and setting the
# variable at all suppresses the code that would otherwise add them.
#
# Measured on DEVS-1068 (one headed Chromium, about:blank): 180 threads with Playwright's default
# flags, 114 with the set below; with a real page and a login form, 341 before the trim. cgroup
# pids count threads, so this is what keeps one session at ~+145 of a 1000 cap instead of ~+270.
# The dock's Browser icon (tools/bot_desktop/browser.py::dock_argv) does not read env and keeps
# Playwright's defaults; the agent's browse path — the one that costs the pids — is covered.
if [ -z "${AGENT_BROWSER_ARGS:-}" ]; then
    AGENT_BROWSER_ARGS="$(HERMES_HOME_FOR_ARGS="${HERMES_HOME:-/data/.hermes}" /opt/hermes/.venv/bin/python - <<'PY_CONFIG'
import os
import shlex
import sys
from pathlib import Path

base = ["--no-sandbox", "--disable-dev-shm-usage"]
extra = []
try:
    # hermes_yaml, not `yaml`: the venv has no PyYAML, and a silent ImportError here would ship the
    # image with the knob missing while looking like it worked. hermes_yaml is the repo's own
    # YAML-1.1 reader (ruamel), the same one the config loader uses.
    sys.path.insert(0, "/opt/hermes")
    import hermes_yaml
    cfg_path = Path(os.environ["HERMES_HOME_FOR_ARGS"]) / "config.yaml"
    cfg = hermes_yaml.safe_load(cfg_path.read_text()) or {}
    raw = (cfg.get("browser") or {}).get("chromium_args") or []
    if isinstance(raw, str):
        raw = shlex.split(raw)  # a flag list written as one string, not one flag per character
    elif not isinstance(raw, (list, tuple)):
        raise TypeError(f"chromium_args must be a list or a string, got {type(raw).__name__}")
    extra = [str(a) for a in raw]
except Exception as exc:  # noqa: BLE001 - boot must not fail over a flag list
    print(f"bot-desktop chromium flags: could not read browser.chromium_args ({exc}); "
          "using sandbox-bypass flags only", file=sys.stderr)
seen, out = set(), []
for arg in base + extra:
    if arg not in seen:
        seen.add(arg)
        out.append(arg)
print(",".join(out))
PY_CONFIG
)"
    export AGENT_BROWSER_ARGS
    echo "bot-desktop chromium flags: $AGENT_BROWSER_ARGS"
fi

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
