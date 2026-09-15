#!/bin/sh
# SAGA operator patch — restore the Slack assistant thread-status indicator.
#
# Why this exists
# ---------------
# Slack's legacy assistant.threads.setStatus accepts FREE-FORM status text
# ("is thinking...", "still working… (2m03s)", live status phrases) and renders
# it as the "…" indicator under a mention in Slack.
#
# Hermes Agent >= v0.21.3 ships slack-sdk >= 3.44, whose Agent Sessions API is
# preferred by plugins/platforms/slack/adapter.py (_session_status_method ->
# agents_sessions_setStatus). But Slack's replacement endpoint only accepts the
# enum statuses active|processing|suspended|closed. Hermes passes its free-form
# phrase, Slack answers {"ok": false, "error": "invalid_arguments", "messages":
# ["must be a valid enum value [json-pointer:/status]"]}, and the adapter
# swallows that at debug level — so the indicator silently stops appearing.
#
# Fix: force _sdk_supports_agent_sessions() to False, which routes the adapter
# back to the legacy assistant.threads.setStatus / assistant.threads.setTitle
# methods (the app manifest already holds assistant:write) and restores the
# indicator with no other behaviour change.
#
# Contract: idempotent, fail-safe, never blocks container boot. If the anchor
# line is missing (upstream refactor) or the patched copy does not compile, the
# adapter is left exactly as shipped and we exit 0.
#
# Remove this patch once upstream stops sending free-form text to
# agents.sessions.setStatus (or when the legacy assistant API is retired).
set -u

ADAPTER="${SAGA_PATCH_ADAPTER:-/opt/hermes/plugins/platforms/slack/adapter.py}"
MARK="SAGA_LEGACY_STATUS_PATCH"

if [ ! -f "$ADAPTER" ]; then
    echo "[saga-patch] $ADAPTER not found — nothing to patch"
    exit 0
fi

if grep -q "$MARK" "$ADAPTER" 2>/dev/null; then
    echo "[saga-patch] already patched — legacy assistant status API in use"
    exit 0
fi

python3 - "$ADAPTER" "$MARK" <<'PY' || echo "[saga-patch] WARNING: patch failed; adapter left untouched"
import os, sys

path, mark = sys.argv[1], sys.argv[2]
anchor = "_AGENT_SESSIONS_SUPPORTED: Optional[bool] = None"

try:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
except OSError as exc:
    print("[saga-patch] cannot read adapter: %s" % exc)
    sys.exit(0)

if anchor not in src:
    print("[saga-patch] anchor line not found (upstream changed?) — skipping")
    sys.exit(0)

patched = src.replace(
    anchor,
    "_AGENT_SESSIONS_SUPPORTED: Optional[bool] = False  # " + mark,
    1,
)

tmp = path + ".saga-patch.tmp"
try:
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(patched)
    compile(patched, path, "exec")  # syntax gate before touching the live file
except Exception as exc:  # never leave a broken adapter behind
    print("[saga-patch] patched copy rejected (%s) — skipping" % exc)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(0)

try:
    mode = os.stat(path).st_mode & 0o7777
    os.replace(tmp, path)
    os.chmod(path, mode)
except OSError as exc:
    print("[saga-patch] could not replace adapter: %s — skipping" % exc)
    sys.exit(0)

print("[saga-patch] patched %s — forced legacy assistant.threads.setStatus path" % path)
PY

exit 0
