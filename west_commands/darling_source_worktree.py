"""West CLI for preparing reproducible nested Darling source worktrees."""

from __future__ import annotations

from pathlib import Path

from west.commands import WestCommand

from source_worktree import (
    SourceWorktreeError,
    cleanup_record,
    default_record_path,
    prepare_source_worktree,
    verify_record,
    write_record,
)


class DarlingSourceWorktree(WestCommand):
    """Hydrate and audit nested gitlinks in an existing clean source worktree."""

    def __init__(self):
        super().__init__(
            "darling-source-worktree",
            "Prepare reproducible nested Darling source worktrees",
            "Materialize every gitlink from local canonical refs at its exact SHA",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        commands = parser.add_subparsers(dest="action", required=True)
        prepare = commands.add_parser("prepare")
        prepare.add_argument("--source", required=True, type=Path)
        prepare.add_argument("--canonical", type=Path)
        prepare.add_argument("--record", type=Path)
        check = commands.add_parser("check")
        check.add_argument("--record", required=True, type=Path)
        check.add_argument("--build-dir", type=Path)
        cleanup = commands.add_parser("cleanup")
        cleanup.add_argument("--record", required=True, type=Path)
        return parser

    def do_run(self, args, unknown):
        if unknown:
            self.die(f"unexpected arguments: {' '.join(unknown)}")
        try:
            if args.action == "prepare":
                source = args.source.resolve()
                canonical = (args.canonical or (Path(self.topdir) / "darling")).resolve()
                record = (args.record or default_record_path(source)).resolve()
                entries = prepare_source_worktree(source, canonical)
                write_record(
                    record,
                    source_root=source,
                    canonical_root=canonical,
                    entries=entries,
                )
                self.inf(
                    f"prepared {len(entries)} nested source worktree(s); record: {record}"
                )
                return
            if args.action == "check":
                payload = verify_record(args.record.resolve(), build_dir=args.build_dir)
                self.inf(
                    f"source worktree OK: {payload['source_root']} "
                    f"({len(payload['gitlinks'])} nested gitlink(s))"
                )
                return
            if args.action == "cleanup":
                cleanup_record(args.record.resolve())
                self.inf(f"removed prepared nested source worktree(s): {args.record}")
                return
            self.die(f"unknown action: {args.action}")
        except SourceWorktreeError as error:
            self.die(str(error))
