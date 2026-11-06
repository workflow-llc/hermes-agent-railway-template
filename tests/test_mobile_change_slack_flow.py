"""Slack mobile-change regressions: shared scratch reuse and gated store plans."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "mobile-change"))

from mobile_change.deploy_gate import confirm, preview  # noqa: E402
from mobile_change.pr_body import (  # noqa: E402
    UnseenOverwriteError,
    read_pr_body,
    shared_pr_body_path,
    write_pr_body,
)

SENTINEL = "SUPER-SECRET-P8-BYTES-DO-NOT-LEAK"
PLAY_SENTINEL = "SUPER-SECRET-PLAY-JSON-DO-NOT-LEAK"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def _ios_repo(root: Path, lanes: str) -> Path:
    repo = root / "app-ios"
    fast = repo / "fastlane"
    fast.mkdir(parents=True)
    (fast / "Fastfile").write_text(lanes, encoding="utf-8")
    return repo


def _android_repo(root: Path, lanes: str) -> Path:
    repo = root / "android-app"
    repo.mkdir(parents=True)
    gradlew = repo / "gradlew"
    gradlew.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(gradlew, 0o755)
    fast = repo / "fastlane"
    fast.mkdir()
    (fast / "Fastfile").write_text(lanes, encoding="utf-8")
    return repo


def _fake_tool(root: Path, name: str) -> str:
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(tool, 0o755)
    return str(bindir)


class SlackPrBodyTests(unittest.TestCase):
    def test_preexisting_shared_file_and_repeated_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shared = shared_pr_body_path(home)
            shared.parent.mkdir(parents=True)
            shared.write_text("left by an earlier session\n", encoding="utf-8")
            before = shared.read_bytes()

            first = write_pr_body(home, "slack-1793920160", "body for thread A\n")
            self.assertTrue(first.created)
            self.assertNotEqual(first.path.resolve(), shared.resolve())
            self.assertEqual(shared.read_bytes(), before)
            self.assertTrue(str(first.path).endswith("/runs/slack-1793920160/pr-body.md"))

            with self.assertRaises(UnseenOverwriteError) as raised:
                write_pr_body(home, "slack-1793920160", "replaced without reading\n")
            self.assertIn("has not seen", str(raised.exception))
            self.assertEqual(first.path.read_text(encoding="utf-8"), "body for thread A\n")
            self.assertEqual(shared.read_bytes(), before)

            _content, digest = read_pr_body(home, "slack-1793920160")
            updated = write_pr_body(
                home,
                "slack-1793920160",
                "body for thread A revised\n",
                expected_sha256=digest,
            )
            self.assertFalse(updated.created)
            self.assertEqual(shared.read_bytes(), before)

            second = write_pr_body(home, "slack-1793920999", "body for thread B\n")
            self.assertTrue(second.created)
            self.assertNotEqual(second.path, first.path)
            self.assertEqual(first.path.read_text(encoding="utf-8"), "body for thread A revised\n")
            self.assertEqual(second.path.read_text(encoding="utf-8"), "body for thread B\n")
            self.assertEqual(shared.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(first.path.stat().st_mode), 0o600)

    def test_cli_write_uses_the_run_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            script = ROOT / "skills" / "mobile-change" / "scripts" / "pr_body.py"
            completed = subprocess.run(
                [sys.executable, str(script), "--home", str(home), "--run-id", "cli-run", "write"],
                input="from stdin\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["created"])
            self.assertTrue(payload["path"].endswith("/runs/cli-run/pr-body.md"))
            self.assertFalse((home / "cache" / "scratch" / "pr-body.md").exists())


class DeployGateTests(unittest.TestCase):
    def _ios_env(self, root: Path, key: Path) -> dict[str, str]:
        bindir = _fake_tool(root, "lim")
        _fake_tool(root, "bundle")
        env = {
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
            "ASC_KEY_ID": "not-the-key-id-value",
            "ASC_ISSUER_ID": "not-the-issuer-value",
            "ASC_PRIVATE_KEY_PATH": str(key),
            "LIM_HOST_READY": "1",
        }
        return env

    def test_preview_does_not_upload_or_leak_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            key = root / "AuthKey.p8"
            _write_private(key, SENTINEL)
            repo = _ios_repo(root, "lane :pilot do\nend\n")
            plan = preview(
                home=home,
                run_id="slack-preview",
                repo=repo,
                platform="ios",
                destination="testflight",
                env=self._ios_env(root, key),
            )
            blob = json.dumps(plan.to_dict())
            self.assertEqual(plan.status, "preview")
            self.assertFalse(plan.executed)
            self.assertEqual(plan.commands, [["bundle", "exec", "fastlane", "pilot"]])
            self.assertNotIn(SENTINEL, blob)
            self.assertNotIn("not-the-key-id-value", blob)
            self.assertNotIn("not-the-issuer-value", blob)
            self.assertNotIn(str(key), blob)
            self.assertEqual(plan.checks["ASC_PRIVATE_KEY_PATH"], "present")

    def test_confirm_blocks_without_credentials_and_keeps_production_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            repo = _ios_repo(root, "lane :pilot do\nend\nlane :release do\nend\n")
            env = {"PATH": _fake_tool(root, "bundle"), "LIM_HOST_READY": "1"}
            shown = preview(
                home=home, run_id="slack-confirm", repo=repo,
                platform="ios", destination="testflight", env=env,
            )
            calls: list[list[str]] = []
            blocked = confirm(
                home=home, run_id="slack-confirm", repo=repo,
                platform="ios", destination="testflight",
                preview_token=shown.preview_token or "",
                execute=True, env=env, runner=lambda argv, _cwd: calls.append(argv) or 0,
            )
            self.assertEqual(blocked.status, "blocked")
            self.assertFalse(blocked.executed)
            self.assertEqual(calls, [])
            self.assertTrue(any(item.startswith("ASC_KEY_ID") for item in blocked.missing))
            wrong = confirm(
                home=home, run_id="slack-confirm", repo=repo,
                platform="ios", destination="testflight",
                preview_token="0" * 32, env=env,
                runner=lambda argv, _cwd: calls.append(argv) or 0,
            )
            self.assertEqual(wrong.status, "blocked")
            self.assertIn("preview_token", wrong.missing)
            self.assertFalse(wrong.executed)
            self.assertEqual(calls, [])

            key = root / "AuthKey.p8"
            _write_private(key, SENTINEL)
            ready_env = self._ios_env(root, key)
            shown = preview(
                home=home, run_id="slack-ready", repo=repo,
                platform="ios", destination="testflight", env=ready_env,
            )
            ready = confirm(
                home=home, run_id="slack-ready", repo=repo,
                platform="ios", destination="testflight",
                preview_token=shown.preview_token or "",
                execute=False, env=ready_env, runner=lambda argv, _cwd: calls.append(argv) or 0,
            )
            self.assertEqual(ready.status, "ready")
            self.assertFalse(ready.executed)
            self.assertEqual(calls, [])

            store = preview(
                home=home, run_id="slack-store", repo=repo,
                platform="ios", destination="appstore", env=ready_env,
            )
            no_ack = confirm(
                home=home, run_id="slack-store", repo=repo,
                platform="ios", destination="appstore",
                preview_token=store.preview_token or "",
                execute=True, env={**ready_env, "MOBILE_DEPLOY_EXECUTE": "1"},
                runner=lambda argv, _cwd: calls.append(argv) or 0,
            )
            self.assertEqual(no_ack.status, "blocked")
            self.assertIn("production_ack", no_ack.missing)
            self.assertFalse(no_ack.executed)
            self.assertEqual(calls, [])

    def test_android_internal_ready_and_production_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            repo = _android_repo(
                root,
                "lane :internal do\nend\nlane :production do\nend\n",
            )
            account = root / "play.json"
            _write_private(account, PLAY_SENTINEL)
            env = {
                "PATH": _fake_tool(root, "bundle"),
                "PLAY_SERVICE_ACCOUNT_JSON": str(account),
            }
            shown = preview(
                home=home, run_id="android-internal", repo=repo,
                platform="android", destination="internal", env=env,
            )
            blob = json.dumps(shown.to_dict())
            self.assertNotIn(PLAY_SENTINEL, blob)
            self.assertFalse(shown.executed)
            self.assertEqual(
                shown.commands,
                [["./gradlew", "bundleRelease"], ["bundle", "exec", "fastlane", "internal"]],
            )
            ready = confirm(
                home=home, run_id="android-internal", repo=repo,
                platform="android", destination="internal",
                preview_token=shown.preview_token or "", env=env,
            )
            self.assertEqual(ready.status, "ready")
            self.assertFalse(ready.executed)

            prod = preview(
                home=home, run_id="android-prod", repo=repo,
                platform="android", destination="production", env=env,
            )
            blocked = confirm(
                home=home, run_id="android-prod", repo=repo,
                platform="android", destination="production",
                preview_token=prod.preview_token or "", env=env,
            )
            self.assertIn("production_ack", blocked.missing)
            self.assertFalse(blocked.executed)

    def test_open_key_file_and_missing_lane_block_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            key = root / "wide.p8"
            key.write_text(SENTINEL, encoding="utf-8")
            os.chmod(key, 0o644)
            repo = _ios_repo(root, "lane :pilot do\nend\n")
            env = self._ios_env(root, key)
            shown = preview(
                home=home, run_id="wide-key", repo=repo,
                platform="ios", destination="testflight", env=env,
            )
            self.assertEqual(shown.checks["ASC_PRIVATE_KEY_PATH"], "mode_too_open")
            blocked = confirm(
                home=home, run_id="wide-key", repo=repo,
                platform="ios", destination="testflight",
                preview_token=shown.preview_token or "", env=env,
            )
            self.assertEqual(blocked.status, "blocked")
            self.assertFalse(blocked.executed)

            bare = root / "bare-ios"
            bare.mkdir()
            lim_env = self._ios_env(root, key)
            os.chmod(key, 0o600)
            lim_only = preview(
                home=home, run_id="lim-only", repo=bare,
                platform="ios", destination="testflight", env=lim_env,
            )
            self.assertEqual(lim_only.commands, [["lim", "upload"]])
            appstore = preview(
                home=home, run_id="no-appstore-lane", repo=bare,
                platform="ios", destination="appstore", env=lim_env,
            )
            self.assertIn("appstore_lane", appstore.missing)
            refused = confirm(
                home=home, run_id="no-appstore-lane", repo=bare,
                platform="ios", destination="appstore",
                preview_token=appstore.preview_token or "",
                production_ack="PUBLISH_PRODUCTION", env=lim_env,
            )
            self.assertEqual(refused.status, "blocked")
            self.assertFalse(refused.executed)

    def test_execute_runs_only_the_planned_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            key = root / "AuthKey.p8"
            _write_private(key, SENTINEL)
            repo = _ios_repo(root, "")
            env = {**self._ios_env(root, key), "MOBILE_DEPLOY_EXECUTE": "1"}
            shown = preview(
                home=home, run_id="execute", repo=repo,
                platform="ios", destination="testflight", env=env,
            )
            seen: list[tuple[list[str], Path]] = []

            def runner(argv: list[str], cwd: Path) -> int:
                seen.append((argv, cwd))
                return 0

            done = confirm(
                home=home, run_id="execute", repo=repo,
                platform="ios", destination="testflight",
                preview_token=shown.preview_token or "",
                execute=True, env=env, runner=runner,
            )
            self.assertEqual(done.status, "executed")
            self.assertTrue(done.executed)
            self.assertEqual(seen, [(["lim", "upload"], repo)])
            self.assertNotIn(SENTINEL, json.dumps([[argv, str(cwd)] for argv, cwd in seen]))


class SkillInstallTests(unittest.TestCase):
    def test_boot_hook_installs_into_code_review_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "data" / ".hermes"
            profile = home / "profiles" / "code-review"
            profile.mkdir(parents=True)
            shared = profile / "cache" / "scratch" / "pr-body.md"
            shared.parent.mkdir(parents=True)
            shared.write_text("keep me\n", encoding="utf-8")
            env = os.environ.copy()
            env["SAGA_MOBILE_SKILL_SRC"] = str(ROOT / "skills" / "mobile-change")
            env["SAGA_HERMES_HOME"] = str(home)
            completed = subprocess.run(
                ["sh", str(ROOT / "saga-patches" / "mobile-change-skill.sh")],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            installed = profile / "skills" / "mobile-change" / "SKILL.md"
            self.assertTrue(installed.is_file())
            text = installed.read_text(encoding="utf-8")
            self.assertIn("cache/scratch/runs/", text)
            self.assertIn("Do not call `write_file`", text)
            self.assertEqual(shared.read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue((home / "skills" / "mobile-change" / "scripts" / "pr_body.py").is_file())


if __name__ == "__main__":
    unittest.main()
