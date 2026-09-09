"""Named local contexts and scenarios over the existing West/job authorities."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid


SCENARIOS = ("homebrew-prepare", "homebrew-preflight", "homebrew-source", "exact-capture")
_CONTEXT_KEYS = ("prefix", "runtime-profile", "executor", "bundle-root")


def add_scenario_parsers(subparsers) -> None:
    context = subparsers.add_parser("context", help="configure/select a named local runtime context")
    context.add_argument("name")
    context.add_argument("--prefix", type=Path)
    context.add_argument("--runtime-profile")
    context.add_argument("--executor", type=Path)
    context.add_argument("--bundle-root", type=Path)
    run = subparsers.add_parser("run", help="launch and observe a managed diagnostic scenario")
    run.add_argument("scenario", choices=SCENARIOS)
    run.add_argument("--context")
    run.add_argument("--prefix", type=Path)
    run.add_argument("--runtime-profile")
    run.add_argument("--executor", type=Path)
    run.add_argument("--bundle-root", type=Path)
    run.add_argument("--detach", action="store_true", help="start only; reconnect with dev follow JOB")
    run.add_argument("--dry-run", action="store_true")
    for name in ("follow", "cancel"):
        action = subparsers.add_parser(name, help=f"{name} an existing managed job")
        action.add_argument("job", type=Path)


def _name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("context name must contain only letters, digits, underscore or dash")
    return value


def _context(host, args) -> dict[str, str]:
    name = _name(getattr(args, "context", None) or host.config.get("dev.active-context", "homebrew"))
    values = {key: host.config.get(f"dev-{name}.{key}") for key in _CONTEXT_KEYS}
    for key in _CONTEXT_KEYS:
        override = getattr(args, key.replace("-", "_"), None)
        if override is not None:
            values[key] = str(override)
    if not values["prefix"]:
        raise ValueError(f"context {name!r} has no prefix; configure it with dev context {name} --prefix PATH")
    values["runtime-profile"] = values["runtime-profile"] or "homebrew-lz4-source"
    values["bundle-root"] = values["bundle-root"] or str(Path(host.topdir).parent / "darling-debug")
    for key in ("prefix", "bundle-root", "executor"):
        if values[key]:
            values[key] = str(Path(values[key]).expanduser().absolute())
    return {key: value for key, value in values.items() if value is not None}


def scenario_command(scenario: str, values: dict[str, str], west_argv: list[str]) -> list[str]:
    if scenario.startswith("homebrew-") and values["runtime-profile"] != "homebrew-lz4-source":
        raise ValueError("Homebrew scenarios require the homebrew-lz4-source runtime profile")
    command = [*west_argv, "test", "--prefix", values["prefix"],
               "--bundle-root", values["bundle-root"], "--executor", values["executor"]]
    if scenario == "homebrew-prepare":
        command += ["--bootstrap-runtime-profile", values["runtime-profile"]]
    elif scenario == "exact-capture":
        command += ["--diagnostic", "exact-capture", "--with-runtime-profile", values["runtime-profile"]]
    elif scenario == "homebrew-preflight":
        command += ["--profile", "wget-residual", "--patch", "darling/homebrew-prefix-tooling.patch",
                    "--env", "darling", "--reuse-prefix-runtime"]
    elif scenario == "homebrew-source":
        command += ["--profile", "homebrew", "--patch", "darling/rootless-homebrew-userland.patch",
                    "--env", "darling", "--reuse-prefix-runtime"]
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    return command


def run_scenario_action(host, args, west_argv: list[str]) -> None:
    root = Path(host.manifest.repo_abspath)
    job_tool = root / "scripts/west-job.sh"
    if args.action in ("follow", "cancel"):
        os.execv(str(job_tool), [str(job_tool), args.action, "--state-dir", str(args.job.expanduser().absolute())])
    if args.action == "context":
        from west.configuration import ConfigFile
        name = _name(args.name)
        values = {}
        for key in _CONTEXT_KEYS:
            value = getattr(args, key.replace("-", "_"))
            if value is not None:
                values[key] = str(value.expanduser().absolute()) if isinstance(value, Path) else value
        prefix = values.get("prefix") or host.config.get(f"dev-{name}.prefix")
        if not prefix:
            raise ValueError("a new context requires --prefix PATH")
        for key, value in values.items():
            host.config.set(f"dev-{name}.{key}", value, configfile=ConfigFile.LOCAL)
        host.config.set("dev.active-context", name, configfile=ConfigFile.LOCAL)
        host.inf(f"Active context: {name}; prefix={prefix}")
        return
    values = _context(host, args)
    runner = Path(host.topdir) / "darling-debug-runner"
    build_runner = "executor" not in values
    if build_runner:
        values["executor"] = str(runner / "target/release/darling-debug-runner")
    command = scenario_command(args.scenario, values, west_argv)
    if args.dry_run:
        host.inf(shlex.join(command))
        return
    if build_runner:
        subprocess.run(["cargo", "build", "--release", "--manifest-path", str(runner / "Cargo.toml")],
                       cwd=root, check=True)
    if not os.access(values["executor"], os.X_OK):
        raise ValueError(f"diagnostic executor is not executable: {values['executor']}")
    prefix = Path(values["prefix"])
    if args.scenario == "homebrew-prepare":
        prefix.mkdir(parents=True, exist_ok=True)
    elif not prefix.is_dir():
        raise ValueError(f"prefix does not exist: {prefix}; run homebrew-prepare first")
    state = Path(values["bundle-root"]) / "jobs" / f"{args.scenario}-{uuid.uuid4().hex}"
    host.inf(f"JOB={state}")
    subprocess.run([str(job_tool), "start", "--state-dir", str(state), "--", *command], cwd=root, check=True)
    entry = str(root / "bin/dw")
    host.inf("Reconnect: " + shlex.join([entry, "dev", "follow", str(state)]))
    host.inf("Cancel: " + shlex.join([entry, "dev", "cancel", str(state)]))
    if not args.detach:
        os.execv(str(job_tool), [str(job_tool), "follow", "--state-dir", str(state)])
