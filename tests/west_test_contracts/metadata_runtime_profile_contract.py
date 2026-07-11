"""Ensure metadata guest tests can use the same typed runtime providers as CTest."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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


test = DarlingTest.__new__(DarlingTest)
test.inf = lambda _message: None
test._prune_stale_west_temp_worktrees = lambda: None
test._display_invocation = lambda _invocation: "guest fixture"
test._missing_requirements = lambda _invocation: []
test._execution_env = lambda _invocation: {"BASE": "1", "DARLING": "stale"}

invocation = {
    "key": "metadata-runtime-profile",
    "name": "runtime_profile_guest",
    "diag": "bare",
    "timeout_seconds": 30,
}
test._test_invocation = lambda _patch, _metadata: invocation


@contextmanager
def passthrough(*_args, **_kwargs):
    yield


test._required_profile_context = passthrough
test._ctest_source_override_context = lambda selected: passthrough(selected)


@contextmanager
def resource_passthrough(_selected, env):
    yield env


test._resource_context = resource_passthrough

deployments: list[tuple[list[str], str, bool, dict, bool]] = []


@contextmanager
def runtime_profile_context(
    profiles, *, label_prefix, retain_deployment, patch=None, omit_patch=False
):
    deployments.append((profiles, label_prefix, retain_deployment, patch, omit_patch))
    yield SimpleNamespace(env={"DARLING": "runtime", "DARLING_ROOTLESS": "1"})


test._runtime_profile_deployment_context = runtime_profile_context
observed: list[dict[str, str]] = []
test._run_invocation = lambda _invocation, env: observed.append(env) or 0

patch = {"path": "xnu/example.patch"}
metadata = {
    "name": "runtime_profile_guest",
    "env": "darling",
    "kind": "guest",
    "runtime-profile": "homebrew-rootless-no-mount",
}

assert test._metadata_needs_prefix([(patch, metadata)])
assert test._run_metadata_tests([(patch, metadata)], False, []) == 0
assert deployments == [(
    ["homebrew-rootless-no-mount"],
    "metadata xnu/example.patch:runtime_profile_guest",
    False,
    patch,
    False,
)], deployments
assert observed == [{"BASE": "1", "DARLING": "runtime", "DARLING_ROOTLESS": "1"}], observed

print("PASS metadata-runtime-profile-contract")
