"""Keep E-UNION prefix bootstrap failures observable and classified."""

from __future__ import annotations

import sys
import tempfile
import types
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

import west_commands.test as test_module
from west_commands.test import DarlingTest
from west_commands.test_execution import ProcessResult


with tempfile.TemporaryDirectory() as temp:
    trace_dir = Path(temp) / "trace"
    test = DarlingTest.__new__(DarlingTest)
    messages: list[str] = []
    phases: list[str] = []
    infos: list[str] = []
    test.err = messages.append
    test.inf = infos.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._darling_prefix_env = lambda prefix: {"DPREFIX": str(prefix)}
    test._resolve_darling_launcher = lambda _prefix: "/fake/darling"
    test._record_failure_phase = lambda _invocation, phase: phases.append(phase)
    test._bootstrap_syscall_trace = trace_dir
    test._bootstrap_timeout_seconds = 45

    original = test_module.run_guest_shell

    def failed_boot(*_args, **kwargs):
        assert kwargs["capture_output"] is True, kwargs
        assert kwargs["timeout_seconds"] == 45, kwargs
        assert kwargs["command_prefix"] == (
            "strace", "-ff", "-o", str(trace_dir / "eunion-bootstrap"),
        ), kwargs
        return ProcessResult(1, stdout=b"boot stdout\n", stderr=b"boot stderr\n")

    test_module.run_guest_shell = failed_boot
    try:
        try:
            test._boot_eunion_runtime_prefix(
                {"name": "eunion_boot_contract"},
                {"DARLING": "/fake/darling"},
                Path("/tmp/eunion-prefix"),
            )
            raise AssertionError("failed E-UNION bootstrap unexpectedly passed")
        except SystemExit as exc:
            assert str(exc) == (
                "eunion_boot_contract: failed to boot Darling E-UNION prefix "
                "before fixture setup (rc=1)"
            ), exc
    finally:
        test_module.run_guest_shell = original

    assert phases == ["bootstrap"], phases
    assert infos == [
        f"eunion_boot_contract: E-UNION bootstrap syscall trace: {trace_dir}"
    ], infos
    assert messages == [
        "eunion_boot_contract: E-UNION prefix bootstrap output:\nboot stdout\nboot stderr"
    ], messages

with tempfile.TemporaryDirectory() as temp:
    trace_dir = Path(temp) / "trace"
    prefix = Path(temp) / "prefix"
    test = DarlingTest.__new__(DarlingTest)
    messages: list[str] = []
    test.err = messages.append
    test.inf = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._darling_prefix_env = lambda value: {"DPREFIX": str(value)}
    test._resolve_darling_launcher = lambda _prefix: "/fake/darling"
    test._record_failure_phase = lambda _invocation, _phase: None
    test._bootstrap_syscall_trace = trace_dir
    test._bootstrap_timeout_seconds = 45

    original = test_module.run_guest_shell

    def timed_out_boot(*_args, **kwargs):
        assert kwargs["timeout_seconds"] == 45, kwargs
        return ProcessResult(124, timed_out=True, stdout="", stderr="")

    test_module.run_guest_shell = timed_out_boot
    try:
        try:
            test._boot_eunion_runtime_prefix(
                {"name": "eunion_timeout_contract"},
                {"DARLING": "/fake/darling"},
                prefix,
            )
            raise AssertionError("timed out E-UNION bootstrap unexpectedly passed")
        except SystemExit as exc:
            assert str(exc) == (
                "eunion_timeout_contract: failed to boot Darling E-UNION prefix "
                "before fixture setup (rc=124)"
            ), exc
    finally:
        test_module.run_guest_shell = original

    assert messages == [
        "eunion_timeout_contract: E-UNION prefix bootstrap timed out after 45s "
        "without output; syscall trace: " + str(trace_dir)
    ], messages

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    sample_dir = root / "stack-sample"
    server_trace = prefix / "private/var/log/dserver-rpc-trace.log"
    server_trace.parent.mkdir(parents=True)
    server_trace.write_text("rpc.recv number=38 name=mach_msg_overwrite\n")
    test = DarlingTest.__new__(DarlingTest)
    messages: list[str] = []
    phases: list[str] = []
    infos: list[str] = []
    test.topdir = str(root)
    test.err = messages.append
    test.inf = infos.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._darling_prefix_env = lambda value: {"DPREFIX": str(value)}
    test._resolve_darling_launcher = lambda _prefix: "/fake/darling"
    test._record_failure_phase = lambda _invocation, phase: phases.append(phase)
    test._bootstrap_syscall_trace = None
    test._bootstrap_stack_sample = sample_dir

    original_guest_shell = test_module.run_guest_shell
    original_bounded = test_module.run_bounded
    original_which = test_module.shutil.which

    def timed_out_boot(*_args, **kwargs):
        assert kwargs["timeout_seconds"] == 15, kwargs
        assert kwargs["command_prefix"] == (
            "perf",
            "record",
            "--all-user",
            "--call-graph",
            "fp",
            "--output",
            str(sample_dir / "eunion-bootstrap.perf.data"),
            "--",
        ), kwargs
        (sample_dir / "eunion-bootstrap.perf.data").write_text("perf data\n")
        return ProcessResult(124, timed_out=True, stdout="", stderr="")

    def render_stack(command, **_kwargs):
        assert command == [
            "perf",
            "script",
            "--input",
            str(sample_dir / "eunion-bootstrap.perf.data"),
        ], command
        return ProcessResult(0, stdout="stack frame\n", stderr="")

    test_module.run_guest_shell = timed_out_boot
    test_module.run_bounded = render_stack
    test_module.shutil.which = lambda name: "/usr/bin/perf" if name == "perf" else None
    try:
        try:
            test._boot_eunion_runtime_prefix(
                {"name": "eunion_stack_contract"},
                {"DARLING": "/fake/darling"},
                prefix,
            )
            raise AssertionError("timed out E-UNION bootstrap unexpectedly passed")
        except SystemExit as exc:
            assert str(exc) == (
                "eunion_stack_contract: failed to boot Darling E-UNION prefix "
                "before fixture setup (rc=124)"
            ), exc
    finally:
        test_module.run_guest_shell = original_guest_shell
        test_module.run_bounded = original_bounded
        test_module.shutil.which = original_which

    assert phases == ["bootstrap"], phases
    assert (sample_dir / "eunion-bootstrap.perf.txt").read_text() == "stack frame\n"
    assert (sample_dir / "darlingserver-rpc.log").read_text() == server_trace.read_text()
    assert any("E-UNION bootstrap stack sample" in message for message in infos), infos
    assert any("stack sample" in message for message in messages), messages

print("PASS eunion-boot-contract")
