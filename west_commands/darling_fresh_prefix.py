"""West command for disposable rootless test prefixes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fresh_prefix import create_fresh_prefix, remove_fresh_prefix
from test_store import durable_subdir
from runtime_install import prefix_path_is_safe


class DarlingFreshPrefix(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-fresh-prefix",
            "Create or remove an isolated disposable Darling prefix",
            "Copy writable guest state without hard links, bind it with the product launcher, and clean it only after lifecycle checks",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument("--from", dest="baseline", help="baseline guest-state prefix to copy")
        parser.add_argument(
            "--runtime-root",
            help="selected immutable Darling installation (default: DARLING_RUNTIME_ROOT)",
        )
        parser.add_argument("--path", help="explicit <state-root>/prefixes/darling-fresh-prefix-* destination")
        parser.add_argument("--root", help="directory holding fresh prefixes (default: "
                            "<DW_STATE_ROOT or <topdir>/.west-state>/prefixes)")
        parser.add_argument("--remove", action="store_true", help="remove a completed fresh prefix")
        return parser

    def do_run(self, args, unknown):
        raw_root = os.path.expanduser(args.root) if args.root else str(durable_subdir(self.topdir, "prefixes"))
        if not prefix_path_is_safe(raw_root):
            self.die(f"unsafe fresh-prefix root: {raw_root}")
        root = Path(raw_root)
        root.mkdir(parents=True, exist_ok=True)
        if args.remove:
            if not args.path or args.baseline:
                self.die("--remove requires --path and cannot be combined with --from")
            result = remove_fresh_prefix(args.path, temp_root=root)
        else:
            if not args.baseline:
                self.die("creation requires --from BASELINE")
            result = create_fresh_prefix(
                args.baseline,
                destination=args.path,
                temp_root=root,
                runtime_root=args.runtime_root,
            )
        for message in result.changed:
            self.inf(message)
        for message in result.problems:
            self.err(message)
        if not result.success:
            raise SystemExit(1)
        if result.path is not None:
            self.inf(str(result.path))
