# Hermes Agent Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on Railway using the official Nous Research image, dashboard, and s6 process supervision. A minimal compatibility entrypoint maps variables from existing template deployments before handing control to the upstream `/init` process.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent)

## Railway configuration

The Dockerfile is pinned to this official image:

```text
nousresearch/hermes-agent:v0.22.5-desktop@sha256:b7837e696eebce08495f6984a79a0a6a7ef696af746d407debff26d88473a389
```

`v0.22.5-desktop` (digest-pinned) is Hermes Agent v0.22.5 in its `HERMES_BOT_DESKTOP=1` variant — identical Hermes code, plus the Bot Desktop display stack (tigervnc-standalone-server, Xfce components, dbus-x11, xauth, chromium; ~233 MB) that the Bot Screen needs against a headless Railway host (DEVS-1068). It rolls up everything since our previous pin (v0.22.2): v0.22.3 (~620 PRs), v0.22.4 (13 PRs) and v0.22.5 (81 PRs). v0.22.5 matters to us because it is the first release with pluggable delegation backends (`ctx.register_delegation_provider`), so the `advanced-delegation` plugin (profile routes / model map) can install — it declares `requires_hermes: >=0.22.5`, which v0.22.2 refused. The explicit tag is pinned so a `stable`/`latest` re-point cannot move this service silently; the digest is the linux/amd64 manifest entry. The compatibility entrypoint delegates to this release's upstream entrypoint dispatcher so both normal PID-1 startup and Railway runtimes with an init wrapper are supported.
Note for upgrades: v0.22.3-v0.22.5 are patch releases on the v0.22.0 "Threshold Release" line; v0.22.2 already carried the gateway-after-reboot fix (#133744). Re-verify the `saga-patches/` boot hooks and the multiplex topology after the roll-out.

Use these service settings:

| Setting | Value |
|---|---|
| Start command | `/usr/local/bin/hermes-railway-entrypoint gateway run` |
| Volume mount | `/data` |
| Public port | `8080` |
| Health-check path | `/api/status` |
| Health-check timeout | `900` s (`railway.toml`) |
| Replicas | `1` |

Railway continues building this GitHub repository so existing template consumers receive update notifications. The compatibility entrypoint only translates legacy environment variables, then executes the official image's `/init`; s6-overlay still supervises the Hermes gateway and dashboard.

### Variables

```dotenv
PORT=8080
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<generated-password>
```

The compatibility entrypoint maps the existing `ADMIN_USERNAME` and sealed `ADMIN_PASSWORD` values to the official dashboard variables before starting `/init`. Existing deployments therefore keep the same login without adding, copying, revealing, or re-entering credentials. It does not modify `config.yaml`. If `ADMIN_PASSWORD` is absent, it generates and logs a password as the previous template did.

For zero-interaction migration, a stable 32-byte dashboard session-signing secret is derived from `ADMIN_PASSWORD`. Derivation supports legacy passwords shorter than Hermes's 16-byte minimum signing-key length while preserving the existing login. After migration, operators may add `HERMES_DASHBOARD_BASIC_AUTH_SECRET` as an independent sealed value containing at least 32 random bytes. Rotating only this secret logs dashboard users out; it does not change the dashboard password or Hermes data. Neither the password nor the derived secret is written to `config.yaml`.

The Dockerfile supplies the remaining official variables:

```dotenv
HERMES_HOME=/data/.hermes
HERMES_WRITE_SAFE_ROOT=/data/.hermes
HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages
HERMES_DASHBOARD=1
HERMES_DASHBOARD_HOST=0.0.0.0
HERMES_GATEWAY_BOOTSTRAP_STATE=running
```

The entrypoint maps `PORT` to `HERMES_DASHBOARD_PORT` at runtime.

Provider credentials, messaging channels, models, skills, profiles, and gateway state are managed through the official dashboard and persisted under `/data/.hermes`.

## Local patches

`saga-patches/` holds operator patches applied on top of the pinned upstream image. They are copied into `/etc/cont-init.d/`, so they run as root on every container start, before any supervised service. Each one is idempotent, must exit 0 unconditionally (a non-zero cont-init script aborts the container), and verifies its own edit before touching the installed tree.

| Patch | Purpose | Delete when |
|---|---|---|
| `slack-legacy-status.sh` | Forces the Slack adapter onto the legacy `assistant.threads.setStatus` / `setTitle` methods. Hermes v0.21.3 ships slack-sdk ≥ 3.44 and prefers `agents.sessions.setStatus`, but that endpoint only accepts the enum `active\|processing\|suspended\|closed`; Hermes sends free-form phrases ("is thinking..."), Slack answers `invalid_arguments`, the adapter swallows it, and the Slack thread indicator disappears. | Upstream stops sending free-form text to `agents.sessions.setStatus`, or Slack retires the legacy assistant API. |
| `code-review-gateway.sh` | Restores the named-profile gateway slots at boot. Hermes v0.21.4's `container_boot.reconcile_profile_gateways` registers every named slot DOWN (multiplex-only convergence) while the root gateway refuses to multiplex inside an s6 container (`gateway_migrate._host_supports_migration`), so nothing serves the secondary profile and its bot goes dark on every container restart. Also strips the `--replace` flag the rendered run script carries, which the host-singleton guard refuses (respawn storm). Under multiplex `(gateway.multiplex_profiles: true` on this host's root config) the hook only keeps the slots parked down and injects the profile `home/bin` onto the root slot's `PATH`. | Upstream's boot reconciler starts named slots again — i.e. `/data/.hermes/logs/container-boot.log` shows `action=started` for the profile on a fresh boot — or the root slot's rendered run script stops needing the profile `PATH` injection. |

## Upgrading Hermes

Update the pinned release and digest in `Dockerfile` deliberately after reviewing the upstream [release notes](https://github.com/NousResearch/hermes-agent/releases) and validating the new image. Do not use `latest`. Because the template remains GitHub-backed, merging an upgrade to the default branch notifies existing template consumers.

See the official [Hermes Docker documentation](https://hermes-agent.nousresearch.com/docs/user-guide/docker) for image behavior and configuration details.
