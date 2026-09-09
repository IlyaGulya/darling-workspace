"""Ensure metadata guest tests can use the same typed runtime providers as CTest."""

from __future__ import annotations

import sys
import types
import json
import tempfile
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

import west_commands.test as test_module
from west_commands.test import DarlingTest
from west_commands.test_runtime_deploy import RuntimeDeploymentService
from west_commands.test_runtime_identity import runtime_identity


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
    yield SimpleNamespace(
        env={"DARLING": "runtime", "DARLING_ROOTLESS": "1"},
        diagnostic_trace_paths=(),
    )


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

idle_checks: list[bool] = []
test._verify_prefix_idle = lambda: idle_checks.append(True) or True
clean_shutdown_metadata = dict(metadata, **{"verify-clean-shutdown": True})
assert test._run_metadata_tests([(patch, clean_shutdown_metadata)], False, []) == 0
assert idle_checks == [True], idle_checks

test._verify_prefix_idle = lambda: False
assert test._run_metadata_tests([(patch, clean_shutdown_metadata)], False, []) == 1


class DeploymentLifecycleHost:
    def __init__(self):
        self.shutdown_envs = []

    def _shutdown_runtime_prefix(self, _prefix, *, extra_env=None):
        self.shutdown_envs.append(dict(extra_env or {}))
        return True

    def inf(self, _message):
        pass


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "prefix"
    prefix.mkdir()
    lifecycle_host = DeploymentLifecycleHost()
    service = RuntimeDeploymentService(lifecycle_host)
    with service.deployed(
        {"launcher-env": {"DARLING_ROOTLESS": "1"}},
        Path(temp),
        prefix,
        label="contract",
        restore_deployment=True,
    ):
        pass
    assert lifecycle_host.shutdown_envs, lifecycle_host.shutdown_envs
    assert all(
        env.get("DARLING_ROOTLESS") == "1"
        for env in lifecycle_host.shutdown_envs
    ), lifecycle_host.shutdown_envs


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "prefix"
    workspace = Path(temp) / "workspace"
    manifest_repo = workspace / "darling-workspace"
    for profile, contents in {
        "homebrew": "patches: []\n",
        "selected": "base-profile: homebrew\npatches: []\n",
        "unrelated": "patches: []\n",
    }.items():
        profile_dir = manifest_repo / "patches" / profile
        profile_dir.mkdir(parents=True)
        (profile_dir / "patches.yml").write_text(contents)
    (manifest_repo / "west.yml").write_text("manifest: {}\n")
    (manifest_repo / "testkit").mkdir()
    (manifest_repo / "testkit/runtime-profiles.yml").write_text("profiles: {}\n")
    (prefix / "bin").mkdir(parents=True)
    launcher = prefix / "bin" / "darling"
    launcher.write_text("launcher\n")
    definition = {
        "source-profile": "selected",
        "launcher-env": {
            "DARLING_ROOTLESS": "1",
            "DARLING_NOOVERLAYFS": "1",
        },
    }
    (prefix / test_module.RETAINED_RUNTIME_PROFILE_MARKER).write_text(
        json.dumps({
            "schema": 2,
            "profile": "homebrew-rootless-no-mount",
            "source-profile": "selected",
            "guest-toolchain": None,
            "fingerprint": runtime_identity(
                topdir=workspace,
                manifest_repo=manifest_repo,
                profile_name="homebrew-rootless-no-mount",
                definition=definition,
                launcher=launcher,
            ),
        })
    )

    reused = DarlingTest.__new__(DarlingTest)
    reused.topdir = str(workspace)
    reused.manifest = SimpleNamespace(repo_abspath=str(manifest_repo))
    reused._prefix = str(prefix)
    reused._reuse_prefix_runtime = True
    reused._ctest_runtime_profile_definitions = lambda: {
        "homebrew-rootless-no-mount": definition,
        "homebrew-guest-toolchain-provisioning": {
            **definition,
            "purpose": "guest-toolchain-provisioning",
        },
    }
    reused._darling_prefix_env = lambda path: {
        "DPREFIX": str(path),
        "DARLING_PREFIX": str(path),
    }
    reused.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    reused._runtime_profile_deployment_context = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("reused runtime unexpectedly rebuilt")
    )

    with reused._metadata_runtime_profile_context(
        patch, metadata
    ) as deployment:
        assert deployment.name == "homebrew-rootless-no-mount"
        assert deployment.env["DARLING_ROOTLESS"] == "1"
        assert deployment.env["DARLING"] == str(prefix / "bin" / "darling")

    mismatched = dict(metadata, **{"runtime-profile": "homebrew-guest-toolchain-provisioning"})
    try:
        with reused._metadata_runtime_profile_context(patch, mismatched):
            raise AssertionError("provider mismatch unexpectedly passed")
    except SystemExit as error:
        assert "fingerprint mismatch" in str(error), error

    def require_reuse_rejected():
        try:
            reused._retained_runtime_profile("homebrew-rootless-no-mount")
        except SystemExit as error:
            assert "fingerprint mismatch" in str(error), error
        else:
            raise AssertionError("changed runtime inputs unexpectedly reused")

    # Changes to the selected profile and its base both invalidate the marker.
    # The manifest lives below the West root, as in a real workspace.
    for profile in ("selected", "homebrew"):
        profile_path = manifest_repo / "patches" / profile / "patches.yml"
        original = profile_path.read_text()
        profile_path.write_text(original.replace("patches: []", """patches:
  - module: darling
    path: changed.patch
    source-commit: changed"""))
        require_reuse_rejected()
        profile_path.write_text(original)

    unrelated = manifest_repo / "patches/unrelated/patches.yml"
    unrelated.write_text("patches: [{path: unrelated.patch}]\n")
    reused._retained_runtime_profile("homebrew-rootless-no-mount")

    lock = manifest_repo / "west.yml"
    lock.write_text("manifest: {revision: changed}\n")
    require_reuse_rejected()
    lock.write_text("manifest: {}\n")

    marker_path = prefix / test_module.RETAINED_RUNTIME_PROFILE_MARKER
    retained_marker = json.loads(marker_path.read_text())
    old_fingerprint = dict(retained_marker["fingerprint"], schema=1)
    marker_path.write_text(json.dumps(dict(retained_marker, fingerprint=old_fingerprint)))
    require_reuse_rejected()
    marker_path.write_text(json.dumps(retained_marker))

    lifecycle = DarlingTest.__new__(DarlingTest)
    lifecycle._prefix_env = {}
    lifecycle._ctest_runtime_profile_definitions = lambda: {
        "homebrew-rootless-no-mount": definition
    }
    resolved = lifecycle._resolve_prefix(SimpleNamespace(
        prefix=str(prefix),
        prefix_profile=None,
        no_overlayfs=False,
    ))
    assert resolved == str(prefix)
    assert lifecycle._prefix_env["DARLING_ROOTLESS"] == "1"
    assert lifecycle._prefix_env["DARLING_NOOVERLAYFS"] == "1"

print("PASS metadata-runtime-profile-contract")
