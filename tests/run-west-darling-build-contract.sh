#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import sys
import tempfile
import types
from pathlib import Path

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, *args, **kwargs):
        pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands import darling_build as db


def make_args(**overrides):
    values = {
        "targets": None,
        "deploy": True,
        "deploy_extra_prefix": [],
        "deploy_closure_names": None,
        "no_deploy_dyld": False,
        "allow_stale_dyld_for_kernel": False,
        "deploy_darlingserver": False,
        "deploy_launcher": False,
        "deploy_mldr": False,
        "deploy_shellspawn": False,
        "deploy_bootchain": False,
        "shutdown_before_deploy": False,
        "skip_post_doctor": True,
        "force": False,
        "skip_doctor": True,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def make_command():
    command = db.DarlingBuild.__new__(db.DarlingBuild)
    command.inf_messages = []
    command.wrn_messages = []
    command.err_messages = []
    command.inf = lambda message: command.inf_messages.append(message)
    command.wrn = lambda message: command.wrn_messages.append(message)
    command.err = lambda message: command.err_messages.append(message)
    command._doctor = lambda *args, **kwargs: 0
    return command


class Completed:
    returncode = 0


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    build_dir = tempdir / "build"
    prefix = tempdir / "prefix"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text("configured\n")

    command = make_command()
    deploy_calls = []
    ninja_calls = []
    command._deploy = lambda *args, **kwargs: deploy_calls.append((args, kwargs))

    original_run = db.subprocess.run
    db.subprocess.run = lambda args, **kwargs: ninja_calls.append(args) or Completed()
    try:
        command._run_locked(
            make_args(
                targets=["src/startup/mldr/mldr", "src/startup/mldr/mldr32"],
                deploy_mldr=True,
            ),
            tempdir,
            build_dir,
            prefix,
        )
    finally:
        db.subprocess.run = original_run

    assert ninja_calls == [["ninja", "src/startup/mldr/mldr", "src/startup/mldr/mldr32"]], ninja_calls
    assert len(deploy_calls) == 1, deploy_calls
    assert deploy_calls[0][1]["closure_names"] == [], deploy_calls
    assert deploy_calls[0][1]["deploy_dyld"] is False, deploy_calls
    assert deploy_calls[0][1]["deploy_mldr"] is True, deploy_calls

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    build_dir = tempdir / "build"
    prefix = tempdir / "prefix"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text("configured\n")

    command = make_command()
    command._closure_targets = lambda _build_dir, names=None: ["closure/all.dylib"] if names is None else [f"closure/{name}" for name in names]
    deploy_calls = []
    ninja_calls = []
    command._deploy = lambda *args, **kwargs: deploy_calls.append((args, kwargs))

    original_run = db.subprocess.run
    db.subprocess.run = lambda args, **kwargs: ninja_calls.append(args) or Completed()
    try:
        command._run_locked(make_args(), tempdir, build_dir, prefix)
    finally:
        db.subprocess.run = original_run

    assert ninja_calls == [["ninja", db._DYLD_TARGET, "closure/all.dylib"]], ninja_calls
    assert deploy_calls[0][1]["closure_names"] is None, deploy_calls
    assert deploy_calls[0][1]["deploy_dyld"] is True, deploy_calls

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    build_dir = tempdir / "build"
    prefix = tempdir / "prefix"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text("configured\n")

    command = make_command()
    command._closure_targets = lambda _build_dir, names=None: [f"closure/{name}" for name in names]

    try:
        command._run_locked(
            make_args(
                deploy_closure_names=["libsystem_kernel.dylib"],
                no_deploy_dyld=True,
            ),
            tempdir,
            build_dir,
            prefix,
        )
    except SystemExit as exc:
        assert exc.code == 1, exc.code
    else:
        raise AssertionError("libsystem_kernel deploy without dyld was not rejected")

print("PASS west-darling-build-contract")
PY
