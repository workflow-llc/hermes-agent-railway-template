---
name: mobile-change
description: >
  Slack or web change loop for the iOS and Android apps: edit, build, simulator
  proof, pull request, and a separate preview/confirm store upload. Use when a
  Slack thread asks Hermes to change app-ios or android-app, open a mobile PR,
  or ship to TestFlight or Play.
version: 1.0.0
platforms:
  - linux
  - macos
metadata:
  hermes:
    tags: [mobile, slack, ios, android, pr, testflight, play]
---

# mobile-change

Use this skill for the code-review profile when Slack (or the web chat) asks for an iOS or Android product change, a proof PR, or a store upload.

## Change loop

Match the web change loop: implement the request, build, run a simulator, open a PR with a screenshot and a short video, then delete the simulator. Do not upload to TestFlight or Play as part of the change loop.

Repos on the Hermes host:

- iOS: `/data/workspace/app-ios` (`workflow-llc/app-ios`)
- Android: `/data/workspace/android-app` (`workflow-llc/android-app`)

Build and drive the UI with the tools already on that host:

- `lim` builds iOS on a Limrun cloud Mac. `lim xcode build` accepts `--configuration Debug` or `Release` only.
- `agent-device` drives the simulator. `agent-device connect limrun --platform ios` needs `--session`.
- Run `lim ios` commands from the git checkout and pass the instance id. `lim ios delete` takes the id as a positional argument.
- Put PNG and MP4 proof in `docs/pr-media/<slug>/` on the PR branch and link them with absolute `blob/<branch>/...?raw=true` URLs.
- Source Limrun credentials from `/data/.hermes/secrets/limrun.env` inside the shell. Do not print that file, do not copy it into git, and do not echo the key.

## PR body

Write the PR body with this skill's script. Do not call `write_file` or `patch` on `/data/.hermes/profiles/code-review/cache/scratch/pr-body.md`. That path is one file for every session. Hermes keeps a per-task record of files this task has fully read, and `write_file` refuses to overwrite a file this task has not seen. The refusal is reported at the end of the turn by the file-mutation verifier. The shared file is left over from earlier sessions, so a new Slack thread hits it every time.

```bash
RUN_ID="<session id or thread ts>"
SCRIPT="<this skill dir>/scripts/pr_body.py"
python3 "$SCRIPT" --home "$HERMES_HOME" --run-id "$RUN_ID" allocate
python3 "$SCRIPT" --home "$HERMES_HOME" --run-id "$RUN_ID" write <<'EOF'
...markdown...
EOF
gh pr create --body-file "<printed path>"
```

`$HERMES_HOME` for the code-review profile is `/data/.hermes/profiles/code-review`. The body lands at `cache/scratch/runs/<run-id>/pr-body.md`. A second write in the same run must `read` first and pass `--expected-sha256` of that read. A later task uses a new `--run-id`. Do not point `--body-file` at the shared `cache/scratch/pr-body.md`.

## Store upload

Uploading is a second step. Preview and confirm are separate. Production is a third explicit acknowledgement.

```bash
GATE="<this skill dir>/scripts/deploy_gate.py"
python3 "$GATE" --home "$HERMES_HOME" --run-id "$RUN_ID" --repo "$REPO" \
  --platform ios --destination testflight preview
```

Show the JSON to the human. It lists missing credentials and the command it would run. `executed` is false. Nothing has been uploaded.

After the human repeats the same platform and destination, confirm with the `preview_token` from that JSON:

```bash
python3 "$GATE" ... confirm --preview-token "$TOKEN"
```

Confirm still does not upload. It returns `status: ready` only when the destination, the repo lane, and the credential checks pass. `executed` stays false.

Run the upload only when the human has confirmed that destination and `MOBILE_DEPLOY_EXECUTE=1` is already in the service environment. Do not export that variable yourself:

```bash
python3 "$GATE" ... confirm --preview-token "$TOKEN" --execute
```

Destinations:

| Platform | Destination | What it is | Extra requirement |
|---|---|---|---|
| ios | testflight | Fastlane `pilot` or `beta` if the Fastfile has that lane, otherwise `lim upload` | App Store Connect API key id, issuer id, `.p8` path mode 600, and `LIM_HOST_READY=1` or a macOS host |
| ios | appstore | Fastlane `appstore` or `release` only | `production_ack` `PUBLISH_PRODUCTION`. This gate does not pass `--upload-to-appstore` to lim |
| android | internal | `./gradlew bundleRelease` plus Fastlane lane `internal` | Play service-account JSON path, mode 600 |
| android | production | `./gradlew bundleRelease` plus Fastlane lane `production` | `PUBLISH_PRODUCTION` |

If the Fastfile has no matching lane, confirm stays blocked. Do not add a new deploy tool and do not call `fastlane supply` or `pilot` by guessing a lane name.

Credential names (values stay in the environment or in mode-600 files outside git):

- `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY_PATH`
- `PLAY_SERVICE_ACCOUNT_JSON`
- `LIM_HOST_READY=1` after `lim` is connected to a signing host
- `MOBILE_DEPLOY_EXECUTE=1` only after the operator wants the confirm `--execute` path to run

Never write a `.p8`, a Play JSON key, or an API key into the repo, the PR, Slack, or the scratch body. Never say a build was submitted unless this command printed `"executed": true`.

Read `references/store-authorizations.md` before asking anyone for Apple or Play access. Report missing items by name. Do not request that the key material be pasted into the thread.
