#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import tempfile
import sys
import os
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
    guest_c_fixture_prerequisite_problems,
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
    stale_pid.write_text("999999999\n")
    check = repair_prefix_prerequisites(prefix, check=True)
    assert not check.success
    assert any(".init.pid points to stale pid 999999999" in item for item in check.problems), check
    assert any("private/var/tmp missing" in item for item in check.problems), check
    assert any("canonical Library/Developer/CommandLineTools symlink missing" in item for item in check.problems), check
    assert any("DarlingCLT clang link missing" in item for item in check.problems), check

    repaired = repair_prefix_prerequisites(prefix)
    assert repaired.success, repaired
    assert repaired.changed, repaired
    assert not stale_pid.exists(), repaired
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
    init_pid.write_text(f"{os.getpid()}\n")

    repaired = repair_prefix_prerequisites(prefix)
    assert repaired.success, repaired
    assert init_pid.read_text().strip() == str(os.getpid()), repaired
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

print("PASS west-prefix-repair-contract")
PY
