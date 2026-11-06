#!/usr/bin/env python3
"""Preview or confirm a store destination. Does not upload unless --execute and MOBILE_DEPLOY_EXECUTE=1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mobile_change.deploy_gate import DeployGateError, confirm, preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--destination", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preview")
    confirm_cmd = sub.add_parser("confirm")
    confirm_cmd.add_argument("--preview-token", required=True)
    confirm_cmd.add_argument("--production-ack", default=None)
    confirm_cmd.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "preview":
            plan = preview(
                home=args.home,
                run_id=args.run_id,
                repo=args.repo,
                platform=args.platform,
                destination=args.destination,
            )
        else:
            plan = confirm(
                home=args.home,
                run_id=args.run_id,
                repo=args.repo,
                platform=args.platform,
                destination=args.destination,
                preview_token=args.preview_token,
                production_ack=args.production_ack,
                execute=args.execute,
            )
    except DeployGateError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(plan.to_dict()))
    return 0 if plan.status in {"preview", "ready", "executed"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
