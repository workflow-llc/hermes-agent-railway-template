"""Preview versus confirm for Slack-triggered iOS and Android store uploads.

This plans commands the repositories already use (Fastlane lanes when a
Fastfile names them, otherwise the Limrun upload path for iOS TestFlight and
Gradle ``bundleRelease`` as an Android artifact step). It does not upload
anything unless ``execute=True`` and ``MOBILE_DEPLOY_EXECUTE=1``. Credential
files are checked for presence and mode only. Their bytes are never read.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from mobile_change.pr_body import allocate, validate_run_id

PRODUCTION_ACK = "PUBLISH_PRODUCTION"
EXECUTE_ENV = "MOBILE_DEPLOY_EXECUTE"
LIM_HOST_ENV = "LIM_HOST_READY"

IOS_TESTFLIGHT_LANES = ("pilot", "beta")
IOS_APPSTORE_LANES = ("appstore", "release")
ANDROID_INTERNAL_LANES = ("internal",)
ANDROID_PRODUCTION_LANES = ("production",)

DESTINATIONS = {
    ("ios", "testflight"): "testing",
    ("ios", "appstore"): "production",
    ("android", "internal"): "testing",
    ("android", "production"): "production",
}

_LANE = re.compile(r"^\s*lane\s+:([A-Za-z0-9_]+)\b", re.MULTILINE)


class DeployGateError(Exception):
    """The plan could not be built."""


@dataclass
class DeployPlan:
    status: str
    platform: str
    destination: str
    stage: str
    executed: bool
    missing: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    preview_token: str | None = None
    checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "platform": self.platform,
            "destination": self.destination,
            "stage": self.stage,
            "executed": self.executed,
            "missing": list(self.missing),
            "commands": [list(cmd) for cmd in self.commands],
            "notes": list(self.notes),
            "preview_token": self.preview_token,
            "checks": dict(self.checks),
        }


Runner = Callable[[list[str], Path], int]


def _lane_names(repo: Path) -> set[str]:
    fastfile = repo / "fastlane" / "Fastfile"
    if not fastfile.is_file():
        return set()
    return set(_LANE.findall(fastfile.read_text(encoding="utf-8", errors="replace")))


def _env_present(name: str, env: Mapping[str, str]) -> str:
    return "present" if env.get(name, "").strip() else "missing"


def _file_status(path_value: str | None) -> str:
    if not path_value or not path_value.strip():
        return "missing"
    path = Path(path_value)
    try:
        info = path.lstat()
    except OSError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "symlink_refused"
    if not stat.S_ISREG(info.st_mode):
        return "not_a_file"
    if info.st_mode & 0o077:
        return "mode_too_open"
    return "present"


def _ios_checks(env: Mapping[str, str]) -> dict[str, str]:
    return {
        "ASC_KEY_ID": _env_present("ASC_KEY_ID", env),
        "ASC_ISSUER_ID": _env_present("ASC_ISSUER_ID", env),
        "ASC_PRIVATE_KEY_PATH": _file_status(env.get("ASC_PRIVATE_KEY_PATH")),
        LIM_HOST_ENV: "ready" if env.get(LIM_HOST_ENV) == "1" or sys.platform == "darwin" else "missing",
    }


def _android_checks(env: Mapping[str, str]) -> dict[str, str]:
    return {
        "PLAY_SERVICE_ACCOUNT_JSON": _file_status(env.get("PLAY_SERVICE_ACCOUNT_JSON")),
    }


def _commands_for(repo: Path, platform: str, destination: str) -> tuple[list[list[str]], list[str], list[str]]:
    """Return argv list, notes, and missing-toolchain tokens."""
    lanes = _lane_names(repo)
    notes: list[str] = []
    missing: list[str] = []
    commands: list[list[str]] = []
    gradlew = repo / "gradlew"

    if platform == "ios" and destination == "testflight":
        lane = next((name for name in IOS_TESTFLIGHT_LANES if name in lanes), None)
        if lane:
            commands.append(["bundle", "exec", "fastlane", lane])
            notes.append(f"Fastfile lane :{lane} is the TestFlight upload.")
        else:
            commands.append(["lim", "upload"])
            notes.append(
                "No Fastfile pilot/beta lane. TestFlight ship path is lim upload, "
                "the command the Slack mobile flow already uses. App Store upload is a different destination."
            )
        return commands, notes, missing

    if platform == "ios" and destination == "appstore":
        lane = next((name for name in IOS_APPSTORE_LANES if name in lanes), None)
        if lane:
            commands.append(["bundle", "exec", "fastlane", lane])
            notes.append(
                f"Fastfile lane :{lane} is the only App Store command this gate will run. "
                "lim upload is not reused for production."
            )
        else:
            missing.append("appstore_lane")
            notes.append(
                "No :appstore or :release lane in fastlane/Fastfile. "
                "App Store submission stays disabled. This gate will not pass --upload-to-appstore to lim."
            )
        return commands, notes, missing

    if platform == "android" and destination == "internal":
        if gradlew.is_file():
            commands.append(["./gradlew", "bundleRelease"])
            notes.append("Artifact step is the repo Gradle bundleRelease task.")
        lane = next((name for name in ANDROID_INTERNAL_LANES if name in lanes), None)
        if lane:
            commands.append(["bundle", "exec", "fastlane", lane])
            notes.append(f"Fastfile lane :{lane} is the Play internal-track upload.")
        else:
            missing.append("internal_lane")
            notes.append(
                "No :internal lane in fastlane/Fastfile. bundleRelease can build an AAB. "
                "Play upload stays disabled until that lane exists. supply is not invented here."
            )
        return commands, notes, missing

    if platform == "android" and destination == "production":
        if gradlew.is_file():
            commands.append(["./gradlew", "bundleRelease"])
        lane = next((name for name in ANDROID_PRODUCTION_LANES if name in lanes), None)
        if lane:
            commands.append(["bundle", "exec", "fastlane", lane])
            notes.append(f"Fastfile lane :{lane} is the Play production upload.")
        else:
            missing.append("production_lane")
            notes.append(
                "No :production lane in fastlane/Fastfile. Production publish stays disabled."
            )
        return commands, notes, missing

    raise DeployGateError(f"unsupported platform/destination {platform}/{destination}")


def _tools_missing(commands: list[list[str]], repo: Path, env: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    search = env.get("PATH")
    for argv in commands:
        head = argv[0]
        if head == "./gradlew":
            if not os.access(repo / "gradlew", os.X_OK):
                missing.append("tool:gradlew")
        elif shutil.which(head, path=search) is None:
            missing.append(f"tool:{head}")
    return missing


def _credential_missing(platform: str, checks: dict[str, str]) -> list[str]:
    missing = []
    if platform == "ios":
        for name in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", LIM_HOST_ENV):
            if checks[name] not in {"present", "ready"}:
                missing.append(f"{name}:{checks[name]}")
    else:
        state = checks["PLAY_SERVICE_ACCOUNT_JSON"]
        if state != "present":
            missing.append(f"PLAY_SERVICE_ACCOUNT_JSON:{state}")
    return missing


def _preview_path(home: Path, run_id: str) -> Path:
    return allocate(home, run_id).parent / "deploy-preview.json"


def preview(
    *,
    home: Path,
    run_id: str,
    repo: Path,
    platform: str,
    destination: str,
    env: Mapping[str, str] | None = None,
) -> DeployPlan:
    validate_run_id(run_id)
    key = (platform, destination)
    if key not in DESTINATIONS:
        raise DeployGateError(
            "destination must be ios/testflight, ios/appstore, android/internal, or android/production"
        )
    if not repo.is_dir():
        raise DeployGateError(f"repo directory does not exist: {repo}")
    env = env if env is not None else os.environ
    commands, notes, missing_tools = _commands_for(repo, platform, destination)
    checks = _ios_checks(env) if platform == "ios" else _android_checks(env)
    missing = list(missing_tools)
    missing.extend(_tools_missing(commands, repo, env))
    missing.extend(_credential_missing(platform, checks))
    token = hmac.new(os.urandom(32), run_id.encode(), "sha256").hexdigest()[:32]
    plan = DeployPlan(
        status="preview",
        platform=platform,
        destination=destination,
        stage=DESTINATIONS[key],
        executed=False,
        missing=missing,
        commands=commands,
        notes=notes
        + [
            "Preview only. No store upload was started.",
            "Confirm with this preview_token and the same platform and destination.",
            "Production destinations also require production_ack PUBLISH_PRODUCTION.",
        ],
        preview_token=token,
        checks=checks,
    )
    path = _preview_path(home, run_id)
    path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    os.chmod(path, 0o600)
    return plan


def _load_preview(home: Path, run_id: str) -> dict:
    path = _preview_path(home, run_id)
    if not path.is_file():
        raise DeployGateError("no preview for this run; call preview before confirm")
    return json.loads(path.read_text(encoding="utf-8"))


def _default_runner(argv: list[str], cwd: Path) -> int:
    completed = subprocess.run(argv, cwd=cwd, check=False)
    return int(completed.returncode)


def confirm(
    *,
    home: Path,
    run_id: str,
    repo: Path,
    platform: str,
    destination: str,
    preview_token: str,
    production_ack: str | None = None,
    execute: bool = False,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
) -> DeployPlan:
    """Accept a prior preview. Upload only when execute is set and the env flag is 1."""
    saved = _load_preview(home, run_id)
    env = env if env is not None else os.environ
    fresh = preview(
        home=home,
        run_id=run_id,
        repo=repo,
        platform=platform,
        destination=destination,
        env=env,
    )
    fresh.notes = [note for note in fresh.notes if not note.startswith("Preview only")]
    # preview() rotates the on-disk token. The human still holds ``issued``.
    issued = saved.get("preview_token") or ""
    if not hmac.compare_digest(str(issued), str(preview_token or "")):
        _store_preview(home, run_id, saved)
        fresh.status = "blocked"
        fresh.missing = ["preview_token"] + fresh.missing
        fresh.notes.insert(0, "preview token does not match this run")
        fresh.executed = False
        fresh.preview_token = None
        return fresh
    if saved.get("platform") != platform or saved.get("destination") != destination:
        _store_preview(home, run_id, saved)
        fresh.status = "blocked"
        fresh.missing = ["destination_changed"] + fresh.missing
        fresh.notes.insert(0, "confirm destination differs from the stored preview")
        fresh.executed = False
        fresh.preview_token = issued
        return fresh
    if DESTINATIONS[(platform, destination)] == "production" and production_ack != PRODUCTION_ACK:
        fresh.status = "blocked"
        fresh.missing = ["production_ack"] + [item for item in fresh.missing if item != "production_ack"]
        fresh.notes.insert(0, "production publish requires production_ack PUBLISH_PRODUCTION")
        fresh.executed = False
        fresh.preview_token = issued
        _rewrite_token(home, run_id, fresh, issued)
        return fresh

    blocking = [
        item
        for item in fresh.missing
        if item.endswith("_lane")
        or item.startswith(("ASC_", "PLAY_", LIM_HOST_ENV, "tool:"))
    ]
    if blocking:
        fresh.status = "blocked"
        fresh.executed = False
        fresh.preview_token = issued
        fresh.notes.insert(0, "confirm refused; prerequisites are still missing")
        _rewrite_token(home, run_id, fresh, issued)
        return fresh

    fresh.status = "ready"
    fresh.preview_token = issued
    fresh.executed = False
    fresh.notes.insert(0, "Confirmed. Store upload has not been started.")
    if not execute:
        _rewrite_token(home, run_id, fresh, issued)
        return fresh
    if env.get(EXECUTE_ENV) != "1":
        fresh.status = "blocked"
        fresh.missing = ["MOBILE_DEPLOY_EXECUTE"] + fresh.missing
        fresh.notes.insert(0, "MOBILE_DEPLOY_EXECUTE is not 1; the upload command was not run")
        fresh.executed = False
        _rewrite_token(home, run_id, fresh, issued)
        return fresh
    run = runner or _default_runner
    for argv in fresh.commands:
        code = run(list(argv), repo)
        if code != 0:
            fresh.status = "blocked"
            fresh.missing = [f"command_exit:{code}"] + fresh.missing
            fresh.notes.insert(0, f"command failed with exit {code}: {argv[0]}")
            fresh.executed = False
            _rewrite_token(home, run_id, fresh, issued)
            return fresh
    fresh.executed = True
    fresh.status = "executed"
    fresh.notes.insert(0, "Runner returned success. This process did not talk to Apple or Google itself.")
    _rewrite_token(home, run_id, fresh, issued)
    return fresh


def _store_preview(home: Path, run_id: str, payload: dict) -> None:
    path = _preview_path(home, run_id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _rewrite_token(home: Path, run_id: str, plan: DeployPlan, token: str) -> None:
    plan.preview_token = token
    _store_preview(home, run_id, plan.to_dict())
