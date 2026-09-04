"""Ensure runtime diagnostic builds honor an explicit per-phase deadline."""

from __future__ import annotations

import os
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
        configure_args=lambda _proof, _prefix, _scratch: [],
        dump_command_tail=lambda *_args: None,
        runner=runner,
        timeout_seconds=7,
    )


    cached_calls = []

    def cached_runner(command, **kwargs):
        cached_calls.append((command, kwargs))
        if command[0] == "cmake":
            build = Path(command[command.index("-B") + 1])
            build.mkdir(parents=True)
        else:
            build = Path(command[2])
            artifact = build / "bin/darling"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"cached runtime\n")
            artifact.chmod(0o755)
        return ProcessResult(0)

    cache_root = root / "cache"
    os.environ["WEST_RUNTIME_BUILD_CACHE_DIR"] = str(cache_root)
    os.environ["WEST_RUNTIME_BUILD_CACHE_KEY"] = "a" * 64
    cached_proof = {
        "runtime-artifacts": [
            {
                "build-targets": ["darling"],
                "deploy": ["bin/darling"],
            }
        ]
    }
    try:
        first = service.build_artifacts(
            root / "source-a",
            cached_proof,
            root / "prefix",
            root / "scratch-a",
            label="CACHED",
            allow_failure=False,
            configure_args=lambda _proof, _prefix, _scratch: [],
            dump_command_tail=lambda *_args: None,
            runner=cached_runner,
            timeout_seconds=7,
        )
        second = service.build_artifacts(
            root / "source-b",
            cached_proof,
            root / "prefix",
            root / "scratch-b",
            label="CACHED",
            allow_failure=False,
            configure_args=lambda _proof, _prefix, _scratch: [],
            dump_command_tail=lambda *_args: None,
            runner=cached_runner,
            timeout_seconds=7,
        )
        assert first == second
        assert len(cached_calls) == 2, cached_calls
        (second / "bin/darling").write_bytes(b"mutated\n")
        try:
            service.build_artifacts(
                root / "source-c",
                cached_proof,
                root / "prefix",
                root / "scratch-c",
                label="CACHED",
                allow_failure=False,
                configure_args=lambda _proof, _prefix, _scratch: [],
                dump_command_tail=lambda *_args: None,
                runner=cached_runner,
                timeout_seconds=7,
            )
        except ValueError as error:
            assert "artifacts differ" in str(error)
        else:
            raise AssertionError("runtime build cache accepted a mutated artifact")
    finally:
        os.environ.pop("WEST_RUNTIME_BUILD_CACHE_DIR", None)
        os.environ.pop("WEST_RUNTIME_BUILD_CACHE_KEY", None)
assert [kwargs["timeout_seconds"] for _command, kwargs in calls] == [7, 7], calls
print("PASS runtime-build-contract")
