#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import sys
import os
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
    build_dir = tempdir / "build"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text(
        "\n".join(
            [
                "CMAKE_GENERATOR:INTERNAL=Ninja",
                "CMAKE_BUILD_TYPE:STRING=Debug",
                "CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang",
                "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++",
                "DARLING_EUNION:BOOL=ON",
                "DARLING_RPC_SLEEP_ACCOUNT:BOOL=OFF",
                "DARLING_GUEST_RECVSPIN:STRING=512",
            ]
        )
        + "\n"
    )
    old_build_dir = os.environ.get("DARLING_BUILD_DIR")
    os.environ["DARLING_BUILD_DIR"] = str(build_dir)
    try:
        args = make_test()._runtime_red_configure_args(tempdir / "prefix")
    finally:
        if old_build_dir is None:
            os.environ.pop("DARLING_BUILD_DIR", None)
        else:
            os.environ["DARLING_BUILD_DIR"] = old_build_dir
    assert "-DDARLING_EUNION=ON" in args, args
    assert "-DDARLING_RPC_SLEEP_ACCOUNT=OFF" in args, args
    assert "-DDARLING_GUEST_RECVSPIN=512" in args, args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'prefix'}" in args, args

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
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (target / "file.txt").write_text("base\nskipped\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "skipped patch"], cwd=target, check=True)
    skipped_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "dependent.txt").write_text("dependent\n")
    subprocess.run(["git", "add", "dependent.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "dependent patch"], cwd=target, check=True)
    dependent_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "other.txt").write_text("kept\n")
    subprocess.run(["git", "add", "other.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "kept patch"], cwd=target, check=True)
    kept_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    profile_dir = tempdir / "patches/runtime"
    skipped_patch_file = profile_dir / "x/skipped.patch"
    dependent_patch_file = profile_dir / "x/dependent.patch"
    kept_patch_file = profile_dir / "x/kept.patch"
    skipped_patch_file.parent.mkdir(parents=True)
    skipped_patch_file.write_text(skipped_patch)
    dependent_patch_file.write_text(dependent_patch)
    kept_patch_file.write_text(kept_patch)
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)

    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "x/skipped.patch", "module": "module"},
            {"path": "x/dependent.patch", "module": "module"},
            {"path": "x/kept.patch", "module": "module"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    test._apply_profile_module_patches(
        "runtime",
        "module",
        target,
        skip_patch_paths={"x/skipped.patch", "x/dependent.patch"},
    )
    assert (target / "file.txt").read_text() == "base\n"
    assert not (target / "dependent.txt").exists()
    assert (target / "other.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def independent_patch(path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        return subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    skipped_patch = independent_patch("skipped.txt", "skipped\n", "skipped patch")
    kept_patch = independent_patch("kept.txt", "kept\n", "kept patch")
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)

    profile_dir = tempdir / "patches/runtime/darling"
    profile_dir.mkdir(parents=True)
    (profile_dir / "skipped.patch").write_text(skipped_patch)
    (profile_dir / "kept.patch").write_text(kept_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/skipped.patch", "module": "darling"},
            {"path": "darling/kept.patch", "module": "darling"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    patch = {"path": "darling/skipped.patch", "module": "darling"}
    proof = {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"}
    with test._guest_runtime_source_forest(patch, proof) as source_root:
        assert (source_root / "base.txt").read_text() == "base\n"
        assert not (source_root / "skipped.txt").exists()
        assert (source_root / "kept.txt").read_text() == "kept\n"

print("PASS west-test-runtime-red-contract")
PY
