#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import tempfile
import sys
import os
import socket
import types
from types import SimpleNamespace
from pathlib import Path

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.prefix_repair import (
    cleanup_prefix_mounts,
    cleanup_prefix_processes_for_mounts,
    guest_c_fixture_prerequisite_problems,
    _parse_fuser_pids,
    prefix_mount_targets,
    prefix_boot_prerequisite_problems,
    repair_prefix_prerequisites,
)


def prepare_versioned_clt(prefix: Path):
    for root in (prefix, prefix / "libexec/darling"):
        clt = root / "Library/Developer/CommandLineTools.apple-clt-15.3"
        (clt / "usr/bin").mkdir(parents=True)
        (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
        (clt / "usr/bin/clang").write_text("#!/bin/sh\n")


def prepare_root_versioned_clt(prefix: Path):
    clt = prefix / "Library/Developer/CommandLineTools.apple-clt-15.3"
    (clt / "usr/bin").mkdir(parents=True)
    (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
    (clt / "usr/bin/clang").write_text("#!/bin/sh\n")


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    prepare_versioned_clt(prefix)

    stale_pid = prefix / ".init.pid"
    stale_socket = prefix / ".darlingserver.sock"
    stale_pid.write_text("999999999\n")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(stale_socket))
    server.close()

    check = repair_prefix_prerequisites(prefix, check=True)
    assert not check.success
    assert any(".init.pid points to stale pid 999999999" in item for item in check.problems), check
    assert any(".darlingserver.sock is stale" in item for item in check.problems), check
    assert any("private/var/tmp missing" in item for item in check.problems), check
    assert any("canonical Library/Developer/CommandLineTools symlink missing" in item for item in check.problems), check
    assert any("DarlingCLT clang link missing" in item for item in check.problems), check

    repaired = repair_prefix_prerequisites(prefix)
    assert repaired.success, repaired
    assert repaired.changed, repaired
    assert not stale_pid.exists(), repaired
    assert not stale_socket.exists(), repaired
    assert prefix_boot_prerequisite_problems(prefix) == []
    assert guest_c_fixture_prerequisite_problems(
        prefix,
        "/Library/Developer/CommandLineTools/usr/bin/clang",
        "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    ) == []

    for root in (prefix, prefix / "libexec/darling"):
        clt = root / "Library/Developer/CommandLineTools"
        clang = root / "Library/Developer/DarlingCLT/usr/bin/clang"
        assert clt.is_symlink(), clt
        assert clt.resolve().name == "CommandLineTools.apple-clt-15.3", clt.resolve()
        assert (clt / "usr/bin/clang").exists(), clt
        assert clang.is_symlink(), clang
        assert clang.exists(), clang

    second = repair_prefix_prerequisites(prefix)
    assert second.success, second
    assert second.changed == [], second

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    failed = repair_prefix_prerequisites(prefix)
    assert not failed.success
    assert any("no CommandLineTools.apple-clt-*" in item for item in failed.problems), failed

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    prepare_versioned_clt(prefix)
    init_pid = prefix / ".init.pid"
    server_socket = prefix / ".darlingserver.sock"
    init_pid.write_text(f"{os.getpid()}\n")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(server_socket))
    server.close()

    repaired = repair_prefix_prerequisites(prefix)
    assert repaired.success, repaired
    assert init_pid.read_text().strip() == str(os.getpid()), repaired
    assert server_socket.exists(), repaired
    assert any(".init.pid points to live pid" in item for item in repaired.ok), repaired

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    prepare_root_versioned_clt(prefix)
    repaired = repair_prefix_prerequisites(prefix)
    assert repaired.success, repaired
    base_candidate = prefix / "libexec/darling/Library/Developer/CommandLineTools.apple-clt-15.3"
    assert base_candidate.is_symlink(), base_candidate
    assert (base_candidate / "usr/bin/clang").exists(), base_candidate
    assert guest_c_fixture_prerequisite_problems(
        prefix,
        "/Library/Developer/CommandLineTools/usr/bin/clang",
        "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    ) == []

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    proc = prefix / "proc"
    proc.mkdir()
    mountinfo = prefix / "mountinfo"
    mountinfo.write_text(
        f"10 1 0:1 / {proc} rw,relatime - proc proc rw\n"
        f"11 10 0:2 / {proc} rw,relatime - proc proc rw\n"
        "12 1 0:3 / /other rw,relatime - proc proc rw\n"
    )
    assert prefix_mount_targets(prefix, mountinfo_path=mountinfo) == [proc, proc]

    calls = []

    def fake_runner(command, capture_output, text, check):
        calls.append((command, capture_output, text, check))
        if len(calls) == 2:
            mountinfo.write_text("")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cleaned = cleanup_prefix_mounts(prefix, runner=fake_runner, mountinfo_path=mountinfo)
    assert cleaned.success, cleaned
    assert len(calls) == 2, calls
    assert all(call[0] == ["umount", str(proc)] for call in calls), calls

assert _parse_fuser_pids("/tmp/prefix-123/proc: 444209 444214c\n") == {444209, 444214}

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    proc = prefix / "proc"
    proc.mkdir()
    mountinfo = prefix / "mountinfo"
    mountinfo.write_text(f"10 1 0:1 / {proc} rw,relatime - proc proc rw\n")
    runner_calls = []
    kill_calls = []
    alive = {444209}

    def fake_runner(command, capture_output, text, check):
        runner_calls.append(command)
        if command[:2] == ["fuser", "-m"]:
            return SimpleNamespace(returncode=0, stdout="", stderr=f"{command[2]}: 444209\n")
        if command[0] == "umount":
            if kill_calls:
                mountinfo.write_text("")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=32, stdout="", stderr="target is busy")
        raise AssertionError(command)

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError(pid)
            return
        alive.discard(pid)

    cleaned = cleanup_prefix_mounts(
        prefix,
        runner=fake_runner,
        mountinfo_path=mountinfo,
        kill_func=fake_kill,
        sleep_func=lambda _seconds: None,
        argv_for_pid=lambda pid: ["/usr/libexec/darling/mldr", "/sbin/launchd"] if pid == 444209 else [],
    )
    assert cleaned.success, cleaned
    assert ["fuser", "-m", str(proc)] in runner_calls, runner_calls
    assert (444209, 15) in kill_calls, kill_calls
    assert any("unmounted" in item and "after killing" in item for item in cleaned.changed), cleaned

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    proc = prefix / "proc"
    proc.mkdir()
    cleaned = cleanup_prefix_processes_for_mounts(
        [proc],
        runner=lambda command, capture_output, text, check: SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=f"{command[2]}: 555\n",
        ),
        kill_func=lambda pid, sig: (_ for _ in ()).throw(AssertionError("non-Darling pid killed")),
        sleep_func=lambda _seconds: None,
        argv_for_pid=lambda _pid: ["/usr/bin/bash"],
    )
    assert cleaned.success, cleaned
    assert cleaned.changed == [], cleaned
    assert any("no Darling runtime processes" in item for item in cleaned.ok), cleaned

print("PASS west-prefix-repair-contract")
PY
