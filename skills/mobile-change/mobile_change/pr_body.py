"""Per-run PR body files under a Hermes profile scratch directory.

Hermes ``write_file`` refuses to overwrite a file the current task has not fully
read. ``<home>/cache/scratch/pr-body.md`` is one shared path, so a later Slack
session cannot replace it. This module writes only
``<home>/cache/scratch/runs/<run-id>/pr-body.md``. Creating that file is
allowed. Replacing it requires the sha256 of the bytes the caller just read.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

SHARED_PR_BODY_NAME = "pr-body.md"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class PrBodyError(Exception):
    """The PR body was not written."""


class UnseenOverwriteError(PrBodyError):
    """Existing body has no full-content proof from this caller."""


def scratch_dir(home: Path) -> Path:
    return Path(home) / "cache" / "scratch"


def shared_pr_body_path(home: Path) -> Path:
    return scratch_dir(home) / SHARED_PR_BODY_NAME


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise PrBodyError(
            "run id must be 1-81 characters of letters, digits, '.', '_' or '-', "
            "and must not contain a path separator"
        )
    if run_id in {".", ".."} or ".." in run_id.split("."):
        raise PrBodyError("run id must not traverse directories")
    return run_id


def run_dir(home: Path, run_id: str) -> Path:
    return scratch_dir(home) / "runs" / validate_run_id(run_id)


def pr_body_path(home: Path, run_id: str) -> Path:
    path = run_dir(home, run_id) / SHARED_PR_BODY_NAME
    _reject_shared_target(home, path)
    return path


def _reject_shared_target(home: Path, path: Path) -> None:
    shared = shared_pr_body_path(home)
    if path.resolve() == shared.resolve():
        raise PrBodyError(
            f"refusing shared scratch path {shared}: allocate a per-run file instead"
        )


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def allocate(home: Path, run_id: str) -> Path:
    """Create the run directory and return the body path. Does not write the file."""
    path = pr_body_path(home, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    return path


@dataclass(frozen=True)
class PrBodyWrite:
    path: Path
    sha256: str
    created: bool


def read_pr_body(home: Path, run_id: str) -> tuple[str, str]:
    path = pr_body_path(home, run_id)
    if not path.is_file():
        raise PrBodyError(f"no pr body at {path}")
    content = path.read_text(encoding="utf-8")
    return content, sha256_text(content)


def write_pr_body(
    home: Path,
    run_id: str,
    content: str,
    *,
    expected_sha256: str | None = None,
) -> PrBodyWrite:
    """Create or replace the per-run body.

    A missing file is created. An existing file is replaced only when
    ``expected_sha256`` is the sha256 of its current UTF-8 bytes (the caller
    read it). Any other existing file is left untouched.
    """
    if not isinstance(content, str):
        raise PrBodyError("pr body content must be text")
    path = allocate(home, run_id)
    _reject_shared_target(home, path)
    exists = path.is_file()
    if exists:
        current = path.read_text(encoding="utf-8")
        current_sha = sha256_text(current)
        if expected_sha256 != current_sha:
            raise UnseenOverwriteError(
                f"{path} exists but this task has not seen its full current content. "
                "Read it and pass expected_sha256. The file was not modified."
            )
    elif expected_sha256 not in (None, ""):
        raise PrBodyError("expected_sha256 was set but the per-run body does not exist yet")

    _atomic_write(path, content)
    return PrBodyWrite(path=path, sha256=sha256_text(content), created=not exists)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".pr-body-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
