#!/usr/bin/env python3
"""Behavioral negative matrix for the accepted lifecycle RuntimeCell."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from runtime_cell import (
    ARTIFACT_PATHS,
    RuntimeCellError,
    load_runtime_cell,
    observe,
    same_object,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def repository(path: Path) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "contract")
    git(path, "config", "user.email", "contract@example.invalid")
    (path / "tracked").write_text("fixture\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


def make_cell(root: Path, *, enabled: bool = True) -> tuple[dict, Path]:
    forest = root / "forest"
    workspace = forest / "workspace"
    darling = forest / "darling"
    (forest / ".west").mkdir(parents=True)
    (forest / ".west/config").write_text("[manifest]\npath = workspace\n")
    workspace_revision = repository(workspace)
    darling_revision = repository(darling)
    (workspace / "west.lock.yml").write_text(
        "manifest:\n  projects:\n"
        f"  - name: darling\n    revision: {darling_revision}\n"
    )
    git(workspace, "add", "west.lock.yml")
    git(workspace, "commit", "-qm", "lock")
    build = root / "build"
    prefix = root / "prefix"
    build.mkdir(); prefix.mkdir()
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_INSTALL_PREFIX:PATH={prefix}\n"
        f"CMAKE_HOME_DIRECTORY:PATH={darling}\n"
        f"DARLING_LIFECYCLE_CONTROLLER_CRATE:PATH={workspace / 'lifecycle/operation-boundary'}\n"
        f"DARLING_LIFECYCLE_COHORT_V1:BOOL={'ON' if enabled else 'OFF'}\n"
    )
    (workspace / "lifecycle/operation-boundary").mkdir(parents=True)
    for index, (name, (build_relative, deployed_relative)) in enumerate(ARTIFACT_PATHS.items()):
        if name == "lifecycle_controller_worker" and not enabled:
            continue
        source = build / build_relative
        deployed = prefix / deployed_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        deployed.parent.mkdir(parents=True, exist_ok=True)
        content = f"{name}-{index}\n".encode()
        source.write_bytes(content); deployed.write_bytes(content)
    prefix_value = prefix.stat()
    sidecar = prefix.with_name(f"{prefix.name}.eunion-sidecar-v1"); sidecar.mkdir()
    sidecar_value = sidecar.stat()
    state = prefix / ".darling-prefix-state-v3"
    state.write_text(
        "DARLING_PREFIX_STATE_V3\n"
        "schema_version=3\nruntime_mode=rootless-eunion\ngeneration=7\n"
        f"prefix_device={prefix_value.st_dev}\nprefix_inode={prefix_value.st_ino}\n"
        f"sidecar_device={sidecar_value.st_dev}\nsidecar_inode={sidecar_value.st_ino}\n"
        f"owner_uid={prefix_value.st_uid}\nowner_gid={prefix_value.st_gid}\n"
        "provenance=darling-runtime-prefix-sidecar-v1\n"
    )
    state.chmod(0o600)
    lock = prefix / ".lifecycle.lock"; lock.write_bytes(b""); lock.chmod(0o600)
    lower = prefix / "libexec/darling"
    controller = prefix / "bin/darlingserver"
    lower_value = lower.stat(); controller_value = controller.stat()
    worker = prefix / "libexec/darling-lifecycle-controller-worker"
    binding = prefix / ".darling-runtime-lower-binding-v1"
    binding_text = (
        ("DARLING_RUNTIME_LOWER_BINDING_V3\n" if enabled else "DARLING_RUNTIME_LOWER_BINDING_V2\n")
        + ("schema_version=3\n" if enabled else "schema_version=2\n")
        + "transaction_id=fixture\nprefix_generation=7\n"
        f"session_prefix_device={prefix_value.st_dev}\nsession_prefix_inode={prefix_value.st_ino}\n"
        "destination=libexec/darling\n"
        f"prefix_device={prefix_value.st_dev}\nprefix_inode={prefix_value.st_ino}\n"
        f"lower_device={lower_value.st_dev}\nlower_inode={lower_value.st_ino}\n"
        f"lower_type=directory\nlower_mode={lower_value.st_mode & 0o7777}\n"
        f"lower_uid={lower_value.st_uid}\nlower_gid={lower_value.st_gid}\n"
        "controller_destination=bin/darlingserver\n"
        f"controller_device={controller_value.st_dev}\ncontroller_inode={controller_value.st_ino}\n"
        f"controller_type=regular\ncontroller_mode={controller_value.st_mode & 0o7777}\n"
        f"controller_uid={controller_value.st_uid}\ncontroller_gid={controller_value.st_gid}\n"
    )
    if enabled:
        worker.chmod(0o755)
        worker_value = worker.stat()
        binding_text += (
            "worker_destination=libexec/darling-lifecycle-controller-worker\n"
            f"worker_device={worker_value.st_dev}\nworker_inode={worker_value.st_ino}\n"
            f"worker_type=regular\nworker_mode={worker_value.st_mode & 0o7777}\n"
            f"worker_uid={worker_value.st_uid}\nworker_gid={worker_value.st_gid}\n"
            "provenance=product-deployment-transaction-v3\n"
        )
    else:
        binding_text += "provenance=product-deployment-transaction-v2\n"
    binding.write_text(
        binding_text
    )
    binding.chmod(0o600)
    arguments = {
        "forest": forest,
        "workspace": workspace,
        "build_dir": build,
        "runtime_prefix": prefix,
        "cohort_enabled": enabled,
        "profile": "homebrew-rootless-bootstrap-minimal",
    }
    return arguments, prefix


def reject(arguments: dict, expected: str) -> None:
    try:
        load_runtime_cell(**arguments)
    except RuntimeCellError as error:
        assert expected in str(error), (expected, str(error))
        return
    raise AssertionError(f"RuntimeCell accepted negative: {expected}")


def isolated(root: Path, name: str, mutate, expected: str) -> None:
    arguments, prefix = make_cell(root / name)
    mutate(arguments, prefix)
    reject(arguments, expected)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="runtime-cell-contract-") as directory:
        root = Path(directory)
        arguments, prefix = make_cell(root / "valid")
        cell = load_runtime_cell(**arguments)
        assert cell.state.generation == 7 and len(cell.artifacts) == len(ARTIFACT_PATHS)

        off_arguments, off_prefix = make_cell(root / "valid-off", enabled=False)
        off_cell = load_runtime_cell(**off_arguments)
        assert all(artifact.name != "lifecycle_controller_worker" for artifact in off_cell.artifacts)
        assert not (off_prefix / "libexec/darling-lifecycle-controller-worker").exists()

        def downgrade_binding(_args: dict, p: Path) -> None:
            path = p / ".darling-runtime-lower-binding-v1"
            lines = path.read_text().splitlines()
            lines[0] = "DARLING_RUNTIME_LOWER_BINDING_V2"
            lines = [line for line in lines if not line.startswith("worker_")]
            lines = [
                "schema_version=2" if line == "schema_version=3" else
                "provenance=product-deployment-transaction-v2"
                if line == "provenance=product-deployment-transaction-v3" else line
                for line in lines
            ]
            path.write_text("\n".join(lines) + "\n")
        isolated(root, "on-v2-binding", downgrade_binding, "requires binding schema v3")

        before = observe(prefix / "bin/darling")
        os.link(prefix / "bin/darling", prefix / "bin/darling-link")
        after = observe(prefix / "bin/darling")
        assert same_object(before, after) and before.nlink != after.nlink
        (prefix / "bin/darling").chmod(0o600)
        metadata_changed = observe(prefix / "bin/darling")
        assert same_object(after, metadata_changed)
        assert after.security != metadata_changed.security

        # A real alternate prefix reaches the explicit cache mismatch gate.
        args, _prefix = make_cell(root / "cross-prefix-real")
        alternate = root / "cross-prefix-real/alternate"; alternate.mkdir()
        args["runtime_prefix"] = alternate
        reject(args, "build/runtime prefix mismatch")
        isolated(
            root, "stale-artifact",
            lambda _args, p: (p / "bin/darling").write_bytes(b"stale\n"),
            "stale or mixed: darling",
        )
        def flat_decoy(_args: dict, p: Path) -> None:
            (p / "libexec/darling/sbin/launchd").unlink()
            (p / "sbin").mkdir(exist_ok=True)
            (p / "sbin/launchd").write_bytes(b"flat\n")
        isolated(root, "flat", flat_decoy, "missing: launchd")
        isolated(root, "missing-lock", lambda _args, p: (p / ".lifecycle.lock").unlink(), "lock is missing")
        def generation(_args: dict, p: Path) -> None:
            path = p / ".darling-prefix-state-v3"
            path.write_text(path.read_text().replace("generation=7", "generation=8"))
        isolated(root, "generation", generation, "binding identity mismatch")
        isolated(
            root, "wrong-profile", lambda args, _p: args.update(profile="perf"),
            "wrong runtime profile",
        )
        def incomplete(args: dict, _p: Path) -> None:
            (args["forest"] / ".west/config").unlink()
        isolated(root, "incomplete", incomplete, "not a West forest")
        def missing_revision(args: dict, _p: Path) -> None:
            lock = args["workspace"] / "west.lock.yml"
            lock.write_text(lock.read_text().replace(
                git(args["forest"] / "darling", "rev-parse", "HEAD"), "f" * 40
            ))
        isolated(root, "missing-revision", missing_revision, "cat-file")
        def binding_inode(_args: dict, p: Path) -> None:
            path = p / ".darling-runtime-lower-binding-v1"
            path.write_text(path.read_text().replace("prefix_generation=7", "prefix_generation=9"))
        isolated(root, "binding-generation", binding_inode, "binding identity mismatch")
        def binding_escape(_args: dict, p: Path) -> None:
            path = p / ".darling-runtime-lower-binding-v1"
            path.write_text(path.read_text().replace("destination=libexec/darling", "destination=../escape"))
        isolated(root, "binding-escape", binding_escape, "destination mismatch")
        def binding_extra(_args: dict, p: Path) -> None:
            path = p / ".darling-runtime-lower-binding-v1"
            path.write_text(path.read_text() + "forged=yes\n")
        isolated(root, "binding-extra", binding_extra, "field set is not exact")
    print("runtime cell contract: PASS negatives=12")


if __name__ == "__main__":
    main()
