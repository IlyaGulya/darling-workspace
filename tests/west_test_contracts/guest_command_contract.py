"""Behavioral contract for the structured guest command runner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import west_commands.test_guest_execution as guest_execution
from west_commands.test_execution import ProcessResult
from west_commands.test_guest_execution import (
    failure_phase_from_output,
    resolve_guest_execution,
    run_guest_argv,
    run_guest_argv_fixture,
    run_guest_shell_argv,
    run_guest_shell,
    run_guest_command_fixture,
)

assert failure_phase_from_output(
    "Rootless shellspawn did not become ready within 60000ms\n"
) == "bootstrap"
assert failure_phase_from_output("guest script failed\n") is None


def fail(message: str) -> None:
    raise AssertionError(message)


resolved = resolve_guest_execution(
    name="guest_resolution_contract",
    env={"DPREFIX": "/explicit-prefix", "DARLING_LAUNCHER": "/explicit-launcher"},
    fallback_prefix="/fallback-prefix",
    resolve_launcher=lambda _prefix: (_ for _ in ()).throw(AssertionError("unexpected resolver")),
    die=fail,
)
assert resolved.prefix == "/explicit-prefix"
assert resolved.launcher == "/explicit-launcher"

resolved = resolve_guest_execution(
    name="guest_resolution_fallback_contract",
    env={},
    fallback_prefix="/fallback-prefix",
    resolve_launcher=lambda prefix: f"{prefix}/bin/darling",
    die=fail,
)
assert resolved.prefix == "/fallback-prefix"
assert resolved.launcher == "/fallback-prefix/bin/darling"


calls = []
original_bounded = guest_execution.run_bounded


def bounded(args, **kwargs):
    calls.append((args, kwargs))
    return ProcessResult(0)


guest_execution.run_bounded = bounded
try:
    assert run_guest_argv(
        "/launcher",
        "/prefix",
        ("/usr/bin/true",),
        cwd=Path("/workspace"),
        env={"EXAMPLE": "1"},
        timeout_seconds=7,
        capture_output=True,
    ).returncode == 0
finally:
    guest_execution.run_bounded = original_bounded

assert calls == [
    (
        [
            "env",
            "DPREFIX=/prefix",
            "DARLING_PREFIX=/prefix",
            "/launcher",
            "exec",
            "/usr/bin/true",
        ],
        {
            "cwd": Path("/workspace"),
            "env": {"EXAMPLE": "1"},
            "timeout_seconds": 7,
            "stdout": None,
            "stderr": None,
            "text": True,
            "capture_output": True,
            "heartbeat_seconds": None,
            "heartbeat": None,
            "output_line": None,
        },
    )
], calls

calls = []
guest_execution.run_bounded = bounded
try:
    assert run_guest_shell(
        "/launcher",
        "/prefix",
        "printf shell",
        cwd=Path("/workspace"),
        env={"EXAMPLE": "1"},
        timeout_seconds=7,
        capture_output=True,
    ).returncode == 0
finally:
    guest_execution.run_bounded = original_bounded

assert calls[0][0] == [
    "env",
    "DPREFIX=/prefix",
    "DARLING_PREFIX=/prefix",
    "/launcher",
    "shell",
    "/bin/bash",
    "--login",
    "-c",
    "printf shell",
], calls

heartbeat = lambda _elapsed: None
output_line = lambda _stream, _line: None
calls = []
guest_execution.run_bounded = bounded
try:
    assert run_guest_shell(
        "/launcher",
        "/prefix",
        "printf shell",
        cwd=Path("/workspace"),
        env={"EXAMPLE": "1"},
        timeout_seconds=7,
        capture_output=True,
        heartbeat_seconds=30,
        heartbeat=heartbeat,
        output_line=output_line,
    ).returncode == 0
finally:
    guest_execution.run_bounded = original_bounded

assert calls[0][1]["heartbeat_seconds"] == 30, calls
assert calls[0][1]["heartbeat"] is heartbeat, calls
assert calls[0][1]["output_line"] is output_line, calls

calls = []
guest_execution.run_bounded = bounded
try:
    assert run_guest_shell_argv(
        "/launcher",
        "/prefix",
        ("/usr/bin/installer", "-pkg", "/private/var/tmp/package file.pkg", "-target", "/"),
        cwd=Path("/workspace"),
        env={"EXAMPLE": "1"},
        timeout_seconds=7,
        capture_output=True,
    ).returncode == 0
finally:
    guest_execution.run_bounded = original_bounded

assert calls[0][0][-1] == (
    "exec /usr/bin/installer -pkg '/private/var/tmp/package file.pkg' -target /"
), calls


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    invocation = {
        "name": "guest_command_contract",
        "cwd": root,
        "guest_command": "printf ignored",
        "timeout_seconds": 3,
        "expect": {"returncode": 0, "output-contains": ["COMMAND_OK"]},
    }
    calls = []
    phases = []

    def successful_shell(*args, **kwargs):
        calls.append((args, kwargs))
        kwargs["stdout"].write("COMMAND_OK\n")
        return ProcessResult(0)

    original = guest_execution.run_guest_shell
    guest_execution.run_guest_shell = successful_shell
    try:
        assert run_guest_command_fixture(
            invocation,
            env={"DPREFIX": "/prefix", "DARLING_LAUNCHER": "/launcher"},
            prefix="/prefix",
            resolve_launcher=lambda _prefix: None,
            die=fail,
            err=fail,
            record_failure_phase=lambda _invocation, phase: phases.append(phase),
        ) == 0
    finally:
        guest_execution.run_guest_shell = original

    assert calls and calls[0][0][:3] == ("/launcher", "/prefix", "set -u\n:\nprintf ignored\n")
    assert not phases

    def timed_out_shell(*_args, **kwargs):
        kwargs["stderr"].write("timed out as expected\n")
        return ProcessResult(124, timed_out=True)

    guest_execution.run_guest_shell = timed_out_shell
    try:
        timeout_invocation = {
            **invocation,
            "expect": {
                "returncode": "timeout",
                "output-contains": ["timed out as expected"],
            },
        }
        assert run_guest_command_fixture(
            timeout_invocation,
            env={"DPREFIX": "/prefix", "DARLING_LAUNCHER": "/launcher"},
            prefix="/prefix",
            resolve_launcher=lambda _prefix: None,
            die=fail,
            err=fail,
            record_failure_phase=lambda _invocation, phase: phases.append(phase),
        ) == 0
    finally:
        guest_execution.run_guest_shell = original

    assert not phases

    errors = []
    guest_execution.run_guest_shell = timed_out_shell
    try:
        assert run_guest_command_fixture(
            invocation,
            env={"DPREFIX": "/prefix", "DARLING_LAUNCHER": "/launcher"},
            prefix="/prefix",
            resolve_launcher=lambda _prefix: None,
            die=fail,
            err=errors.append,
            record_failure_phase=lambda _invocation, phase: phases.append(phase),
        ) == 124
    finally:
        guest_execution.run_guest_shell = original

    assert phases == ["run"], phases
    assert errors == ["guest_command_contract: guest command watchdog timed out after 3s"]

    mismatch_phases = []
    errors = []
    guest_execution.run_guest_shell = successful_shell
    try:
        mismatch_invocation = {
            **invocation,
            "expect": {"returncode": 134},
        }
        assert run_guest_command_fixture(
            mismatch_invocation,
            env={"DPREFIX": "/prefix", "DARLING_LAUNCHER": "/launcher"},
            prefix="/prefix",
            resolve_launcher=lambda _prefix: None,
            die=fail,
            err=errors.append,
            record_failure_phase=lambda _invocation, phase: mismatch_phases.append(phase),
        ) == 1
    finally:
        guest_execution.run_guest_shell = original

    assert mismatch_phases == ["run"], mismatch_phases
    assert errors == ["guest_command_contract: guest command rc 0, want 134"]

    argv_calls = []

    def successful_argv(*args, **kwargs):
        argv_calls.append((args, kwargs))
        kwargs["stdout"].write("ARGV_OK\n")
        return ProcessResult(0)

    original_argv = guest_execution.run_guest_argv
    guest_execution.run_guest_argv = successful_argv
    try:
        argv_invocation = {
            "name": "guest_argv_contract",
            "cwd": root,
            "guest_argv": ("/usr/bin/true",),
            "timeout_seconds": 3,
            "expect": {"returncode": 0, "output-contains": ["ARGV_OK"]},
        }
        assert run_guest_argv_fixture(
            argv_invocation,
            env={"DPREFIX": "/prefix", "DARLING_LAUNCHER": "/launcher"},
            prefix="/prefix",
            resolve_launcher=lambda _prefix: None,
            die=fail,
            err=fail,
            record_failure_phase=lambda _invocation, phase: phases.append(phase),
        ) == 0
    finally:
        guest_execution.run_guest_argv = original_argv

    assert argv_calls and argv_calls[0][0][:3] == (
        "/launcher",
        "/prefix",
        ("/usr/bin/true",),
    )


print("PASS west-test-guest-command-contract")
