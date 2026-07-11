"""Repair common Darling prefix prerequisites used by guest tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefix_repair import (
    cleanup_prefix_mounts,
    prefix_mount_targets,
    repair_prefix_prerequisites,
)
from test_prefix import (
    cleanup_rootless_runtime_sockets,
    rootless_prefix_process_snapshot,
)


class DarlingPrefixRepair(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-prefix-repair",
            "Repair Darling prefix boot and guest-test prerequisites",
            "Create required tmp directories and restore canonical CLT links in a Darling prefix",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
            "--prefix",
            action="append",
            default=[],
            help=(
                "Darling prefix to repair; repeatable. Defaults to DARLING_PREFIX "
                "or ~/work/darling-prefix."
            ),
        )
        parser.add_argument(
            "--extra-prefix",
            action="append",
            default=[],
            help="additional prefix to repair; repeatable",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="only verify repairable prerequisites; do not modify files",
        )
        parser.add_argument(
            "--cleanup-mounts",
            action="store_true",
            help="also unmount stale filesystems and remove idle rootless control sockets",
        )
        return parser

    def do_run(self, args, unknown):
        raw_prefixes = [*args.prefix, *args.extra_prefix]
        if not raw_prefixes:
            raw_prefixes = [
                os.environ.get("DARLING_PREFIX", str(Path.home() / "work/darling-prefix"))
            ]

        failed = False
        for raw_prefix in raw_prefixes:
            prefix = Path(raw_prefix).expanduser()
            self.inf(f"== {prefix} ==")
            result = repair_prefix_prerequisites(prefix, check=args.check)
            if args.cleanup_mounts:
                if args.check:
                    mounts = prefix_mount_targets(prefix)
                    if mounts:
                        result.problems.extend(
                            f"mounted filesystem under prefix: {mount}" for mount in mounts
                        )
                    else:
                        result.ok.append("no mounted filesystems under prefix")
                else:
                    result.extend(cleanup_prefix_mounts(prefix))
                    remaining_mounts = prefix_mount_targets(prefix)
                    rootless_processes = rootless_prefix_process_snapshot(prefix)
                    if remaining_mounts:
                        result.problems.append(
                            "refusing rootless runtime socket cleanup while prefix mounts remain"
                        )
                    elif rootless_processes:
                        result.problems.append(
                            "refusing rootless runtime socket cleanup while prefix process(es) remain: "
                            + "; ".join(rootless_processes)
                        )
                    else:
                        socket_cleanup = cleanup_rootless_runtime_sockets(prefix)
                        result.changed.extend(socket_cleanup.changed)
                        result.problems.extend(socket_cleanup.problems)
            for message in result.ok:
                self.inf(f"  ok: {message}")
            for message in result.changed:
                self.inf(f"  fixed: {message}")
            for message in result.problems:
                self.err(f"  problem: {message}")
            if not result.success:
                failed = True
        if failed:
            raise SystemExit(1)
