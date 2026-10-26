"""Behavioral contract for idle rootless protocol-socket cleanup."""

from __future__ import annotations

import socket
import argparse
import sys
import tempfile
import types
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, *_args, **_kwargs):
        pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import west_commands.darling_prefix_repair as repair_command_module
from west_commands.darling_prefix_repair import DarlingPrefixRepair
from west_commands.prefix_repair import PrefixRepairResult


def prepare_prefix(prefix: Path) -> None:
    clt = prefix / "Library/Developer/CommandLineTools.apple-clt-15.3"
    (clt / "usr/bin").mkdir(parents=True)
    (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
    (clt / "usr/bin/clang").write_text("#!/bin/sh\n")
    runtime = prefix.parent / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/darling").write_text("#!/bin/sh\nexit 0\n")
    (runtime / "bin/darling").chmod(0o755)
    (runtime / "bin/darlingserver").write_text("server")
    (runtime / "libexec/darling").mkdir(parents=True)


def run_cleanup(prefix: Path) -> tuple[int, list[str]]:
    command = DarlingPrefixRepair()
    messages: list[str] = []
    command.inf = lambda message: messages.append(f"info: {message}")
    command.err = lambda message: messages.append(f"error: {message}")
    try:
        command.do_run(
            Namespace(
                prefix=[str(prefix)], extra_prefix=[], check=False, cleanup_mounts=True,
                runtime_root=str(prefix.parent / "runtime"),
            ),
            [],
        )
    except SystemExit as error:
        return int(error.code), messages
    return 0, messages


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "guest"
    prepare_prefix(prefix)
    runtime_socket = prefix / "var/run/shellspawn.sock"
    runtime_socket.parent.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(runtime_socket))
    listener.close()

    code, messages = run_cleanup(prefix)
    assert code == 0, messages
    assert not runtime_socket.exists(), messages
    assert any("removed stale rootless runtime socket" in message for message in messages), messages

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "guest"
    prepare_prefix(prefix)
    runtime_socket = prefix / "var/run/shellspawn.sock"
    runtime_socket.parent.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(runtime_socket))
    listener.close()

    original_snapshot = repair_command_module.rootless_prefix_process_snapshot
    repair_command_module.rootless_prefix_process_snapshot = lambda _prefix: [
        "123 /usr/libexec/shellspawn"
    ]
    try:
        code, messages = run_cleanup(prefix)
    finally:
        repair_command_module.rootless_prefix_process_snapshot = original_snapshot
    assert code == 1, messages
    assert runtime_socket.exists(), messages
    assert any("refusing rootless runtime socket cleanup" in message for message in messages), messages

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "guest"
    prepare_prefix(prefix)
    runtime_socket = prefix / "var/run/shellspawn.sock"
    runtime_socket.parent.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(runtime_socket))
    listener.close()

    original_cleanup_mounts = repair_command_module.cleanup_prefix_mounts
    original_mount_targets = repair_command_module.prefix_mount_targets
    repair_command_module.cleanup_prefix_mounts = lambda _prefix: PrefixRepairResult()
    repair_command_module.prefix_mount_targets = lambda _prefix: [prefix / "proc"]
    try:
        code, messages = run_cleanup(prefix)
    finally:
        repair_command_module.cleanup_prefix_mounts = original_cleanup_mounts
        repair_command_module.prefix_mount_targets = original_mount_targets
    assert code == 1, messages
    assert runtime_socket.exists(), messages
    assert any("while prefix mounts remain" in message for message in messages), messages

def snapshot(root: Path):
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            str(path.readlink()) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    prefix = base / "guest"
    prepare_prefix(prefix)
    runtime = base / "runtime"
    before = snapshot(runtime)
    parser = argparse.ArgumentParser().add_subparsers()
    command = DarlingPrefixRepair()
    # The minimal West stand-in intentionally does not retain command metadata.
    command.name = "darling-prefix-repair"
    command.description = "Repair guest state"
    command_parser = command.do_add_parser(parser)
    args = command_parser.parse_args(["--runtime-root", str(runtime), "--prefix", str(prefix)])
    command.inf = lambda *_: None
    command.err = lambda *_: None
    command.do_run(args, [])
    assert (prefix / "Library/Developer/CommandLineTools").is_symlink()
    assert not (runtime / "libexec/darling/Library").exists()
    assert snapshot(runtime) == before
    with patch.dict("os.environ", {"DARLING_RUNTIME_ROOT": str(runtime), "DPREFIX": str(prefix)}, clear=True):
        args = command_parser.parse_args([])
        command.do_run(args, [])
    # All selections must be validated before the first repair/cleanup.
    alias = base / "alias"
    alias.symlink_to(prefix, target_is_directory=True)
    legacy = base / "legacy"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin/darling").write_text("legacy launcher")
    (legacy / "libexec/darling").mkdir(parents=True)
    for rejected in (str(prefix) + "/../victim", str(alias), str(runtime),
                     str(runtime / "guest"), str(base), str(legacy)):
        args = command_parser.parse_args([
            "--runtime-root", str(runtime), "--prefix", str(prefix),
            "--extra-prefix", rejected, "--cleanup-mounts",
        ])
        with patch.object(repair_command_module, "repair_prefix_prerequisites") as repair, \
             patch.object(repair_command_module, "cleanup_prefix_mounts") as cleanup:
            try:
                command.do_run(args, [])
            except SystemExit as error:
                assert error.code == 1
            else:
                raise AssertionError(f"unsafe guest prefix accepted: {rejected}")
            repair.assert_not_called()
            cleanup.assert_not_called()
    assert snapshot(runtime) == before
    lower_clt = runtime / "libexec/darling/Library/Developer/CommandLineTools"
    (lower_clt / "usr/bin").mkdir(parents=True)
    (lower_clt / "usr/bin/clang").write_bytes(b"immutable installed compiler")
    (lower_clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
    lower_prefix = base / "lower-guest"
    lower_prefix.mkdir()
    before = snapshot(runtime)
    args = command_parser.parse_args([
        "--runtime-root", str(runtime), "--prefix", str(lower_prefix),
    ])
    command.do_run(args, [])
    assert list(lower_prefix.iterdir()) == [], "repair shadowed installed CLT into guest state"
    assert snapshot(runtime) == before
    with patch.dict("os.environ", {"DARLING_LAUNCHER": str(runtime / "bin/darling")}, clear=True):
        args = command_parser.parse_args(["--prefix", str(lower_prefix)])
        command.do_run(args, [])
    # No selected installation may be replaced by a prefix-local launcher.
    with patch.dict("os.environ", {"PATH": "", "DARLING_PREFIX": str(legacy)}, clear=True), \
         patch.object(repair_command_module, "repair_prefix_prerequisites") as repair:
        try:
            command.do_run(command_parser.parse_args([]), [])
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("legacy prefix launcher supplied a missing installation")
        repair.assert_not_called()
    assert snapshot(runtime) == before

print("PASS west-prefix-repair-runtime-socket-contract")
