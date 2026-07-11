"""Keep profile-selected source contracts bound to the materialized source tree."""

from __future__ import annotations

import sys
import types
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
test.manifest = SimpleNamespace(repo_abspath="/tmp/workspace")
test._active_profile = "homebrew"
test._profile_is_applied = lambda profile: profile == "arch"
test._profile_stack_modules = lambda profile: {
    "darling/src/external/xnu", "darling-workspace"
} if profile == "homebrew" else set()
test._test_invocation = lambda _patch, spec: spec
test._project_path = lambda module: Path("/tmp/workspace") if module == "darling-workspace" else Path("/tmp/profile-source/xnu")

source_bound = {"source_env": "XNU_SRC_ROOT", "source_module": "darling/src/external/xnu"}
assert test._metadata_needs_profile_worktree([({}, source_bound)])
assert not test._metadata_needs_profile_worktree([({}, {"source_env": ""})])
assert not test._metadata_needs_profile_worktree(
    [({}, {"source_env": "DARLING_SRC_ROOT", "source_module": "darling-workspace"})]
)

test._profile_is_applied = lambda profile: profile == "homebrew"
assert not test._metadata_needs_profile_worktree([({}, source_bound)])

materialized_source = Path("/tmp/profile-source/xnu")
environment = test._execution_env(source_bound)
assert environment is not None
assert environment["XNU_SRC_ROOT"] == str(materialized_source), environment

print("PASS metadata-source-profile-contract")
