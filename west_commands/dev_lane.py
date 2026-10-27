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
    lane_env,
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
)


def add_lane_parsers(subparsers: argparse._SubParsersAction) -> None:
    lane = subparsers.add_parser(
        "lane", help="run an experiment in its own isolated prefix, state and cgroup"
    )
    actions = lane.add_subparsers(dest="lane_action", required=True)

    create = actions.add_parser(
        "create", help="layer a disposable copy-on-write prefix over a verified base"
    )
    create.add_argument("name")
    create.add_argument(
        "--base",
        required=True,
        help="stable guest-state prefix used as the immutable lower layer",
    )
    create.add_argument(
        "--runtime-root",
        type=Path,
        help="selected immutable Darling installation (default: DARLING_RUNTIME_ROOT)",
    )
    create.add_argument(
        "--source",
        type=Path,
        help="independent source checkout this lane works on (recorded and verified)",
    )
    create.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT)")
    create.add_argument("--json", action="store_true")

    for name, help_text in (
        ("destroy", "stop and remove exactly this lane"),
        ("status", "report this lane's mounts, owners, scope and disk use"),
        ("verify", "assert the base and source are unchanged since creation"),
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
            f"'west dev lane create {name} --base <guest-state> --runtime-root <installation>'"
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
            source=args.source,
            runtime_root=args.runtime_root,
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
                f"{entry['name']:<24} mounted={str(status['mounted']).lower():<5} "
                f"scope={status['scope']:<8} processes={len(status['processes'])} "
                f"upper={_human_bytes(status['upper_bytes'])} "
                f"prefix={status['prefix']}"
            )
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
        payload = {"changed": [], "problems": [], "status": lane_status(lane)}
        _report(command, payload, as_json)
        if as_json:
            return
        status = payload["status"]
        command.inf(f"lane:        {status['name']} ({status['path']})")
        command.inf(f"prefix:      {status['prefix']} mounted={status['mounted']}")
        command.inf(f"state root:  {status['state_root']}")
        command.inf(f"scope:       {status['scope']} ({len(status['processes'])} owned processes)")
        command.inf(f"scope unit:  {status['scope_unit']}")
        for process in status["processes"]:
            command.inf(f"  {process}")
        for pid in status["fuse_daemons"]:
            command.inf(f"  fuse-overlayfs pid={pid}")
        command.inf(
            f"upper layer: {_human_bytes(status['upper_bytes'])} in {status['upper_files']} file(s)"
        )
        if "base" in status:
            command.inf(
                f"base:        {status['base']} present={status['base_present']} "
                f"same-identity={status.get('base_same_identity')}"
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
    environment = lane_env(lane, record)
    unit = scope_unit(lane)
    wrapped = False
    if use_scope:
        # Fail closed: a lane run has to be owned by its own cgroup or it is not a lane run.
        require_scope()
        claim = claim_scope(unit, lane=lane)
        for message in claim.changed:
            command.inf(message)
        environment[SCOPE_ACTIVE_ENV] = str(lane)
        argv = scope_wrap(unit, env_argv(environment, argv), description=scope_description(lane))
        wrapped = True
    else:
        command.err(UNSAFE_NO_SCOPE_NOTICE)
        command.err(
            f"UNSAFE-NO-SCOPE lane={lane.name} command={' '.join(argv[:2])}"
        )
        argv = env_argv(environment, argv)
    command.inf(
        f"lane {lane.name}: prefix={environment['DPREFIX']} "
        f"cgroup={'own scope ' + unit if wrapped else 'unwrapped'}"
    )
    os.execvp(argv[0], argv)


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GiB"
