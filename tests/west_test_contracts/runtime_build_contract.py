"""Ensure runtime diagnostic builds honor an explicit per-phase deadline."""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_execution import ProcessResult
from test_runtime_build import RuntimeBuildService


west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")
west_commands_module.WestCommand = object
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)


class Host:
    topdir = "/tmp"

    def inf(self, _message):
        pass

    def err(self, _message):
        pass


calls = []


def runner(command, **kwargs):
    calls.append((command, kwargs))
    return ProcessResult(0)


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    service = RuntimeBuildService(Host())
    service.build_artifacts(
        root / "source",
        {"runtime-artifacts": [{"build-targets": ["darlingserver"]}]},
        root / "prefix",
        root / "scratch",
        label="DIAGNOSTIC",
        allow_failure=False,
        configure_args=lambda _proof, _prefix: [],
        dump_command_tail=lambda *_args: None,
        runner=runner,
        timeout_seconds=7,
    )

assert [kwargs["timeout_seconds"] for _command, kwargs in calls] == [7, 7], calls
print("PASS runtime-build-contract")
