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

    managed = actions.add_parser(
        "run", help="own a temporary experiment from creation through evidence and retirement"
    )
    origin = managed.add_mutually_exclusive_group(required=True)
    origin.add_argument("--base", type=Path, help="inert guest-state prefix to import")
    origin.add_argument("--parent", help="frozen parent in the selected lane root")
    managed.add_argument("--root", type=Path, help="lane root (default DW_LANE_ROOT or STATE/l)")
    managed.add_argument("--state-root", type=Path, help="owner root (default DW_STATE_ROOT or workspace state)")
    managed.add_argument("--runtime-root", type=Path, help="immutable image; defaults to parent")
    managed.add_argument("--source", type=Path, help="source selection to record, never owned or deleted")
    managed.add_argument("--name", help="fresh lane name (default unique short name)")
    managed.add_argument("--allow-root", action="store_true", help="new view permits root and owner")
    managed.add_argument("--keep", action="store_true", help="retain idle lane for inspection; release with recover --release-kept")
    managed.add_argument("--pin", action="store_true", help="protect this run's evidence from bounded retention")
    managed.add_argument("--min-free-gib", type=int, default=30, help="reserve before allocation (default 30 GiB)")
    managed.add_argument("--evidence-keep-last", type=int, default=5, help="maximum ordinary completed evidence count")
    managed.add_argument("--evidence-max-gib", type=int, default=1, help="ordinary evidence budget (default 1 GiB)")
    managed.add_argument("--runtime-keep-last", type=int, default=2, help="newest managed images, plus all pins (minimum 2)")
    managed.add_argument("--json", action="store_true", help="JSON result on stdout; live command output on stderr")
    managed.add_argument("command", nargs=argparse.REMAINDER)

    for name in ("runs", "recover", "pin", "unpin", "images", "pin-runtime", "unpin-runtime"):
        action = actions.add_parser(name, help={
            "runs": "inspect managed experiment owners and final states",
            "recover": "finish only unlocked interrupted owners through their lifecycle",
            "pin": "protect a completed experiment's actual evidence",
            "unpin": "release an explicit evidence pin",
            "images": "show the managed image retention plan without deletion",
            "pin-runtime": "protect a registered immutable image",
            "unpin-runtime": "release a named manual image pin",
        }[name])
        action.add_argument("--state-root", type=Path, help="declared owner root")
        action.add_argument("--json", action="store_true")
        if name in ("recover", "pin", "unpin"):
            action.add_argument("--run", type=Path, required=name != "recover", help="managed run directory")
        if name == "recover":
            action.add_argument("--release-kept", action="store_true", help="explicitly finish kept idle lanes")
        if name in ("pin-runtime", "unpin-runtime"):
            action.add_argument("--runtime-root", type=Path, required=True)
            action.add_argument("--reason", required=True, help="stable name of the manual pin")


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


def _run_managed_action(
    command: Any, args: Any, *, topdir: Path, environ: dict[str, str],
) -> None:
    from managed_run import (
        RetentionPolicy, collect_retired, list_runs, owner_root, recover_runs,
        run_experiment, set_run_pin,
    )
    from runtime_retention import pin_runtime, prune_runtimes, unpin_runtime

    action = args.lane_action
    as_json = bool(args.json)
    root = owner_root(topdir, args.state_root, environ, create=action == "run")
    previous_root = os.environ.get("DW_STATE_ROOT")
    os.environ["DW_STATE_ROOT"] = str(root)
    try:
        if action == "run":
            argv = list(args.command)
            if argv and argv[0] == "--":
                argv = argv[1:]
            selected_root = args.root or environ.get("DW_LANE_ROOT") or root / "l"
            policy = RetentionPolicy(
                min_free_bytes=args.min_free_gib * 2**30,
                evidence_keep_last=args.evidence_keep_last,
                evidence_max_bytes=args.evidence_max_gib * 2**30,
                runtime_keep_last=args.runtime_keep_last,
            )
            payload = run_experiment(
                root, Path(selected_root).expanduser().absolute(), argv,
                base=args.base, parent=args.parent, runtime_root=args.runtime_root,
                source=args.source, name=args.name, allow_root=args.allow_root,
                keep=args.keep, pin=args.pin, policy=policy, json_output=as_json,
                emit=lambda text: print(text, file=sys.stderr if as_json else sys.stdout, flush=True),
            )
            if as_json:
                command.inf(json.dumps(payload, sort_keys=True, indent=2))
            else:
                command.inf(
                    f"run {payload['id']}: phase={payload['phase']} workload_rc={payload['workload_rc']} "
                    f"final_rc={payload['final_rc']} evidence={payload['path']}"
                )
                for problem in payload.get("cleanup", {}).get("problems", []):
                    command.err(problem)
                if payload.get("error"):
                    command.err(payload["error"])
                for row in payload.get("retention", []):
                    if row.get("action") not in ("keep", "would-delete"):
                        command.inf(
                            f"retention {row.get('action')}: {row.get('path', row.get('root', root))} "
                            f"{row.get('reason', '')}"
                        )
            raise SystemExit(payload["final_rc"])
        if action == "runs":
            payload = {"root": str(root), "runs": list_runs(root)}
        elif action == "recover":
            rows = recover_runs(root, run=args.run, release_kept=args.release_kept)
            payload = {"root": str(root), "recovery": rows, "retention": collect_retired(root, RetentionPolicy())}
        elif action in ("pin", "unpin"):
            payload = set_run_pin(root, args.run, action == "pin")
        elif action == "images":
            payload = {"root": str(root), "images": prune_runtimes(root, dry_run=True)}
        else:
            image = args.runtime_root.expanduser().absolute()
            if image != root and root not in image.parents:
                raise LaneError("image pin must stay inside its declared state root")
            key = "manual:" + args.reason
            changed = (pin_runtime if action == "pin-runtime" else unpin_runtime)(image, key)
            if not changed:
                raise LaneError("image is unregistered; it is already outside automatic retention")
            payload = {"runtime_root": str(image), "pin": key, "enabled": action == "pin-runtime"}
        command.inf(json.dumps(payload, sort_keys=True, indent=2))
        if action == "recover" and args.run and any(row["action"] != "recovered" for row in rows):
            raise LaneError("selected run could not be recovered; its state was preserved")
    finally:
        if previous_root is None:
            os.environ.pop("DW_STATE_ROOT", None)
        else:
            os.environ["DW_STATE_ROOT"] = previous_root


def run_lane_action(
    command: Any, args: Any, *, topdir: Path, environ: dict[str, str]
) -> None:
    """Dispatch one ``west dev lane`` action. Raises LaneError on refusal."""

    action = args.lane_action
    as_json = bool(getattr(args, "json", False))
    if action in {"run", "runs", "recover", "pin", "unpin", "images", "pin-runtime", "unpin-runtime"}:
        _run_managed_action(command, args, topdir=topdir, environ=environ)
        return

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
    from runtime_retention import runtime_use
    with lane_execution_lease(lane) as environment, runtime_use(Path(environment["DARLING_RUNTIME_ROOT"])):
        command.inf(
            f"lane {lane.name}: prefix={environment['DPREFIX']} "
            f"cgroup={'own scope ' + unit if use_scope else 'unwrapped'}"
        )
        try:
            child_environment = dict(os.environ, **environment)
            child_environment.pop("WEST_TEST_PREFIX_LEASE", None)
            child_environment.pop("WEST_TEST_PREFIX_LEASE_FD", None)
            if record.get("managed_run") is not None:
                from managed_run import run_lane_payload
                returncode = run_lane_payload(argv, child_environment, record["managed_run"])
            else:
                returncode = subprocess.call(argv, env=child_environment)
        except OSError as error:
            raise LaneError(f"cannot execute command in lane '{lane.name}': {error}") from error
        raise SystemExit(returncode)


def _human_bytes(count: int | None) -> str:
    if count is None:
        return "unknown"
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GiB"
