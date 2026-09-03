"""Human-oriented orchestration over authoritative Darling workspace commands."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from typing import Any

from west.commands import WestCommand
sys.path.insert(0, str(Path(__file__).resolve().parent))


from dev_check import (
    build_check_plan,
    build_package_plan,
    execute_check,
    execute_package,
)
from dev_start import build_start_plan, execute_start, recover_start
from dev_status import collect_status


_TIERS = ("quick", "canonical", "acceptance")


def _west_argv() -> list[str]:
    invoked = Path(sys.argv[0])
    if invoked.name == "__main__.py":
        return [sys.executable, "-m", "west"]
    if invoked.suffix == ".py" and invoked.is_file():
        return [sys.executable, str(invoked.resolve())]
    if invoked.is_file():
        return [str(invoked.resolve())]
    executable = shutil.which("west")
    if executable is None:
        raise RuntimeError("cannot locate the current west executable")
    return [executable]


def _active_repository_roots(manifest: Any, manifest_repo: Path) -> list[Path]:
    roots = {manifest_repo.resolve()}
    for project in getattr(manifest, "projects", ()):
        checker = getattr(manifest, "is_active", None)
        if checker is not None:
            try:
                if not checker(project):
                    continue
            except Exception as error:
                raise RuntimeError(
                    f"cannot determine whether West project is active: {error}"
                ) from error
        abspath = getattr(project, "abspath", None)
        if abspath:
            roots.add(Path(abspath).resolve())
    return sorted(roots, key=str)


def _render_human(command: WestCommand, payload: dict[str, Any]) -> None:
    operation = payload.get("operation", "dev")
    state = payload.get("state", "unknown")
    transaction = payload.get("transaction_id")
    suffix = f" transaction={transaction}" if transaction else ""
    command.inf(f"west dev {operation}: {state}{suffix}")
    if operation == "status":
        for name, section in payload.get("results", payload.get("sections", {})).items():
            health = section.get("health", section.get("state", "unknown"))
            details: list[str] = []
            if name == "git":
                dirty = [
                    repository.get("name", repository.get("path", "?"))
                    for repository in section.get("repositories", [])
                    if repository.get("dirty") is True
                ]
                if dirty:
                    details.append("dirty=" + ",".join(dirty))
            active = section.get("active")
            if active:
                details.append("active=" + ",".join(str(item) for item in active))
            invalid = section.get("invalid")
            if invalid:
                details.append(f"invalid={len(invalid)}")
            detail = f" {' '.join(details)}" if details else ""
            command.inf(f"{health:11} {name}{detail}")
    steps = payload.get("steps", [])
    if state == "planned":
        command.inf(f"plan: {len(steps)} steps")
        for step in steps:
            if step.get("mutating"):
                command.inf(
                    f"mutates: {step.get('name', 'step')} "
                    + " ".join(step.get("argv", []))
                )
    elif steps:
        results = payload.get("results", [])
        command.inf(f"commands recorded: {len(results)}")
        if payload.get("returncode") not in (None, 0):
            terminal = next(
                (
                    result
                    for result in reversed(results)
                    if result.get("returncode") not in (None, 0)
                ),
                None,
            )
            if terminal is not None:
                command.inf(
                    f"failed: {terminal.get('name', 'step')} "
                    f"rc={terminal.get('returncode')}"
                )
    next_action = payload.get("next_safe_action")
    if next_action:
        command.inf(f"next: {next_action}")


class DarlingDev(WestCommand):
    def __init__(self) -> None:
        super().__init__("dev", "", "Run the daily multi-repository Darling feature workflow")

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)

        status = subparsers.add_parser("status", help="summarize local feature-work state")
        status.add_argument("--profile", default="homebrew")
        status.add_argument("--bead")
        status.add_argument("--prefix", type=Path)
        status.add_argument("--build-dir", type=Path)
        status.add_argument("--json", action="store_true")

        start = subparsers.add_parser("start", help="create an isolated authoring checkout")
        start.add_argument("--source", type=Path, required=True)
        start.add_argument("--destination", type=Path, required=True)
        start.add_argument("--base", required=True)
        start.add_argument("--branch", required=True)
        start.add_argument("--bead", required=True)
        start.add_argument("--module", required=True)
        start.add_argument("--evidence", type=Path, required=True)
        start.add_argument("--dry-run", action="store_true")
        start.add_argument("--json", action="store_true")
        recover = subparsers.add_parser(
            "recover-start", help="recover an interrupted isolated checkout"
        )
        recover.add_argument("--evidence", type=Path, required=True)
        recover.add_argument("--json", action="store_true")


        check = subparsers.add_parser("check", help="run a named verification tier")
        check.add_argument("tier", choices=_TIERS)
        check.add_argument("--profile", default="homebrew")
        check.add_argument("--bead")
        check.add_argument("--patch")
        check.add_argument("--prefix", type=Path)
        check.add_argument("--build-dir", type=Path)
        check.add_argument("--evidence", type=Path, required=True)
        check.add_argument("--dry-run", action="store_true")
        check.add_argument("--json", action="store_true")

        package = subparsers.add_parser("package", help="create a checked local review package")
        package.add_argument("--profile", default="homebrew")
        package.add_argument("--receipt", type=Path, required=True)
        package.add_argument("--output", type=Path, required=True)
        package.add_argument("--evidence", type=Path, required=True)
        package.add_argument("--required-tier", choices=_TIERS, default="canonical")
        package.add_argument("--dry-run", action="store_true")
        package.add_argument("--json", action="store_true")
        return parser

    def _emit(self, payload: dict[str, Any], json_output: bool) -> None:
        if json_output:
            self.inf(json.dumps(payload, sort_keys=True, indent=2))
        else:
            _render_human(self, payload)

    def do_run(self, args, unknown) -> None:
        if unknown:
            self.die(f"unknown arguments: {' '.join(unknown)}")
        manifest_repo = Path(self.manifest.repo_abspath).resolve()
        topdir = Path(self.topdir).resolve()
        try:
            west_argv = _west_argv()
            if args.action == "status":
                result = collect_status(
                    topdir=topdir,
                    manifest_repo=manifest_repo,
                    manifest=self.manifest,
                    profile=args.profile,
                    bead=args.bead,
                    prefix=args.prefix.resolve() if args.prefix else None,
                    build_dir=args.build_dir.resolve() if args.build_dir else None,
                    west_argv=west_argv,
                )
            elif args.action == "start":
                plan = build_start_plan(
                    source=args.source.resolve(),
                    destination=args.destination.absolute(),
                    base=args.base,
                    branch=args.branch,
                    bead=args.bead,
                    module=args.module,
                    evidence=args.evidence.absolute(),
                    forbidden_roots=_active_repository_roots(
                        self.manifest, manifest_repo
                    ),
                )
                if args.dry_run:
                    result = plan
                else:
                    if not args.json:
                        _render_human(self, plan)
                    result = execute_start(plan)
            elif args.action == "recover-start":
                result = recover_start(args.evidence.absolute())
            elif args.action == "check":
                plan = build_check_plan(
                    manifest_repo=manifest_repo,
                    west_argv=west_argv,
                    tier=args.tier,
                    profile=args.profile,
                    bead=args.bead,
                    patch=args.patch,
                    evidence=args.evidence.absolute(),
                    prefix=args.prefix.resolve() if args.prefix else None,
                    build_dir=args.build_dir.resolve() if args.build_dir else None,
                )
                if args.dry_run:
                    result = plan
                else:
                    if not args.json:
                        _render_human(self, plan)
                    result = execute_check(plan)
            else:
                plan = build_package_plan(
                    manifest_repo=manifest_repo,
                    west_argv=west_argv,
                    profile=args.profile,
                    receipt=args.receipt.resolve(),
                    output=args.output.absolute(),
                    evidence=args.evidence.absolute(),
                    required_tier=args.required_tier,
                )
                if args.dry_run:
                    result = plan
                else:
                    if not args.json:
                        _render_human(self, plan)
                    result = execute_package(plan)
        except (OSError, RuntimeError, ValueError) as error:
            self.die(str(error))
            return

        self._emit(result, args.json)
        returncode = result.get("returncode", 0)
        if returncode:
            self.die(
                f"west dev {args.action} failed with exit status {returncode}",
                exit_code=returncode,
            )
