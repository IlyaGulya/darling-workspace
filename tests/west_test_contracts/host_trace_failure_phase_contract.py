"""A failed host trace oracle is a post-command run failure, not an unknown failure."""

from __future__ import annotations

import sys
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


test = DarlingTest.__new__(DarlingTest)
test._debug_runner_args = lambda _invocation: ["fixture"]
test._check_host_traces = lambda _invocation, _env: 1
test.err = lambda _message: None
phases: list[str] = []
test._record_failure_phase = lambda _invocation, phase: phases.append(phase)

original = test_module.run_bounded
test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(0, "", "", False)
try:
    invocation = {"name": "trace_oracle", "timeout_seconds": 1, "cwd": ROOT}
    assert test._run_command_invocation(invocation, {"DPREFIX": "/tmp/prefix"}) == 1
finally:
    test_module.run_bounded = original

assert phases == ["run"], phases
print("PASS host-trace-failure-phase-contract")
