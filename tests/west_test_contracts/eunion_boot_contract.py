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

    original = test_module.run_guest_shell

    def failed_boot(*_args, **kwargs):
        assert kwargs["capture_output"] is True, kwargs
        assert kwargs["timeout_seconds"] == 15, kwargs
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

print("PASS eunion-boot-contract")
