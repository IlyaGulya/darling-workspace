"""Ensure a metadata provider can materialize the current profile minus one patch."""

from __future__ import annotations

import sys
import tempfile
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


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    launcher = prefix / "bin" / "darling"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher")

    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(prefix)
    test._runtime_evidence_root = root / "evidence"
    test._active_profile = "outer-profile"
    test._runtime_cmake_define_overrides = {"DARLING_GUEST_RECVSPIN": "0"}
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(AssertionError(message))
    test._require_runtime_scratch_space = lambda _label: None
    test._preflight_runtime_profile_stack = lambda _profile, _label: None
    test._darling_prefix_env = lambda _prefix: {"DPREFIX": str(prefix)}
    test._bootstrap_diagnostics_enabled = lambda: False
    test._ctest_runtime_profile_definitions = lambda: {
        "rootless": {
            "source-profile": "homebrew",
            "source-module": "darling",
            "source-modules": ["darling"],
            "runtime-artifacts": [],
            "launcher-env": {"DARLING_ROOTLESS": "1"},
        }
    }
    captured: dict[str, object] = {}

    @contextmanager
    def source_forest(anchor, proof, *, omit_patch, root, evidence_session):
        captured["anchor"] = anchor
        captured["proof"] = proof
        captured["omit_patch"] = omit_patch
        assert root.parent == evidence_session.directory
        yield root / "darling"

    @contextmanager
    def deployed(_proof, _build_root, _prefix, *, label, restore_deployment):
        assert label == "metadata RED rootless"
        assert restore_deployment is True
        yield

    test._guest_runtime_source_forest = source_forest
    test._runtime_red_build_artifacts = lambda *_args, **_kwargs: root / "build"
    test._runtime_red_deployed_artifacts = deployed

    patch = {
        "path": "darlingserver/example.patch",
        "module": "darling/src/external/darlingserver",
        "source-base": "deadbeef",
    }
    with test._runtime_profile_deployment_context(
        ["rootless"],
        label_prefix="metadata RED",
        retain_deployment=False,
        patch=patch,
        omit_patch=True,
    ) as deployment:
        assert deployment.env["DARLING"] == str(launcher)
        assert deployment.env["DARLING_ROOTLESS"] == "1"

    assert captured["anchor"] == patch
    assert captured["omit_patch"] is True
    assert captured["proof"] == {
        "source-modules": ["darling"],
        "runtime-artifacts": [],
        "cmake-defines": {"DARLING_GUEST_RECVSPIN": "0"},
        "bad-profile": "current-minus-patch",
    }
    assert test._active_profile == "outer-profile"

print("PASS runtime-profile-current-minus-contract")
