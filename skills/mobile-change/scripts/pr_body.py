#!/usr/bin/env python3
"""Allocate, read, or write the per-run PR body. Never touches cache/scratch/pr-body.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mobile_change.pr_body import PrBodyError, allocate, read_pr_body, write_pr_body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("allocate")
    sub.add_parser("read")
    write = sub.add_parser("write")
    write.add_argument("--expected-sha256", default=None)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "allocate":
            path = allocate(args.home, args.run_id)
            print(json.dumps({"path": str(path), "created": False}))
            return 0
        if args.cmd == "read":
            content, digest = read_pr_body(args.home, args.run_id)
            print(json.dumps({"sha256": digest, "content": content}))
            return 0
        content = sys.stdin.read()
        result = write_pr_body(
            args.home, args.run_id, content, expected_sha256=args.expected_sha256
        )
        print(json.dumps({"path": str(result.path), "sha256": result.sha256, "created": result.created}))
        return 0
    except PrBodyError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
