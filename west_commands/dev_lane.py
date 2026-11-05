"""``west dev lane`` — parallel-experiment isolation for the Darling workflow.

This is the human-facing surface of `lane.py`. It owns argument shapes and
reporting only; every rule about what a lane is, and what is safe to do to one,
lives in the module so the contracts can test it without a West command.

`west dev lane exec` is the narrow integration point for everything that is not
`west test`: it runs any existing command (a `dwdiag cycle`, a
`west darling-build --deploy`, a guest workload) with the lane's prefix, state
root, temporary directories and cgroup ownership.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lane import (
    SCOPE_ACTIVE_ENV,
    UNSAFE_NO_SCOPE_NOTICE,
    LaneError,
    claim_scope,
    create_lane,
    destroy_lane,
    env_argv,
    freeze_lane,
    lane_env,
    lane_execution_lease,
    lane_path,
    lane_root,
    lane_status,
    list_lanes,
    read_record,
    require_scope,
    scope_description,
    scope_unit,
    scope_wrap,
    validate_lane_name,
    verify_lane,
    west_command_argv,
)


def add_lane_parsers(subparsers: argparse._SubParsersAction) -> None:
    lane = subparsers.add_parser(
        "lane", help="run an experiment in its own isolated prefix, state and cgroup"
    )
    actions = lane.add_subparsers(dest="lane_action", required=True)

    create = actions.add_parser(
        "create", help="create a disposable copy-on-write prefix from a base or frozen parent"
    )
    create.add_argument("name")
    origin = create.add_mutually_exclusive_group(required=True)
    origin.add_argument(
        "--base",
        type=Path,
        help="stable guest-state prefix to import into the snapshot store",
    )
    origin.add_argument(
        "--parent",
        help="frozen lane in this lane root whose snapshot the new lane inherits",
    )
    create.add_argument(
        "--runtime-root",
        type=Path,
        help="immutable Darling installation (default: parent, then DARLING_RUNTIME_ROOT)",
    )
    create.add_argument(
        "--source",
        type=Path,
        help="independent source checkout to record and verify (default: parent's source)",
    )
    create.add_argument(
        "--allow-root",
        action="store_true",
        help=(
            "mount this lane's view for root and its owner (provider allow_root) "
            "instead of owner-only; requires 'user_allow_other' in /etc/fuse.conf "
            "and is not inherited by child lanes"
        ),
    )
    create.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT)")
    create.add_argument("--json", action="store_true")

    for name, help_text in (
        ("destroy", "stop and remove exactly this lane"),
        ("freeze", "commit this idle lane as a parent snapshot; retain its mounted view"),
        ("status", "report this lane's mounts, owners, scope and disk use"),
        ("verify", "verify managed layers, retained mount, runtime and source identities"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("name")
        action.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT)")
        action.add_argument("--json", action="store_true")

    listing = actions.add_parser("list", help="list the lanes in the lane root")
    listing.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT)")
    listing.add_argument("--json", action="store_true")

    execute = actions.add_parser(
        "exec", help="run a command inside one lane (prefix, state root, cgroup)"
    )
    execute.add_argument("name")
    execute.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT)")
    execute.add_argument(
        "--no-scope",
        action="store_true",
        help=(
            "run WITHOUT the lane's cgroup: " + UNSAFE_NO_SCOPE_NOTICE
        ),
    )
    execute.add_argument("command", nargs=argparse.REMAINDER)


def _lane_argument_root(args: Any, topdir: Path, environ: dict[str, str]) -> Path:
    declared = getattr(args, "root", None)
    if declared is not None:
        return declared.expanduser().absolute()
    return lane_root(environ, topdir).absolute()


def _resolve_lane(args: Any, topdir: Path, environ: dict[str, str]) -> Path:
    root = _lane_argument_root(args, topdir, environ)
    name = validate_lane_name(args.name)
    path = lane_path(root, name)
    if read_record(path) is None:
        raise LaneError(
            f"no lane '{name}' under {root}; create it with "
            f"'west dev lane create {name} (--base <guest-state> | --parent <frozen-lane>)'"
        )
    return path


def _report(command: Any, payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        command.inf(json.dumps(payload, sort_keys=True, indent=2))
        return
    for message in payload.get("changed", ()):
        command.inf(message)
    for message in payload.get("problems", ()):
        command.err(message)


def run_lane_action(
    command: Any, args: Any, *, topdir: Path, environ: dict[str, str]
) -> None:
    """Dispatch one ``west dev lane`` action. Raises LaneError on refusal."""

    action = args.lane_action
    as_json = bool(getattr(args, "json", False))

    if action == "create":
        root = _lane_argument_root(args, topdir, environ)
        result, lane = create_lane(
            args.base,
            root,
            args.name,
            parent=args.parent,
            source=args.source,
            runtime_root=args.runtime_root,
            allow_root=args.allow_root,
        )
        payload = {
            "changed": result.changed + [f"lane path: {lane}"],
            "problems": result.problems,
            "lane": str(lane),
            "prefix": str(lane / "merged"),
            "state_root": str(lane / "state"),
        }
        _report(command, payload, as_json)
        if not result.success:
            raise LaneError("lane created with problems; see above")
        return

    if action == "list":
        root = _lane_argument_root(args, topdir, environ)
        lanes = [
            {
                "name": path.name,
                "path": str(path),
                "status": lane_status(path),
            }
            for path in list_lanes(root)
        ]
        payload = {"changed": [], "problems": [], "root": str(root), "lanes": lanes}
        if as_json:
            _report(command, payload, True)
            return
        if not lanes:
            command.inf(f"no lanes under {root}")
            return
        for entry in lanes:
            status = entry["status"]
            command.inf(
                f"{entry['name']:<24} mode={status['mode']} "
                f"mounted={str(status['mounted']).lower():<5} "
                f"scope={status['scope']:<8} processes={len(status['processes'])} "
                f"snapshot={status['snapshot']} parent={status['parent'] or '-'} "
                f"children={','.join(status['children']) or '-'} "
                f"layer={_human_bytes(status['layer_bytes'])} inodes={status['layer_inodes']} "
                f"prefix={status['prefix']}"
            )
            for problem in status["problems"]:
                command.err(f"lane '{entry['name']}': {problem}")
        return

    if action == "exec":
        lane = _resolve_lane(args, topdir, environ)
        record = read_record(lane) or {}
        argv = list(args.command)
        if argv and argv[0] == "--":
            argv = argv[1:]
        if argv and argv[0] in ("--root", "--no-scope"):
            # `command` is argparse REMAINDER, so an option written after the lane name is part of
            # the command and reaches `env` as a program name. MEASURED: that reads as
            # `/usr/bin/env: '--root': No such file or directory` and says nothing about the cause.
            raise LaneError(
                f"'{argv[0]}' after the lane name is the command's, not this option's; write "
                f"'west dev lane exec {argv[0]} ... {args.name} -- <command>'"
            )
        if not argv:
            raise LaneError("west dev lane exec requires a command after the lane name")
        _exec_in_lane(command, lane, record, argv, use_scope=not args.no_scope)
        return

    lane = _resolve_lane(args, topdir, environ)
    if action == "status":
        status = lane_status(lane)
        payload = {"changed": [], "problems": status["problems"], "status": status}
        _report(command, payload, as_json)
        if as_json:
            return
        command.inf(f"lane:        {status['name']} ({status['path']})")
        command.inf(f"mode:        {status['mode']}")
        command.inf(f"snapshot:    {status['snapshot']} ({status['snapshot_kind']})")
        command.inf(f"parent:      {status['parent'] or '-'}")
        command.inf(f"children:    {', '.join(status['children']) or '-'}")
        command.inf(f"store:       {status['storage_root']}")
        command.inf(f"prefix:      {status['prefix']} mounted={status['mounted']}")
        command.inf(f"state root:  {status['state_root']}")
        command.inf(f"scope:       {status['scope']} ({len(status['processes'])} owned processes)")
        command.inf(f"scope unit:  {status['scope_unit']}")
        for process in status["processes"]:
            command.inf(f"  {process}")
        for pid in status["fuse_daemons"]:
            command.inf(f"  fuse-overlayfs pid={pid}")
        command.inf(
            f"layer:       {_human_bytes(status['layer_bytes'])} in {status['layer_inodes']} inode(s)"
        )
        return

    if action == "verify":
        result = verify_lane(lane)
        payload = {
            "changed": result.changed,
            "problems": result.problems,
            "lane": str(lane),
        }
        _report(command, payload, as_json)
        if not result.success:
            raise LaneError("lane verification failed")
        return

    if action == "freeze":
        result = freeze_lane(lane)
        payload = {"changed": result.changed, "problems": result.problems, "lane": str(lane)}
        _report(command, payload, as_json)
        if not result.success:
            raise LaneError("lane freeze refused; see the problems above")
        return

    if action == "destroy":
        result = destroy_lane(lane)
        payload = {"changed": result.changed, "problems": result.problems, "lane": str(lane)}
        _report(command, payload, as_json)
        if not result.success:
            raise LaneError("lane removal refused; see the problems above")
        return

    raise LaneError(f"unknown lane action {action!r}")


def _exec_in_lane(
    command: Any, lane: Path, record: dict[str, Any], argv: list[str], *, use_scope: bool
) -> None:
    unit = scope_unit(lane)
    if use_scope and os.environ.get(SCOPE_ACTIVE_ENV) != str(lane):
        environment = lane_env(lane, record)
        require_scope()
        claim = claim_scope(unit, lane=lane)
        for message in claim.changed:
            command.inf(message)
        environment[SCOPE_ACTIVE_ENV] = str(lane)
        reentry = [
            *west_command_argv(), "dev", "lane", "exec",
            "--root", str(lane.parent), lane.name, "--", *argv,
        ]
        wrapped = scope_wrap(
            unit, env_argv(environment, reentry), description=scope_description(lane)
        )
        command.inf(f"lane {lane.name}: re-entering under cgroup {unit}")
        try:
            os.execvp(wrapped[0], wrapped)
        except OSError as error:
            raise LaneError(f"cannot re-enter lane '{lane.name}' under {unit}: {error}") from error
    if not use_scope:
        command.err(UNSAFE_NO_SCOPE_NOTICE)
        command.err(f"UNSAFE-NO-SCOPE lane={lane.name} command={' '.join(argv[:2])}")
    # Admission must happen again inside the scope, after any concurrent freeze,
    # and the controller must retain the lease until the actual command exits.
    with lane_execution_lease(lane) as environment:
        command.inf(
            f"lane {lane.name}: prefix={environment['DPREFIX']} "
            f"cgroup={'own scope ' + unit if use_scope else 'unwrapped'}"
        )
        try:
            child_environment = dict(os.environ, **environment)
            child_environment.pop("WEST_TEST_PREFIX_LEASE", None)
            child_environment.pop("WEST_TEST_PREFIX_LEASE_FD", None)
            completed = subprocess.run(argv, env=child_environment)
        except OSError as error:
            raise LaneError(f"cannot execute command in lane '{lane.name}': {error}") from error
        raise SystemExit(completed.returncode)


def _human_bytes(count: int | None) -> str:
    if count is None:
        return "unknown"
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GiB"
