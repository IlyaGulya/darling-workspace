#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import sys
import os
import signal
import shutil
import subprocess
import tempfile
import time
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

import west_commands.test as west_test_module
from west_commands.test import DarlingTest


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(Path.cwd())
    test.inf_messages = []
    test.err_messages = []
    test.inf = lambda message: test.inf_messages.append(message)
    test.wrn = lambda message: None
    test.err = lambda message: test.err_messages.append(message)
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
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
                "DARLING_RING_TRANSPORT:BOOL=ON",
                "DARLING_RPC_SLEEP_ACCOUNT:BOOL=OFF",
                "DARLING_GUEST_RECVSPIN:STRING=512",
                "DSERVER_RING_TRANSPORT:BOOL=ON",
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
    assert "-DDARLING_RING_TRANSPORT=ON" in args, args
    assert "-DDARLING_RPC_SLEEP_ACCOUNT=OFF" in args, args
    assert "-DDARLING_GUEST_RECVSPIN=512" in args, args
    assert "-DDSERVER_RING_TRANSPORT=ON" in args, args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'prefix'}" in args, args

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    init_pid = tempdir / ".init.pid"
    init_pid.write_text("12345\n")
    old_checker = west_test_module.darling_init_pid_is_usable
    west_test_module.darling_init_pid_is_usable = lambda pid: pid != 12345
    try:
        make_test()._remove_stale_init_pid(tempdir)
    finally:
        west_test_module.darling_init_pid_is_usable = old_checker
    assert not init_pid.exists()

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
    def fake_source_forest(patch, proof, *, omit_patch):
        calls.append(("source", patch["module"], proof["mode"], omit_patch))
        yield tempdir / "source/darling"

    def fake_build(source_root, proof, build_prefix, scratch_root, *, label="RED"):
        calls.append(("build", source_root, build_prefix, scratch_root.exists(), label))
        output = scratch_root / "build/xnu/libsystem_kernel.dylib"
        output.parent.mkdir(parents=True)
        output.write_text(f"{label}\n")
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
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][3] is True and calls[1][4] == "RED", calls
    assert calls[2] == ("run", "runtime_red_contract", "RED\n", "RED\n"), calls
    assert calls[3] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[4][0] == "build" and calls[4][3] is True and calls[4][4] == "GREEN", calls
    assert calls[5] == ("run", "runtime_red_contract", "GREEN\n", "GREEN\n"), calls

    symlink_parent = tempdir / "forest/darling/src/external/parent"
    symlink_parent.parent.mkdir(parents=True)
    symlink_parent.symlink_to(prefix, target_is_directory=True)
    nested_target = symlink_parent / "nested/project"
    assert test._has_symlink_parent(nested_target, tempdir / "forest/darling")

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling"
    hold_pid = tempdir / "hold.pid"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
if [ "${1:-}" != shell ]; then
\texit 64
fi
(sleep 30) &
echo "$!" > "${WEST_FAKE_HOLD_PID:?}"
printf 'FAKE_GUEST_COMMAND_DONE\\n' >&2
exit 0
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_inherited_fd_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": 0,
            "output-contains": ["FAKE_GUEST_COMMAND_DONE"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    env["WEST_FAKE_HOLD_PID"] = str(hold_pid)
    started = time.monotonic()
    rc = test._run_guest_command_fixture(invocation, env=env)
    elapsed = time.monotonic() - started
    try:
        if hold_pid.exists():
            os.kill(int(hold_pid.read_text()), signal.SIGKILL)
    except ProcessLookupError:
        pass
    assert rc == 0, (rc, test.err_messages)
    assert elapsed < 5, elapsed

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling-any"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
printf 'ANY_RETURN_MARKER\\n' >&2
exit 7
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_any_returncode_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": "any",
            "output-contains": ["ANY_RETURN_MARKER"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    rc = test._run_guest_command_fixture(invocation, env=env)
    assert rc == 0, (rc, test.err_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    test = make_test()
    test._prefix = str(prefix)
    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))

    @contextmanager
    def fake_source_forest(_patch, _proof, *, omit_patch):
        assert omit_patch is True
        yield tempdir / "source/darling"

    def failing_build(_source_root, _proof, _build_prefix, scratch_root, *, label="RED"):
        assert label == "RED"
        (scratch_root / "diagnostic.txt").write_text("kept\n")
        raise RuntimeError("forced build failure")

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = failing_build

    try:
        test._run_guest_runtime_deploy_proof(
            {"path": "xnu/failing.patch", "module": "darling/src/external/xnu"},
            {"mode": "guest-runtime-deploy", "runtime-artifacts": []},
            {"guest_c_fixture": True, "name": "runtime_red_keep_scratch"},
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced runtime-red build failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "diagnostic.txt").read_text() == "kept\n"
    shutil.rmtree(kept[0])

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
    with test._guest_runtime_source_forest(patch, proof, omit_patch=True) as source_root:
        assert (source_root / "base.txt").read_text() == "base\n"
        assert not (source_root / "skipped.txt").exists()
        assert (source_root / "kept.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    darling_repo = tempdir / "darling"
    xnu_repo = tempdir / "xnu"
    dserver_repo = tempdir / "darlingserver"
    for repo in (darling_repo, xnu_repo, dserver_repo):
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
        (repo / "base.txt").write_text(f"{repo.name} base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    xnu_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=xnu_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dserver_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dserver_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def patch_from(repo, base_rev, path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        patch_text = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        return patch_text, commit

    skipped_patch, _ = patch_from(xnu_repo, xnu_base, "skipped.txt", "skipped\n", "skipped patch")
    dserver_patch, dserver_commit = patch_from(dserver_repo, dserver_base, "ring_abi.txt", "profile abi\n", "profile abi")

    profile_dir = tempdir / "patches/runtime"
    (profile_dir / "xnu").mkdir(parents=True)
    (profile_dir / "darlingserver").mkdir(parents=True)
    (profile_dir / "xnu/skipped.patch").write_text(skipped_patch)
    (profile_dir / "darlingserver/ring-abi.patch").write_text(dserver_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(name="darling", path="darling", abspath=str(darling_repo), revision="HEAD"),
            types.SimpleNamespace(name="xnu", path="darling/src/external/xnu", abspath=str(xnu_repo), revision=xnu_base),
            types.SimpleNamespace(name="darlingserver", path="darling/src/external/darlingserver", abspath=str(dserver_repo), revision=dserver_base),
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "xnu/skipped.patch", "module": "darling/src/external/xnu"},
            {
                "path": "darlingserver/ring-abi.patch",
                "module": "darling/src/external/darlingserver",
                "source-commit": dserver_base,
            },
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    with test._guest_runtime_source_forest(
        {"path": "xnu/skipped.patch", "module": "darling/src/external/xnu"},
        {
            "mode": "guest-runtime-deploy",
            "bad-profile": "current-minus-patch",
            "source-modules": ["darling/src/external/darlingserver"],
        },
        omit_patch=True,
    ) as source_root:
        assert not (source_root / "src/external/xnu/skipped.txt").exists()
        assert (source_root / "src/external/darlingserver/ring_abi.txt").read_text() == "profile abi\n"

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
    test._load_profile = lambda _profile: {"patches": []}
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    try:
        with test._guest_runtime_source_forest(
            {"path": "darling/example.patch", "module": "darling", "source-base": base_rev},
            {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"},
            omit_patch=True,
        ) as source_root:
            assert (source_root / "base.txt").read_text() == "base\n"
            raise RuntimeError("forced downstream failure")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced downstream failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "darling/base.txt").read_text() == "base\n"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(kept[0] / "darling")],
        cwd=repo,
        check=True,
    )
    shutil.rmtree(kept[0])

print("PASS west-test-runtime-red-contract")
PY
