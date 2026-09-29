#!/bin/sh
# SAGA_CODE_REVIEW_SLOT_PATCH — restore named-profile gateway slots at container boot.
#
# WHY (upstream regression in Hermes v0.21.4, 2026-09-22):
#   hermes_cli/container_boot.py::reconcile_profile_gateways() now registers EVERY
#   named-profile s6 slot DOWN ("multiplex-only convergence", should_start = False),
#   on the assumption that the root gateway multiplexes every profile. But the root
#   gateway REFUSES to multiplex inside an s6 container
#   (hermes_cli/gateway_migrate.py::_host_supports_migration -> gateway._running_under_s6),
#   and publishes served_profiles: [] + multiplex_standalone_reason instead.
#   Net effect: after any container restart / image bump NOTHING serves the
#   secondary profile and its bot goes dark (@HermesCR was down 5h45m on
#   2026-09-22, 15:08 -> 21:06 UTC). The docs (Docker § per-profile gateway
#   supervision) still document the old, working behaviour: "auto-starts only
#   those whose last recorded state was running".
#
# SECOND TRAP: the run script the reconciler renders hardcodes `--replace`
#   (hermes_cli/service_manager.py::S6ServiceManager._render_run_script). In a
#   multiplex-only build that resolves to REPLACE_HOST against the DEFAULT
#   gateway and fails closed ("Refusing --replace: no valid gateway pid record to
#   prove ownership of PID <root>") -> s6 respawn storm, slot never healthy.
#   (It would SIGTERM the root gateway — taking the default bot down too — if the
#   ownership record ever did match, so this must never be left in place.)
#
# WHAT THIS DOES (runs as root, after 02-reconcile-profiles, before s6-svscan):
#   1. strips `--replace` from each named slot's run script;
#   2. removes the reconciler's `down` marker for profiles whose recorded
#      desired state is `running`, so s6-svscan starts them like any service.
#   cont-init.d runs BEFORE s6-svscan exists, so `s6-svc -u` is not available
#   here — clearing `down` is the only way to express start intent at this stage.
#
# CONTRACT: idempotent, fail-safe, ALWAYS exit 0 — a non-zero cont-init script
#   aborts container boot, and the failure mode of a stale patch must be
#   "nothing happened", never "the container is down".
# DELETE THIS PATCH once upstream starts the slots again (verify with
#   /data/.hermes/logs/container-boot.log showing action=started for the profile).
#
# 2026-09-29: the premise above stopped holding. `gateway.multiplex_profiles: true` on the root
#   config short-circuits the s6 preflight, so the root DOES multiplex now and the slots must stay
#   down. This hook therefore branches on multiplex (see the code below): it registers the slot
#   down instead of starting it, and puts each profile's home/bin on the ROOT slot's PATH (a
#   mirrored route's `deliver: github_comment` shells out of the root process). Retire the whole
#   file — and its COPY line in the Dockerfile — when a named slot is no longer a topology we
#   might revert to.
set -u

# MULTIPLEX (2026-09-29): when the ROOT config sets gateway.multiplex_profiles: true the root
# gateway serves every profile itself and named-profile slots must stay DOWN. Starting one prints
# "The host gateway already serves profile '<name>' - nothing to start" and exits 75, which s6
# treats as restart -> a ~5s respawn loop (~1,300 spawns/hour, most of a core each, +660 KB/h of
# log). So under multiplex this hook does NOT clear the `down` marker; it writes it, and instead
# makes sure the programs a MIRRORED route needs are on the ROOT slot's PATH.
mux=0
if grep -qE '^[[:space:]]*multiplex_profiles:[[:space:]]*true' /data/.hermes/config.yaml 2>/dev/null; then
    mux=1
elif command -v python3 >/dev/null 2>&1; then
    mux=$(python3 -c "
import glob, json
for p in ['/data/.hermes/gateway_state.json'] + sorted(glob.glob('/data/.hermes/profiles/*/gateway_state.json')):
    try:
        d = json.load(open(p))
    except Exception:
        continue
    if len(d.get('served_profiles') or []) > 1:
        print(1); raise SystemExit
print(0)" 2>/dev/null || echo 0)
fi

if [ "$mux" = "1" ]; then
    echo "SAGA_CODE_REVIEW_SLOT_PATCH: multiplex is on - named slots stay down, root slot gets the profile PATHs"
    root_run=/run/service/gateway-default/run
    for state in /data/.hermes/profiles/*/gateway_state.json; do
        [ -f "$state" ] || continue
        prof=$(basename "$(dirname "$state")")
        svc="/run/service/gateway-${prof}"
        [ -d "$svc" ] || continue
        # Registered down BY DESIGN: the marker stops s6-svscan from starting it.
        touch "$svc/down" 2>/dev/null || true
        # A mirrored route's `deliver: github_comment` shells out of the ROOT process, so the
        # profile's gh (in its home/bin) has to be on the ROOT slot's PATH.
        if [ -f "$root_run" ] && [ -x "/data/.hermes/profiles/${prof}/home/bin/gh" ] \
            && ! grep -qF "/data/.hermes/profiles/${prof}/home/bin" "$root_run" 2>/dev/null; then
            sed -i "\|^\. /opt/hermes/.venv/bin/activate|a export PATH=\"/data/.hermes/profiles/${prof}/home/bin:\$PATH\"" \
                "$root_run" 2>/dev/null || true
            chmod 0755 "$root_run" 2>/dev/null || true
        fi
    done
    exit 0
fi

for state in /data/.hermes/profiles/*/gateway_state.json; do
    [ -f "$state" ] || continue
    prof=$(basename "$(dirname "$state")")
    svc="/run/service/gateway-${prof}"
    [ -d "$svc" ] || continue

    # Last recorded run intent: an explicit desired_state wins; a file that only
    # has the runtime gateway_state treats the transient draining/degraded states
    # as "was running" (same rule the boot reconciler uses).
    desired=$(grep -o '"desired_state"[[:space:]]*:[[:space:]]*"[^"]*"' "$state" 2>/dev/null \
        | head -1 | sed 's/.*:[[:space:]]*"//; s/"$//')
    runtime=$(grep -o '"gateway_state"[[:space:]]*:[[:space:]]*"[^"]*"' "$state" 2>/dev/null \
        | head -1 | sed 's/.*:[[:space:]]*"//; s/"$//')
    start=0
    case "$desired" in
        running) start=1 ;;
        "") case "$runtime" in running|draining|degraded) start=1 ;; esac ;;
    esac
    [ "$start" = "1" ] || continue

    if grep -q 'gateway run --replace' "$svc/run" 2>/dev/null; then
        sed -i 's/ gateway run --replace/ gateway run/g' "$svc/run" 2>/dev/null || true
        chmod 0755 "$svc/run" 2>/dev/null || true
    fi
    # gh must be resolvable by the GATEWAY process: the webhook route's `deliver:
    # github_comment` step shells out to `gh`, and the profile's binary lives in
    # its home/bin, which is on the AGENT's login PATH (~/.profile) but not on the
    # gateway's. Without this, PR reviews stop posting their summary comment while
    # inline findings still land ("'gh' CLI not found" in gateway.log).
    if [ -x "/data/.hermes/profiles/${prof}/home/bin/gh" ] \
        && ! grep -qF "/data/.hermes/profiles/${prof}/home/bin" "$svc/run" 2>/dev/null; then
        sed -i "\|^\. /opt/hermes/.venv/bin/activate|a export PATH=\"/data/.hermes/profiles/${prof}/home/bin:\$PATH\"" \
            "$svc/run" 2>/dev/null || true
        chmod 0755 "$svc/run" 2>/dev/null || true
    fi
    rm -f "$svc/down" 2>/dev/null || true
done

exit 0
