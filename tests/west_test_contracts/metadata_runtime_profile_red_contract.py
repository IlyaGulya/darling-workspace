"""Ensure metadata runtime RED/GREEN uses the declared provider in both arms."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
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
from west_commands.test_results import InvocationResult


test = DarlingTest.__new__(DarlingTest)
test.inf = lambda _message: None
test.err = lambda _message: None
test._execution_env = lambda _invocation: {"BASE": "1", "DARLING": "stale"}


@contextmanager
def passthrough(selected):
    yield selected


test._ctest_source_override_context = passthrough


@contextmanager
def resource_passthrough(_invocation, env):
    yield env


test._resource_context = resource_passthrough
deployments: list[bool] = []


@contextmanager
def runtime_context(_patch, _metadata, *, omit_patch=False):
    deployments.append(omit_patch)
    yield {
        "DARLING": "rootless-launcher",
        "DARLING_ROOTLESS": "1",
        "DARLING_NOOVERLAYFS": "1",
    }


test._metadata_runtime_profile_context = runtime_context
test._guest_runtime_red_invocation = lambda _patch, _proof, invocation: {
    **invocation,
    "name": "provider_red",
}
seen: list[tuple[str, dict[str, str]]] = []


def run_captured(invocation, env):
    seen.append((invocation["name"], env))
    return InvocationResult(
        1,
        "missing host trace content in provider trace: rpc.semaphore.begin",
        "run",
    )


test._run_invocation_captured = run_captured
test._check_red_failure_phase = lambda _proof, _invocation, phase: phase == "run"
test._check_guest_runtime_red_failure = lambda _proof, _invocation, **_kwargs: True
test._run_invocation = lambda invocation, env: seen.append((invocation["name"], env)) or 0

patch = {"path": "darlingserver/provider.patch"}
metadata = {"name": "provider_guest", "runtime-profile": "homebrew-rootless-no-mount"}
proof = {
    "mode": "guest-runtime-deploy",
    "expect-failure-phase": "run",
    "expect-output-contains": ["missing host trace content"],
}
invocation = {"name": "provider_green", "diag": "guarded"}

assert test._run_metadata_runtime_profile_proof(patch, metadata, proof, invocation) == 0
assert deployments == [True, False], deployments
assert seen == [
    (
        "provider_red",
        {
            "BASE": "1",
            "DARLING": "rootless-launcher",
            "DARLING_ROOTLESS": "1",
            "DARLING_NOOVERLAYFS": "1",
        },
    ),
    (
        "provider_green",
        {
            "BASE": "1",
            "DARLING": "rootless-launcher",
            "DARLING_ROOTLESS": "1",
            "DARLING_NOOVERLAYFS": "1",
        },
    ),
], seen

print("PASS metadata-runtime-profile-red-contract")
