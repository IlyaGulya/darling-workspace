"""West command for disposable rootless test prefixes."""

from __future__ import annotations

import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fresh_prefix import create_fresh_prefix, remove_fresh_prefix


class DarlingFreshPrefix(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-fresh-prefix",
            "Create or remove an isolated disposable Darling prefix",
            "Copy a baseline prefix without hard links and clean it only after lifecycle checks",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument("--from", dest="baseline", help="baseline prefix to copy")
        parser.add_argument("--path", help="explicit /tmp/darling-fresh-prefix-* destination")
        parser.add_argument("--remove", action="store_true", help="remove a completed fresh prefix")
        return parser

    def do_run(self, args, unknown):
        if args.remove:
            if not args.path or args.baseline:
                self.die("--remove requires --path and cannot be combined with --from")
            result = remove_fresh_prefix(Path(args.path))
        else:
            if not args.baseline:
                self.die("creation requires --from BASELINE")
            result = create_fresh_prefix(
                Path(args.baseline), destination=Path(args.path) if args.path else None
            )
        for message in result.changed:
            self.inf(message)
        for message in result.problems:
            self.err(message)
        if not result.success:
            raise SystemExit(1)
        if result.path is not None:
            self.inf(str(result.path))
