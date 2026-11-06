#!/bin/sh
# Install the mobile-change skill onto the Hermes home and each named profile.
#
# The code-review profile's agent-written skill used one shared
# cache/scratch/pr-body.md. Hermes write_file refuses to overwrite that file
# when the Slack session has not read it. This copy is the per-run writer and
# the preview/confirm store gate. It does not read or write secrets.
#
# Idempotent. Always exits 0 so a missing volume cannot abort container boot.
set -u

SRC="${SAGA_MOBILE_SKILL_SRC:-/opt/hermes/saga-skills/mobile-change}"
HOME_DIR="${SAGA_HERMES_HOME:-/data/.hermes}"

if [ ! -f "$SRC/SKILL.md" ]; then
    echo "[saga-patch] mobile-change source missing ($SRC) — skipping"
    exit 0
fi
if [ ! -d "$HOME_DIR" ]; then
    echo "[saga-patch] $HOME_DIR is not mounted — skipping mobile-change install"
    exit 0
fi

install_one() {
    root=$1
    [ -d "$root" ] || return 0
    dest="$root/skills/mobile-change"
    rm -rf "$dest"
    mkdir -p "$root/skills"
    cp -a "$SRC" "$dest"
    find "$dest" -type d -exec chmod 0755 {} \;
    find "$dest" -type f -exec chmod 0644 {} \;
    find "$dest/scripts" -type f -name '*.py' -exec chmod 0755 {} \;
    echo "[saga-patch] installed mobile-change into $dest"
}

install_one "$HOME_DIR"
for prof in "$HOME_DIR"/profiles/*; do
    [ -d "$prof" ] || continue
    case "$prof" in
        *'/secrets'|*'secrets') continue ;;
    esac
    install_one "$prof"
done

exit 0
