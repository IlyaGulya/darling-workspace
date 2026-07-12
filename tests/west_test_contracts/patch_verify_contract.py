"""Behavioral contracts for disposable Git worktrees used by ``west patch``."""

from __future__ import annotations

import os
import subprocess
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

from west_commands.patch import (
    DarlingPatch,
    PATCH_APPLICATION_GIT_OPTIONS,
    TEMPORARY_PATCH_GIT_OPTIONS,
    git_for_temporary_patch_application,
)


assert PATCH_APPLICATION_GIT_OPTIONS == (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
assert TEMPORARY_PATCH_GIT_OPTIONS == PATCH_APPLICATION_GIT_OPTIONS


def runtime_patch(artifact):
    return {
        "module": "darling",
        "tests": [
            {
                "name": "rootless_bootstrap_resource_contract",
                "runs": "guest",
                "runner": "guest-runtime-script",
                "script": "tests/west_test_contracts/patch_verify_contract.py",
                "red-proof": {
                    "mode": "guest-runtime-deploy",
                    "runtime-artifacts": [artifact],
                }
            }
        ]
    }


metadata_command = DarlingPatch.__new__(DarlingPatch)
metadata_command._project_path = lambda _repo: ROOT
rootless_artifact = {
    "module": "darling",
    "build-targets": ["rootless_bootstrap"],
    "resource": "rootless-bootstrap",
}
rootless_errors = metadata_command._validate_test_metadata(runtime_patch(rootless_artifact))
assert rootless_errors == [], rootless_errors
for invalid_artifact, expected in (
    ({**rootless_artifact, "deploy": ["bin/darling"]}, "must not declare deploy paths"),
    ({**rootless_artifact, "build-targets": ["darling"]}, "must build only"),
    ({**rootless_artifact, "resource": "unknown"}, "has unknown resource"),
):
    errors = metadata_command._validate_test_metadata(runtime_patch(invalid_artifact))
    assert any(expected in error for error in errors), errors

source_revision_patch = {
    "module": "darling/src/external/xnu",
    "tests": [
        {
            "name": "source_revision_contract",
            "runs": "host",
            "runner": "source-contract-script",
            "script": "tests/source_revision_contract.sh",
            "red": True,
            "red-proof": {
                "mode": "source-base",
                "source-env": "XNU_SRC_ROOT",
                "source-revision": "deadbeef",
            },
        }
    ],
}
source_revision_errors = metadata_command._validate_test_metadata(source_revision_patch)
assert source_revision_errors == [], source_revision_errors
guest_source_revision_patch = {
    "module": "darling/src/external/xnu",
    "tests": [
        {
            "name": "guest_source_revision_contract",
            "runs": "guest",
            "runner": "guest-c-fixture",
            "script": "tests/eunion_mkdir_opaque_guest.c",
            "ok-marker": "WEST_EUNION_MKDIR_OPAQUE_OK",
            "red": True,
            "requires": ["darling-prefix"],
            "red-proof": {
                "mode": "guest-runtime-deploy",
                "bad-profile": "current-minus-patch",
                "source-revision": "deadbeef",
                "runtime-artifacts": [
                    {
                        "module": "darling/src/external/xnu",
                        "build-targets": ["system_kernel"],
                        "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
                    }
                ],
            },
        }
    ],
}
guest_source_revision_errors = metadata_command._validate_test_metadata(guest_source_revision_patch)
assert guest_source_revision_errors == [], guest_source_revision_errors
for invalid_proof, expected in (
    (
        {"mode": "self", "source-revision": "deadbeef"},
        "requires mode: source-base or guest-runtime-deploy",
    ),
    ({"mode": "source-base", "source-revision": ""}, "non-empty revision"),
):
    invalid_patch = {
        **source_revision_patch,
        "tests": [{**source_revision_patch["tests"][0], "red-proof": invalid_proof}],
    }
    errors = metadata_command._validate_test_metadata(invalid_patch)
    assert any(expected in error for error in errors), errors


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL)


with tempfile.TemporaryDirectory(prefix="west-patch-verify-contract-") as temp:
    repo = Path(temp)
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "west-test@example.invalid")
    git(repo, "config", "user.name", "West patch test")
    # Force a repository state where a normal ``git am`` would consider auto
    # maintenance. The temporary verifier must leave such housekeeping alone.
    git(repo, "config", "gc.auto", "1")
    (repo / "fixture.txt").write_text("base\n")
    git(repo, "add", "fixture.txt")
    git(repo, "commit", "--quiet", "-m", "base")
    for index in range(32):
        blob = repo / f"blob-{index}"
        blob.write_text(f"{index}\n")
        git(repo, "hash-object", "-w", blob.name)

    (repo / "fixture.txt").write_text("patched\n")
    git(repo, "commit", "--quiet", "-am", "patched")
    patch = repo / "fixture.patch"
    with patch.open("wb") as output:
        subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            stdout=output,
        )
    git(repo, "reset", "--hard", "--quiet", "HEAD~")

    trace = repo / "trace.json"
    previous_trace = os.environ.get("GIT_TRACE2_EVENT")
    os.environ["GIT_TRACE2_EVENT"] = str(trace)
    try:
        git_for_temporary_patch_application(repo, "am", "--3way", str(patch))
    finally:
        if previous_trace is None:
            del os.environ["GIT_TRACE2_EVENT"]
        else:
            os.environ["GIT_TRACE2_EVENT"] = previous_trace

    assert (repo / "fixture.txt").read_text() == "patched\n"
    assert not (repo / ".git" / "gc.log").exists()
    assert "maintenance run --auto" not in trace.read_text()

    git(repo, "reset", "--hard", "--quiet", "HEAD~")
    trace.unlink()
    os.environ["GIT_TRACE2_EVENT"] = str(trace)
    command = DarlingPatch.__new__(DarlingPatch)
    command._base_profile = None
    (repo / "west.lock.yml").write_text("manifest: contract\n")
    command.manifest = types.SimpleNamespace(repo_abspath=repo)
    command._group = lambda _patches: {"module": [{"path": "fixture.patch"}]}
    command._require_base_applied = lambda _modules: None
    command._ensure_generated_context = lambda *_args, **_kwargs: None
    command._repo = lambda _module: repo
    command._prepare = lambda *_args, **_kwargs: None
    command._verify_patch = lambda *_args, **_kwargs: patch
    command._record_integration = lambda *_args, **_kwargs: None
    command._abort_am = lambda *_args, **_kwargs: None
    command._reset = lambda *_args, **_kwargs: None
    command.inf = lambda _message: None
    command.die = lambda message: (_ for _ in ()).throw(AssertionError(message))
    try:
        command._apply("contract", repo, [{"path": "fixture.patch"}], "0", False)
    finally:
        if previous_trace is None:
            del os.environ["GIT_TRACE2_EVENT"]
        else:
            os.environ["GIT_TRACE2_EVENT"] = previous_trace

    assert (repo / "fixture.txt").read_text() == "patched\n"
    assert not (repo / ".git" / "gc.log").exists()
    assert "maintenance run --auto" not in trace.read_text()


print("PASS west-patch-verify-contract")
