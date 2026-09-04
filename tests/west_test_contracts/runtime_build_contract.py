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

    def __init__(self):
        self.messages = []

    def inf(self, message):
        self.messages.append(message)

    def err(self, _message):
        pass


calls = []


def runner(command, **kwargs):
    calls.append((command, kwargs))
    return ProcessResult(0)


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    host = Host()
    service = RuntimeBuildService(host)
    discover_artifacts = service._cache_artifacts

    def counted_artifact_discovery(proof, build_root):
        nonlocal_artifact_discovery[0] += 1
        return discover_artifacts(proof, build_root)

    nonlocal_artifact_discovery = [0]
    service._cache_artifacts = counted_artifact_discovery
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
            build = Path(command[command.index("-C") + 1])
            artifact = build / "bin/darling"
            artifact.parent.mkdir(parents=True, exist_ok=True)
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
        assert len(cached_calls) == 3, cached_calls
        assert [call[0][0] for call in cached_calls] == ["cmake", "ninja", "ninja"]
        assert "-d" in cached_calls[-1][0] and "stats" in cached_calls[-1][0]
        assert [call[0][-1] for call in cached_calls[1:]] == ["darling", "darling"]
        assert any(
            "ninja_edges=" in message
            and "ccache_hits=" in message
            and "ccache_hit_rate=" in message
            for message in host.messages
        )
        assert nonlocal_artifact_discovery == [1], (
            "a warm no-op build must validate indexed artifacts without "
            f"rediscovering the build tree: {nonlocal_artifact_discovery}"
        )
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
assert calls[1][0][-1] == "darlingserver", calls
print("PASS runtime-build-contract")
