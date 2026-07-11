import os
import signal
import subprocess
import tempfile
import time
import sys
import types
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest
from west_commands.test_prefix import (
    cleanup_rootless_prefix_processes,
    darlingserver_pids_for_prefix,
    prefix_process_snapshot,
    remove_stale_init_pid,
    rootless_prefix_process_snapshot,
)


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.inf = lambda *args, **kwargs: None
    test.wrn = lambda *args, **kwargs: None
    test.err_messages = []
    test.err = lambda message: test.err_messages.append(message)
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    return test


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    fallback = root / "work" / "darling-prefix" / "bin" / "darling"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("launcher\n")
    explicit = root / "explicit-prefix"
    old_home = os.environ.get("HOME")
    old_darling = os.environ.pop("DARLING", None)
    old_launcher = os.environ.pop("DARLING_LAUNCHER", None)
    os.environ["HOME"] = str(root)
    try:
        test = make_test()
        assert test._resolve_darling_launcher(str(explicit)) is None
        explicit_launcher = explicit / "bin" / "darling"
        explicit_launcher.parent.mkdir(parents=True)
        explicit_launcher.write_text("launcher\n")
        assert test._resolve_darling_launcher(str(explicit)) == str(explicit_launcher)
        assert test._resolve_darling_launcher(None) == str(fallback)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_darling is not None:
            os.environ["DARLING"] = old_darling
        if old_launcher is not None:
            os.environ["DARLING_LAUNCHER"] = old_launcher


test = make_test()
prefix = Path("/tmp/west-test-prefix-contract")
entries = [
    (100, 1, f"darlingserver {prefix} 1000 1000 4 0"),
    (101, 100, "/sbin/launchd"),
    (102, 101, "/usr/libexec/shellspawn"),
    (200, 1, "darlingserver /tmp/other-prefix 1000 1000 4 0"),
]
helper_snapshot = prefix_process_snapshot(prefix, entries)
assert any(line.startswith("100 darlingserver ") for line in helper_snapshot), helper_snapshot
assert any(line == "101 /sbin/launchd" for line in helper_snapshot), helper_snapshot
assert any(line == "102 /usr/libexec/shellspawn" for line in helper_snapshot), helper_snapshot
assert all("other-prefix" not in line for line in helper_snapshot), helper_snapshot
assert darlingserver_pids_for_prefix(prefix, entries) == [100]

test._prefix = str(prefix)
test._keep_prefix_running = False
test._resolve_darling_launcher = lambda _prefix: None
test._kill_dserver_for_prefix = lambda _prefix: None
test._ps_entries = lambda: entries

snapshot = test._prefix_process_snapshot(prefix)
assert any(line.startswith("100 darlingserver ") for line in snapshot), snapshot
assert any(line == "101 /sbin/launchd" for line in snapshot), snapshot
assert any(line == "102 /usr/libexec/shellspawn" for line in snapshot), snapshot
assert all("other-prefix" not in line for line in snapshot), snapshot
assert not test._shutdown_test_prefix()
assert any("leftover Darling prefix" in line for line in test.err_messages), test.err_messages

test = make_test()
test._prefix = str(prefix)
test._keep_prefix_running = False
test._resolve_darling_launcher = lambda _prefix: None
test._kill_dserver_for_prefix = lambda _prefix: None
test._ps_entries = lambda: []
assert test._shutdown_test_prefix()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    tagged = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={
            **os.environ,
            "DARLING_PREFIX": str(prefix),
            "DARLING_ROOTLESS": "1",
        },
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "DARLING_PREFIX": str(prefix)},
    )
    try:
        for _ in range(20):
            if any(entry.startswith(f"{tagged.pid} ") for entry in rootless_prefix_process_snapshot(prefix)):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("tagged rootless process was not discovered")
        result = cleanup_rootless_prefix_processes(prefix)
        assert result.success and result.changed, result
        tagged.wait(timeout=3)
        assert unrelated.poll() is None, "cleanup matched an untagged prefix process"
    finally:
        for process in (tagged, unrelated):
            if process.poll() is None:
                process.kill()
                process.wait()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    tagged = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={
            **os.environ,
            "DARLING_PREFIX": str(prefix),
            "DARLING_ROOTLESS": "1",
        },
    )
    test = make_test()
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._cleanup_prefix_mounts = lambda _prefix: True
    test._remove_stale_init_pid = lambda _prefix: None
    try:
        assert test._shutdown_runtime_prefix(prefix)
        tagged.wait(timeout=3)
    finally:
        if tagged.poll() is None:
            tagged.kill()
            tagged.wait()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    launcher = prefix / "fake-darling"
    child_pid = prefix / "shutdown-child.pid"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
(sleep 30) &
echo "$!" > "${WEST_SHUTDOWN_CHILD_PID:?}"
sleep 30
"""
    )
    launcher.chmod(0o755)
    test = make_test()
    test._prefix = str(prefix)
    test._keep_prefix_running = False
    test._resolve_darling_launcher = lambda _prefix: str(launcher)
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._ps_entries = lambda: []
    old_timeout = os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS")
    old_child_pid = os.environ.get("WEST_SHUTDOWN_CHILD_PID")
    os.environ["WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS"] = "1"
    os.environ["WEST_SHUTDOWN_CHILD_PID"] = str(child_pid)
    try:
        started = time.monotonic()
        assert test._shutdown_test_prefix()
        assert time.monotonic() - started < 5
        pid = int(child_pid.read_text())
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.05)
        assert not Path(f"/proc/{pid}").exists(), f"timed out shutdown child survived: {pid}"
        assert any("shutdown timed out" in message for message in test.err_messages)
    finally:
        if old_timeout is None:
            os.environ.pop("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", None)
        else:
            os.environ["WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS"] = old_timeout
        if old_child_pid is None:
            os.environ.pop("WEST_SHUTDOWN_CHILD_PID", None)
        else:
            os.environ["WEST_SHUTDOWN_CHILD_PID"] = old_child_pid
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

with tempfile.TemporaryDirectory() as temp:
    stale_prefix = Path(temp)
    (stale_prefix / ".init.pid").write_text("999999999\n")
    assert remove_stale_init_pid(stale_prefix, pid_is_usable=lambda _pid: False)
    (stale_prefix / ".init.pid").write_text("999999999\n")
    test._remove_stale_init_pid(stale_prefix)
    assert not (stale_prefix / ".init.pid").exists()

test = make_test()
with tempfile.TemporaryDirectory() as temp:
    test._prefix = temp
    test._shutdown_test_prefix = lambda: False
    try:
        with test._prefix_resource_context(True):
            raise AssertionError("failed prefix reset unexpectedly yielded")
    except SystemExit as exc:
        assert str(exc) == f"could not reset Darling prefix before test run: {temp}", exc
    else:
        raise AssertionError("failed prefix reset unexpectedly passed")
    assert test._prefix_cleanup_failed

test = make_test()
with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    problems = test._prefix_boot_prerequisite_problems(prefix)
    assert "private/var/tmp missing in Darling prefix" in problems, problems
    assert "libexec/darling/private/var/tmp missing in Darling prefix" in problems, problems

    (prefix / "private/var/tmp").mkdir(parents=True)
    (prefix / "libexec/darling/private/var/tmp").mkdir(parents=True)
    (prefix / "private/var/tmp").chmod(0o755)
    (prefix / "libexec/darling/private/var/tmp").chmod(0o1777)
    problems = test._prefix_boot_prerequisite_problems(prefix)
    assert "private/var/tmp mode 755, expected 1777" in problems, problems
    assert all("libexec/darling/private/var/tmp" not in item for item in problems), problems

    (prefix / "private/var/tmp").chmod(0o1777)
    assert test._prefix_boot_prerequisite_problems(prefix) == []

    problems = test._guest_c_fixture_prerequisite_problems(
        prefix,
        "/Library/Developer/CommandLineTools/usr/bin/clang",
        "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    )
    assert any("guest compiler missing in prefix root" in item for item in problems), problems
    assert any("guest SDK sysroot missing in base tree" in item for item in problems), problems

    for root in (prefix, prefix / "libexec/darling"):
        (root / "Library/Developer/CommandLineTools/usr/bin").mkdir(parents=True)
        (root / "Library/Developer/CommandLineTools/usr/bin/clang").write_text("")
        (root / "Library/Developer/CommandLineTools/SDKs/MacOSX.sdk").mkdir(parents=True)
    assert test._guest_c_fixture_prerequisite_problems(
        prefix,
        "/Library/Developer/CommandLineTools/usr/bin/clang",
        "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    ) == []

test = make_test()
test.manifest = types.SimpleNamespace(repo_abspath=str(Path.cwd()), projects=[])
test.topdir = str(Path.cwd())
args = Namespace(prefix=None, prefix_profile="homebrew", no_overlayfs=False)
prefix = test._resolve_prefix(args)
assert prefix.endswith("darling-prefix-homebrew-test"), prefix
assert test._prefix_env == {"DARLING_NOOVERLAYFS": "1"}, test._prefix_env

args = Namespace(prefix="/tmp/custom-prefix", prefix_profile=None, no_overlayfs=True)
prefix = test._resolve_prefix(args)
assert prefix == "/tmp/custom-prefix", prefix
assert test._prefix_env == {"DARLING_NOOVERLAYFS": "1"}, test._prefix_env

print("PASS west-test-prefix-cleanup-contract")
