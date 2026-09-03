"""Human-oriented orchestration over authoritative Darling workspace commands."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any

from west.commands import WestCommand
sys.path.insert(0, str(Path(__file__).resolve().parent))


from dev_check import (
    DevCheckError,
    build_check_plan,
    build_package_plan,
    execute_check,
    execute_package,
    verify_package,
)
from dev_start import build_start_plan, execute_start, recover_start
from dev_status import collect_status
from profile_catalog import (
    ALL_PROFILE_KIND,
    PATCH_PROFILE_KIND,
    PROFILE_KINDS,
    PROFILE_OPTION,
    add_profile_argument,
    bash_profile_completion,
    collect_profile_catalog,
    profile_names,
)


_TIERS = ("quick", "canonical", "acceptance")
_HUMAN_PREVIEW_LIMIT = 8
_HUMAN_ITEM_CHARACTER_LIMIT = 160
_HUMAN_COMMAND_CHARACTER_LIMIT = 320


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


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _preview(values: list[str]) -> tuple[str, int]:
    shown = [
        _truncate(value, _HUMAN_ITEM_CHARACTER_LIMIT)
        for value in values[:_HUMAN_PREVIEW_LIMIT]
    ]
    return ",".join(shown), len(values) - len(shown)


def _replay_west_argv(
    payload: dict[str, Any], current_west_argv: list[str] | None
) -> list[str]:
    recorded = payload.get("inputs", {}).get("west_argv")
    if (
        isinstance(recorded, list)
        and recorded
        and all(isinstance(item, str) and item for item in recorded)
    ):
        return list(recorded)
    if current_west_argv:
        return list(current_west_argv)
    return _west_argv()


def _status_json_command(
    payload: dict[str, Any], current_west_argv: list[str] | None = None
) -> str:
    inputs = payload.get("inputs", {})
    argv = [
        *_replay_west_argv(payload, current_west_argv),
        "dev",
        "status",
        f"{PROFILE_OPTION}={inputs.get('profile', 'homebrew')}",
    ]
    for option, key in (
        ("--bead", "bead"),
        ("--prefix", "prefix"),
        ("--build-dir", "build_dir"),
    ):
        value = inputs.get(key)
        if value is not None:
            argv.append(f"{option}={value}")
    argv.append("--json")
    return shlex.join(argv)


def _profiles_json_command(
    kind: str,
    purposes: list[str] | None = None,
    current_west_argv: list[str] | None = None,
) -> str:
    argv = [
        *(current_west_argv or _west_argv()),
        "dev",
        "profiles",
        f"--kind={kind}",
    ]
    argv.extend(f"--purpose={purpose}" for purpose in purposes or [])
    argv.append("--json")
    return shlex.join(argv)


def _planned_json_command(
    payload: dict[str, Any], current_west_argv: list[str] | None = None
) -> str | None:
    operation = payload.get("operation")
    inputs = payload.get("inputs", {})
    west = _replay_west_argv(payload, current_west_argv)
    if operation == "start":
        argv = [
            *west,
            "dev",
            "start",
            f"--source={inputs['source']}",
            f"--destination={inputs['destination']}",
            f"--base={inputs['requested_base']}",
            f"--branch={inputs['branch']}",
            f"--bead={inputs['bead']}",
            f"--module={inputs['module']}",
            f"--evidence={inputs['evidence']}",
        ]
    elif operation == "check":
        argv = [
            *west,
            "dev",
            "check",
            str(inputs["tier"]),
            f"{PROFILE_OPTION}={inputs['profile']}",
        ]
        for option, key in (
            ("--bead", "bead"),
            ("--patch", "patch"),
            ("--prefix", "prefix"),
            ("--build-dir", "build_dir"),
        ):
            value = inputs.get(key)
            if value is not None:
                argv.append(f"{option}={value}")
        argv.append(f"--evidence={inputs['evidence']}")
    elif operation == "package":
        argv = [
            *west,
            "dev",
            "package",
            f"{PROFILE_OPTION}={inputs['profile']}",
            f"--receipt={inputs['receipt']}",
            f"--output={inputs['output']}",
            f"--evidence={inputs['evidence']}",
            f"--required-tier={inputs['required_tier']}",
        ]
    else:
        return None
    argv.extend(("--dry-run", "--json"))
    return shlex.join(argv)


def _detail_access(
    payload: dict[str, Any], current_west_argv: list[str] | None = None
) -> str | None:
    operation = payload.get("operation")
    state = payload.get("state")
    if operation == "status":
        return _status_json_command(payload, current_west_argv)
    if state == "planned":
        return _planned_json_command(payload, current_west_argv)
    if operation in {"start", "check", "package"}:
        evidence = payload.get("inputs", {}).get("evidence")
        if evidence:
            return shlex.join(["cat", "--", str(evidence)])
    return None


def _render_human(
    command: WestCommand,
    payload: dict[str, Any],
    current_west_argv: list[str] | None = None,
) -> None:
    operation = payload.get("operation", "dev")
    state = payload.get("state", "unknown")
    if operation == "profiles":
        profiles = payload.get("profiles", [])
        patch_count = sum(
            profile.get("kind") == PATCH_PROFILE_KIND for profile in profiles
        )
        runtime_count = len(profiles) - patch_count
        command.inf(
            f"west dev profiles: {state} "
            f"patch={patch_count} runtime={runtime_count}"
        )
        command.inf(
            "details: "
            + _profiles_json_command(
                payload.get("inputs", {}).get("kind", ALL_PROFILE_KIND),
                payload.get("inputs", {}).get("purposes"),
                current_west_argv,
            )
        )
        for profile in profiles[:_HUMAN_PREVIEW_LIMIT]:
            if profile.get("kind") == PATCH_PROFILE_KIND:
                base = profile.get("base_profile")
                base_text = f" base={base}" if base else ""
                description = " ".join(profile["description"].split())
                line = (
                    f"patch   {profile['name']} patches={profile['patch_count']}"
                    f"{base_text} path={profile['path']}"
                )
                if description:
                    line += f" description={description}"
            else:
                line = (
                    f"runtime {profile['name']} source={profile['source_profile']}"
                    f":{profile['source_module']} purpose={profile['purpose']}"
                    f" path={profile['path']}"
                )
            command.inf(_truncate(line, _HUMAN_ITEM_CHARACTER_LIMIT))
        omitted = len(profiles) - min(len(profiles), _HUMAN_PREVIEW_LIMIT)
        if omitted:
            command.inf(f"... {omitted} additional profiles omitted")
        return
    transaction = payload.get("transaction_id")
    suffix = f" transaction={transaction}" if transaction else ""
    command.inf(f"west dev {operation}: {state}{suffix}")
    detail_access = _detail_access(payload, current_west_argv)
    if detail_access:
        command.inf(f"details: {detail_access}")
    if operation == "status":
        for name, section in payload.get("results", payload.get("sections", {})).items():
            health = section.get("health", section.get("state", "unknown"))
            details: list[str] = []
            if name == "git":
                dirty = [
                    str(repository.get("name", repository.get("path", "?")))
                    for repository in section.get("repositories", [])
                    if repository.get("dirty") is True
                ]
                if dirty:
                    preview, omitted = _preview(dirty)
                    details.append("dirty=" + preview)
                    if omitted:
                        details.append(f"dirty_omitted={omitted}")
            active = section.get("active")
            if active:
                active_values = [str(item) for item in active]
                preview, omitted = _preview(active_values)
                details.append("active=" + preview)
                if omitted:
                    details.append(f"active_omitted={omitted}")
            invalid = section.get("invalid")
            if invalid:
                details.append(f"invalid={len(invalid)}")
            detail = f" {' '.join(details)}" if details else ""
            command.inf(f"{health:11} {name}{detail}")
    steps = payload.get("steps", [])
    if state == "planned":
        command.inf(f"plan: {len(steps)} steps")
        mutations = [step for step in steps if step.get("mutating")]
        for step in mutations[:_HUMAN_PREVIEW_LIMIT]:
            invocation = shlex.join(
                [str(part) for part in step.get("argv", [])]
            )
            command.inf(
                f"mutates: {step.get('name', 'step')} "
                + _truncate(invocation, _HUMAN_COMMAND_CHARACTER_LIMIT)
            )
        omitted = len(mutations) - min(len(mutations), _HUMAN_PREVIEW_LIMIT)
        if omitted:
            command.inf(f"... {omitted} additional mutation previews omitted")
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


def _json_error_payload(args: Any, error: Exception) -> dict[str, Any]:
    action = args.action
    operation = {
        "recover-start": "start",
        "verify-package": "package-verify",
    }.get(action, action)
    state = (
        "invalid"
        if action == "verify-package" or isinstance(error, (DevCheckError, ValueError))
        else "operational_error"
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "state": state,
        "returncode": 1,
        "action": action,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    package = getattr(args, "package", None)
    if action == "verify-package" and package is not None:
        payload["package"] = str(package.absolute())
    return payload


class DarlingDev(WestCommand):
    def __init__(self) -> None:
        super().__init__("dev", "", "Run the daily multi-repository Darling feature workflow")

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)

        profiles = subparsers.add_parser(
            "profiles", help="discover patch and CTest runtime profiles"
        )
        profiles.add_argument(
            "--kind", choices=PROFILE_KINDS, default=ALL_PROFILE_KIND
        )
        profiles.add_argument(
            "--purpose",
            action="append",
            default=[],
            help="include only runtime profiles with this purpose (repeatable)",
        )
        profile_output = profiles.add_mutually_exclusive_group()
        profile_output.add_argument("--json", action="store_true")
        profile_output.add_argument("--names", action="store_true")
        profile_output.add_argument("--completion", choices=("bash",))

        status = subparsers.add_parser("status", help="summarize local feature-work state")
        add_profile_argument(
            status, PROFILE_OPTION, PATCH_PROFILE_KIND, default="homebrew"
        )
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
        add_profile_argument(
            check, PROFILE_OPTION, PATCH_PROFILE_KIND, default="homebrew"
        )
        check.add_argument("--bead")
        check.add_argument("--patch")
        check.add_argument("--prefix", type=Path)
        check.add_argument("--build-dir", type=Path)
        check.add_argument("--evidence", type=Path, required=True)
        check.add_argument("--dry-run", action="store_true")
        check.add_argument("--json", action="store_true")

        package = subparsers.add_parser("package", help="create a checked local review package")
        add_profile_argument(
            package, PROFILE_OPTION, PATCH_PROFILE_KIND, default="homebrew"
        )
        package.add_argument("--receipt", type=Path, required=True)
        package.add_argument("--output", type=Path, required=True)
        package.add_argument("--evidence", type=Path, required=True)
        package.add_argument(
            "--required-tier",
            choices=("acceptance",),
            default="acceptance",
            help="required receipt tier (acceptance is mandatory for review packages)",
        )
        package.add_argument("--dry-run", action="store_true")
        package.add_argument("--json", action="store_true")

        verify = subparsers.add_parser(
            "verify-package", help="verify a published local review package"
        )
        verify.add_argument("package", type=Path)
        verify.add_argument("--json", action="store_true")
        return parser

    def _emit(
        self,
        payload: dict[str, Any],
        json_output: bool,
        current_west_argv: list[str] | None = None,
    ) -> None:
        if json_output:
            self.inf(json.dumps(payload, sort_keys=True, indent=2))
        else:
            _render_human(self, payload, current_west_argv)

    def do_run(self, args, unknown) -> None:
        if unknown:
            self.err(f"unknown arguments: {' '.join(unknown)}")
            raise SystemExit(2)
        if args.action == "profiles" and args.completion == "bash":
            self.inf(bash_profile_completion().rstrip("\n"))
            return
        manifest_repo = Path(self.manifest.repo_abspath).resolve()
        topdir = Path(self.topdir).resolve()
        west_argv: list[str] | None = None
        try:
            west_argv = _west_argv()
            if args.action == "profiles":
                result = collect_profile_catalog(
                    manifest_repo, args.kind, args.purpose
                )
            elif args.action == "status":
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
                        _render_human(self, plan, west_argv)
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
                        _render_human(self, plan, west_argv)
                    result = execute_check(plan)
            elif args.action == "package":
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
                        _render_human(self, plan, west_argv)
                    result = execute_package(plan)
            else:
                result = verify_package(args.package.absolute())
        except (OSError, RuntimeError, ValueError) as error:
            if args.json:
                self._emit(_json_error_payload(args, error), True, west_argv)
                raise SystemExit(1)
            self.die(str(error))
            return

        if args.action == "profiles" and args.names:
            names = profile_names(result)
            if names:
                self.inf("\n".join(names))
            return
        self._emit(result, args.json, west_argv)
        returncode = result.get("returncode", 0)
        if returncode:
            if args.json:
                raise SystemExit(returncode)
            self.die(
                f"west dev {args.action} failed with exit status {returncode}",
                exit_code=returncode,
            )
