"""Darling workspace west extension."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beads_aliases import normalize_beads_args


class _DelegatingActionParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        raw_args = [] if args is None else list(args)
        if raw_args in (["-h"], ["--help"]):
            return super().parse_known_args(raw_args, namespace)
        parsed, _unknown = super().parse_known_args([], namespace)
        parsed.args = raw_args
        return parsed, []


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
        actions = parser.add_subparsers(
            dest="action",
            required=True,
            parser_class=_DelegatingActionParser,
        )
        actions.add_parser("summary")
        for action in ("beads", "restore", "handoff"):
            actions.add_parser(action)
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
        darling_root = Path(self.topdir) / "darling"
        closure = []
        for project in self.manifest.projects:
            if not self.manifest.is_active(project):
                continue
            userdata = project.userdata
            if isinstance(userdata, str):
                try:
                    userdata = json.loads(userdata)
                except json.JSONDecodeError:
                    userdata = {}
            if not isinstance(userdata, dict) or userdata.get("kind") != "darling-source":
                continue
            project_path = Path(self.topdir) / project.path
            try:
                relative = project_path.relative_to(darling_root)
            except ValueError:
                continue
            closure.append("." if not relative.parts else relative.as_posix())
        env["DW_HANDOFF_EXPECTED_PROJECTS"] = json.dumps(closure, separators=(",", ":"))
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
