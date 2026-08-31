#!/usr/bin/env python3
"""Create, retain, discard, or collect Darling-owned scratch roots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))

from owned_scratch import (  # noqa: E402
    DEFAULT_CANDIDATES,
    DEFAULT_KEEP,
    DEFAULT_SECONDS,
    DEFAULT_TTL_SECONDS,
    OwnedScratchRoot,
    ScratchSafetyError,
    default_namespace,
    dissociate_repository,
    discard_exact,
    garbage_collect,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--namespace", type=Path, default=default_namespace())
    commands = result.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create")
    create.add_argument("--kind", choices=("agent-review", "lifecycle-lab", "runtime-proof", "patch-verify"), required=True)
    create.add_argument("--prefix")
    discard = commands.add_parser("discard")
    discard.add_argument("path", type=Path)
    discard.add_argument("--force-dirty", action="store_true")
    discard.add_argument("--dry-run", action="store_true")
    retain = commands.add_parser("retain")
    retain.add_argument("path", type=Path)
    retain.add_argument("--disposable", action="append", type=Path, default=[])
    retain.add_argument("--raw-log", type=Path)
    dissociate = commands.add_parser("dissociate")
    dissociate.add_argument("repository", type=Path)
    dissociate.add_argument("donor", type=Path)
    gc = commands.add_parser("gc")
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_SECONDS / 3600)
    gc.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    gc.add_argument("--max-candidates", type=int, default=DEFAULT_CANDIDATES)
    gc.add_argument("--max-seconds", type=float, default=DEFAULT_SECONDS)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "create":
            owner = OwnedScratchRoot.create(namespace=args.namespace, kind=args.kind, prefix=args.prefix)
            owner.close()
            print(owner.path)
            return 0
        if args.action == "discard":
            diagnostics: list[str] = []
            freed = discard_exact(
                args.namespace, args.path, force_dirty=args.force_dirty,
                dry_run=args.dry_run, diagnostics=diagnostics,
            )
            print(json.dumps({
                "path": str(args.path), "bytes": freed, "dry_run": args.dry_run,
                "diagnostics": diagnostics,
            }, sort_keys=True))
            return 0
        if args.action == "retain":
            owner = OwnedScratchRoot.acquire(args.namespace, args.path)
            for path in args.disposable:
                owner.register_disposable(path)
            owner.retain(raw_log=args.raw_log)
            print(owner.path)
            return 0
        if args.action == "dissociate":
            dissociate_repository(args.namespace, args.repository, args.donor)
            print(json.dumps({"repository": str(args.repository), "donor": str(args.donor), "dissociated": True}, sort_keys=True))
            return 0
        if args.ttl_hours < 0 or args.keep < 0 or args.max_candidates <= 0 or args.max_seconds <= 0:
            raise ScratchSafetyError("GC budgets must be non-negative and bounded")
        outcome = garbage_collect(
            args.namespace,
            ttl_seconds=args.ttl_hours * 3600,
            keep=args.keep,
            dry_run=args.dry_run,
            max_candidates=args.max_candidates,
            max_seconds=args.max_seconds,
        )
        print(json.dumps({
            "removed": [str(path) for path in outcome.removed],
            "quarantined": [str(path) for path in outcome.quarantined],
            "retained": [{"path": str(path), "reason": reason} for path, reason in outcome.retained],
            "bytes": outcome.bytes_freed,
            "scanned": outcome.scanned,
            "bounded": outcome.bounded,
            "diagnostics": outcome.diagnostics,
            "dry_run": args.dry_run,
        }, sort_keys=True))
        return 0
    except ScratchSafetyError as error:
        print(f"owned scratch: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
