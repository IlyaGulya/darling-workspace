"""Darling workspace west extension."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beads_aliases import normalize_beads_args


class DarlingWorkspace(WestCommand):
    def __init__(self):
        super().__init__(
            "dw",
            "",
            "Darling workspace coordination and handoff",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
            "action",
            choices=("summary", "beads", "restore", "handoff"),
        )
        parser.add_argument("args", nargs=argparse.REMAINDER)
        return parser

    def do_run(self, args, unknown):
        manifest_repo = Path(self.manifest.repo_abspath)
        if args.action == "summary":
            projects = self.manifest.projects
            active = [project for project in projects if self.manifest.is_active(project)]
            private = [project for project in projects if "private" in project.groups]
            self.inf(f"workspace: {self.topdir}")
            self.inf(f"manifest:  {manifest_repo}")
            self.inf(f"projects:  {len(projects)} ({len(active)} active)")
            self.inf(f"private:   {len(private)}")
            return

        if args.action == "beads":
            env = os.environ.copy()
            env["BEADS_DIR"] = str(manifest_repo / ".beads")
            command = normalize_beads_args(args.args, unknown)
            raise SystemExit(
                subprocess.run(
                    ["br", *command],
                    cwd=manifest_repo,
                    env=env,
                    check=False,
                ).returncode
            )

        if args.action == "restore":
            command = [
                str(manifest_repo / "scripts" / "west_workspace.py"),
                "--topdir",
                self.topdir,
                "--manifest-repo",
                str(manifest_repo),
                "restore",
                *args.args,
                *unknown,
            ]
            raise SystemExit(
                subprocess.run(command, cwd=manifest_repo, check=False).returncode
            )

        env = os.environ.copy()
        env["DW_DARLING_SRC"] = str(Path(self.topdir) / "darling")
        raise SystemExit(
            subprocess.run(
                [
                    str(manifest_repo / "bin" / "dw"),
                    "handoff",
                    *args.args,
                    *unknown,
                ],
                cwd=manifest_repo,
                env=env,
                check=False,
            ).returncode
        )
