"""Behavioral contract for idle rootless protocol-socket cleanup."""

from __future__ import annotations

import socket
import sys
import tempfile
import types
from argparse import Namespace
from pathlib import Path

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
    for root in (prefix, prefix / "libexec/darling"):
        clt = root / "Library/Developer/CommandLineTools.apple-clt-15.3"
        (clt / "usr/bin").mkdir(parents=True)
        (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
        (clt / "usr/bin/clang").write_text("#!/bin/sh\n")


def run_cleanup(prefix: Path) -> tuple[int, list[str]]:
    command = DarlingPrefixRepair()
    messages: list[str] = []
    command.inf = lambda message: messages.append(f"info: {message}")
    command.err = lambda message: messages.append(f"error: {message}")
    try:
        command.do_run(
            Namespace(prefix=[str(prefix)], extra_prefix=[], check=False, cleanup_mounts=True),
            [],
        )
    except SystemExit as error:
        return int(error.code), messages
    return 0, messages


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
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
    prefix = Path(temp)
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
    prefix = Path(temp)
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

print("PASS west-prefix-repair-runtime-socket-contract")
