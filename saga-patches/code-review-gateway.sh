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
set -u

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
    rm -f "$svc/down" 2>/dev/null || true
done

exit 0
