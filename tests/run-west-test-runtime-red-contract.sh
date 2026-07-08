#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import sys
import subprocess
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(Path.cwd())
    test.inf_messages = []
    test.err_messages = []
    test.inf = lambda message: test.inf_messages.append(message)
    test.wrn = lambda message: None
    test.err = lambda message: test.err_messages.append(message)
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._prefix_process_snapshot = lambda _prefix: []
    test._missing_requirements = lambda _invocation: []
    test._execution_env = lambda _invocation: {"DPREFIX": test._prefix}
    return test


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    root_copy = prefix / "usr/lib/system/libsystem_kernel.dylib"
    base_copy = prefix / "libexec/darling/usr/lib/system/libsystem_kernel.dylib"
    root_copy.parent.mkdir(parents=True)
    base_copy.parent.mkdir(parents=True)
    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")

    test = make_test()
    test._prefix = str(prefix)
    calls = []

    @contextmanager
    def fake_source_forest(patch, proof):
        calls.append(("source", patch["module"], proof["mode"]))
        yield tempdir / "source/darling"

    def fake_build(source_root, proof, build_prefix, scratch_root):
        calls.append(("build", source_root, build_prefix, scratch_root.exists()))
        output = scratch_root / "build/xnu/libsystem_kernel.dylib"
        output.parent.mkdir(parents=True)
        output.write_text("BAD\n")
        return scratch_root / "build"

    def fake_run(invocation, env=None):
        del env
        calls.append(("run", invocation["name"], root_copy.read_text(), base_copy.read_text()))
        return 77 if len([call for call in calls if call[0] == "run"]) == 1 else 0

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = fake_build
    test._run_invocation = fake_run

    patch = {"path": "xnu/example.patch", "module": "darling/src/external/xnu"}
    proof = {
        "mode": "guest-runtime-deploy",
        "runtime-artifacts": [
            {
                "module": "darling/src/external/xnu",
                "build-targets": ["libsystem_kernel"],
                "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
            }
        ],
    }
    invocation = {"guest_c_fixture": True, "name": "runtime_red_contract"}

    rc = test._run_guest_runtime_deploy_proof(patch, proof, invocation)
    assert rc == 0, rc
    assert root_copy.read_text() == "ORIGINAL\n"
    assert base_copy.read_text() == "ORIGINAL\n"
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy"), calls
    assert calls[1][0] == "build" and calls[1][3] is True, calls
    assert calls[2] == ("run", "runtime_red_contract", "BAD\n", "BAD\n"), calls
    assert calls[3] == ("run", "runtime_red_contract", "ORIGINAL\n", "ORIGINAL\n"), calls

    symlink_parent = tempdir / "forest/darling/src/external/parent"
    symlink_parent.parent.mkdir(parents=True)
    symlink_parent.symlink_to(prefix, target_is_directory=True)
    nested_target = symlink_parent / "nested/project"
    assert test._has_symlink_parent(nested_target, tempdir / "forest/darling")

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    target = tempdir / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=target, check=True)
    (target / "file.txt").write_text("base\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=target, check=True)
    (target / "file.txt").write_text("base\nadded\n")
    patch_text = subprocess.run(
        ["git", "diff", "--", "file.txt"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add line"], cwd=target, check=True)

    profile_dir = tempdir / "patches/runtime"
    patch_file = profile_dir / "x/example.patch"
    patch_file.parent.mkdir(parents=True)
    patch_file.write_text(patch_text)

    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._active_profile = "runtime"
    test._reverse_apply_patch_file({"path": "x/example.patch"}, target)
    assert (target / "file.txt").read_text() == "base\n"

print("PASS west-test-runtime-red-contract")
PY
