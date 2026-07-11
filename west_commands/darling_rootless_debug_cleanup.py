"""West command for guarded cleanup of disposable rootless debug trees."""

from __future__ import annotations

import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rootless_debug_cleanup import cleanup_rootless_debug_tree, validate_rootless_debug_tree


class DarlingRootlessDebugCleanup(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-rootless-debug-cleanup",
            "Remove one completed rootless Darling debug tree",
            "Refuses non-disposable paths, mounts, and live prefix-owned processes",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument("--path", required=True, help="/tmp/darling-rootless-*-debug-* tree")
        parser.add_argument("--dry-run", action="store_true", help="validate only")
        parser.add_argument("--sudo", action="store_true", help="allow explicit sudo removal after ownership failure")
        return parser

    def do_run(self, args, unknown):
        try:
            target = validate_rootless_debug_tree(Path(args.path))
        except ValueError as error:
            self.die(str(error))
        result = cleanup_rootless_debug_tree(target, allow_sudo=args.sudo, dry_run=args.dry_run)
        if result.problems:
            for problem in result.problems:
                self.err(problem)
            raise SystemExit(1)
        self.inf(f"{'would remove' if args.dry_run else 'removed'} rootless debug tree: {target}")
