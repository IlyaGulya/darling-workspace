"""Installed image integrity survives mutable builds; corruption and unsafe inventory fail."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from argparse import Namespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")
west_commands_module.WestCommand = type("WestCommand", (), {})
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.deploy_receipt import (
    publish_install_receipt, read_receipt, receipt_path, verify_receipt, write_receipt,
)
from west_commands import runtime_boot_smoke
from west_commands.doctor import DarlingDoctor


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "tracked.txt").write_text("original\n")
    git(path, "add", "tracked.txt")
    git(path, "-c", "user.name=contract", "-c", "user.email=c@example.invalid", "commit", "-qm", "seed")
    return git(path, "rev-parse", "HEAD")


def check(runtime_root: Path, prefix: Path) -> tuple[int, list[str], list[str]]:
    doctor = DarlingDoctor.__new__(DarlingDoctor)
    doctor.fail = 0
    doctor._checks = []
    doctor._legacy_lines = []
    doctor._current_section = "overview"
    doctor._check_installation(Namespace(runtime_root=runtime_root, prefix=prefix))
    return doctor.fail, [row["message"] for row in doctor._checks if row["state"] == "problem"], [
        row["message"] for row in doctor._checks if row["state"] == "warning"
    ]


with tempfile.TemporaryDirectory(prefix="doctor-receipt-contract-") as temporary:
    root = Path(temporary)
    manifest = root / "manifest"
    manifest_revision = make_repo(manifest)
    source = root / "prepared-source"
    make_repo(source)
    child = source / "nested"
    child_revision = make_repo(child)
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{child_revision},nested")
    git(source, "-c", "user.name=contract", "-c", "user.email=c@example.invalid", "commit", "-qm", "nested source")
    source_revision = git(source, "rev-parse", "HEAD")
    build = root / "build"
    build.mkdir()
    runtime = root / "runtime"
    guest = root / "guest"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "libexec/darling/usr/lib").mkdir(parents=True)
    launcher = runtime / "bin/darling"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    server = runtime / "bin/darlingserver"
    server.write_bytes(b"installed-server")
    dyld = runtime / "libexec/darling/usr/lib/dyld"
    dyld.write_bytes(b"installed-dyld")
    external = root / "external-secret"
    external.write_bytes(b"not installed")
    guest_link = runtime / "libexec/darling/absolute-guest-link"
    guest_link.symlink_to(external)
    cache = build / "CMakeCache.txt"
    cache.write_text(
        f"CMAKE_INSTALL_PREFIX:PATH={runtime}\nCMAKE_HOME_DIRECTORY:INTERNAL={source}\nDARLING_EUNION:BOOL=ON\n"
    )
    install_manifest = build / "install_manifest.txt"
    installed = [launcher, server, dyld, guest_link]
    install_manifest.write_text("\n".join(map(str, installed)) + "\n")
    receipt = publish_install_receipt(runtime, build, manifest)
    assert receipt == read_receipt(runtime)
    assert receipt["schema_version"] == 2
    assert receipt["workspace"]["manifest_commit"] == manifest_revision
    assert receipt["source"]["revision"] == source_revision
    assert receipt["source"]["dirty"] is False
    assert receipt["source"]["untracked"] == 0
    assert receipt["source"]["gitlinks"] == [{
        "path": "nested", "gitlink_revision": child_revision, "revision": child_revision,
        "dirty": False, "untracked": 0,
    }]
    assert receipt["build"]["directory"] == str(build)
    assert len(receipt["build"]["cache_sha256"]) == 64
    link_record = next(row for row in receipt["artifacts"] if row["kind"] == "symlink")
    # A selected prepared closure, not a foreign canonical checkout, owns workspace pin checks.
    canonical = root / "darling"
    make_repo(canonical)
    (canonical / "tracked.txt").write_text("foreign edits\n")
    project = types.SimpleNamespace(name="darling", path="darling", abspath=str(canonical), revision=source_revision)
    nested_project = types.SimpleNamespace(
        name="nested", path="darling/nested", abspath=str(canonical / "nested"), revision=child_revision,
    )
    docs = canonical / "docs/darling-docs"
    docs_revision = make_repo(docs)
    docs_project = types.SimpleNamespace(
        name="docs", path="darling/docs/darling-docs", abspath=str(docs), revision=docs_revision,
    )
    workspace_doctor = DarlingDoctor.__new__(DarlingDoctor)
    workspace_doctor.topdir = str(root)
    workspace_doctor.manifest = types.SimpleNamespace(
        repo_abspath=str(manifest), projects=[project, nested_project, docs_project], is_active=lambda project: True,
    )
    workspace_doctor.fail = 0
    workspace_doctor._checks = []
    workspace_doctor._legacy_lines = []
    workspace_doctor._check_manifest_drift(root, Namespace(source_root=source, build_dir=None, allow_drift=[]))
    assert workspace_doctor.fail == 0, workspace_doctor._checks
    (child / "tracked.txt").write_text("selected dirty nested source\n")
    workspace_doctor._check_manifest_drift(root, Namespace(source_root=source, build_dir=None, allow_drift=[]))
    assert workspace_doctor.fail == 1
    assert any("tracked modifications" in row["message"] for row in workspace_doctor._checks)
    (child / "tracked.txt").write_text("original\n")

    assert link_record["target"] == str(external) and "sha256" not in link_record
    assert verify_receipt(receipt)[0] == []
    assert check(runtime, guest)[0] == 0
    # Smoke protocol requires the guest log oracle AND product-owned clean teardown.
    smoke_outcome = {"oracle": True, "state": "clean", "shutdown_rc": 0, "timeout": False}
    smoke_calls = []

    def smoke_process(command, **kwargs):
        smoke_calls.append((command, kwargs))
        assert kwargs["env"]["DPREFIX"] == str(guest)
        assert kwargs["env"]["DARLING_LAUNCHER"] == str(launcher)
        assert not any(name in kwargs["env"] for name in (
            "DARLING_ROOTLESS", "DARLING_EUNION", "DARLING_NOOVERLAYFS",
        ))
        if command == [str(launcher), "shutdown"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        assert command[command.index("--runtime-root") + 1] == str(runtime)
        log = Path(command[command.index("--log") + 1])
        bundle = Path(command[command.index("--bundle") + 1])
        if smoke_outcome["timeout"]:
            raise subprocess.TimeoutExpired(command, 1, output=b"watchdog")
        log.write_text(runtime_boot_smoke.MARKER + "\n" if smoke_outcome["oracle"] else "")
        bundle.mkdir()
        (bundle / "cleanup.json").write_text(json.dumps({
            "state": smoke_outcome["state"], "shutdown_rc": smoke_outcome["shutdown_rc"],
            "processes": 0, "mounts": 0,
        }))
        return types.SimpleNamespace(returncode=0, stdout="VERDICT: PASS\n", stderr="")

    with mock.patch.dict(os.environ, {"DW_STATE_ROOT": str(root / "state")}, clear=True), mock.patch.object(
        runtime_boot_smoke.subprocess, "run", side_effect=smoke_process,
    ):
        passed, detail = runtime_boot_smoke.run_boot_smoke(ROOT, guest, runtime_root=runtime)
        assert passed and "evidence:" in detail
        smoke_outcome["state"] = "rescued"
        assert not runtime_boot_smoke.run_boot_smoke(ROOT, guest, runtime_root=runtime)[0]
        smoke_outcome["state"] = "clean"
        smoke_outcome["shutdown_rc"] = 7
        assert not runtime_boot_smoke.run_boot_smoke(ROOT, guest, runtime_root=runtime)[0]
        smoke_outcome["shutdown_rc"] = 0
        smoke_outcome["oracle"] = False
        assert not runtime_boot_smoke.run_boot_smoke(ROOT, guest, runtime_root=runtime)[0]
        smoke_outcome["timeout"] = True
        assert not runtime_boot_smoke.run_boot_smoke(ROOT, guest, runtime_root=runtime)[0]
        assert smoke_calls[-1][0] == [str(launcher), "shutdown"]
    assert not guest.exists(), "smoke orchestration protocol stub must not initialize guest state"

    assert not guest.exists(), "read-only doctor must not create guest state"

    # Source provenance distinguishes tracked changes from untracked input.
    (source / "untracked.txt").write_text("untracked\n")
    untracked_receipt = publish_install_receipt(runtime, build, manifest)
    assert untracked_receipt["source"]["dirty"] is False
    assert untracked_receipt["source"]["untracked"] == 1
    (source / "tracked.txt").write_text("edited\n")
    dirty_receipt = publish_install_receipt(runtime, build, manifest)
    assert dirty_receipt["source"]["dirty"] is True
    write_receipt(runtime, receipt)

    # Mutable source/build B, including a reconfigured install destination, cannot invalidate image A.
    cache.write_text("CMAKE_INSTALL_PREFIX:PATH=/another-install\n")
    (build / "rebuilt-dyld").write_bytes(b"new candidate")
    external.write_bytes(b"guest symlink target changed outside installation")
    assert verify_receipt(receipt)[0] == []
    assert check(runtime, guest)[0] == 0
    shutil.rmtree(build)
    assert verify_receipt(receipt)[0] == []

    dyld.write_bytes(b"corrupted")
    failure, problems, _ = check(runtime, guest)
    assert failure == 1 and any("usr/lib/dyld" in problem for problem in problems), problems
    dyld.write_bytes(b"installed-dyld")
    launcher.chmod(0o700)
    assert verify_receipt(receipt)[0], "installed executable mode is part of image integrity"
    launcher.chmod(0o755)
    guest_link.unlink()
    guest_link.symlink_to("/different/guest/target")
    assert verify_receipt(receipt)[0], "symlink targets must be verified without following them"
    guest_link.unlink()
    guest_link.symlink_to(external)

    # A nested symlink must not redirect inventory reads into the host.
    redirect = runtime / "redirect"
    redirect.symlink_to(root, target_is_directory=True)
    forged = {**receipt, "artifacts": [{"path": "redirect/external-secret", "kind": "file", "mode": 0o644, "sha256": "x", "size": 0}]}
    assert verify_receipt(forged)[0]
    assert verify_receipt({**receipt, "artifacts": [{"path": "../external-secret"}]})[0]

    # A copied receipt cannot certify a different installation root.
    copied = root / "copied-runtime"
    copied.mkdir()
    shutil.copyfile(receipt_path(runtime), receipt_path(copied))
    try:
        read_receipt(copied)
    except ValueError:
        pass
    else:
        raise AssertionError("receipt copied to another installation was accepted")

    receipt_path(runtime).unlink()
    failure, problems, warnings = check(runtime, guest)
    assert failure == 0 and not problems
    assert any("unknown/unverified" in warning for warning in warnings)
    assert read_receipt(runtime) is None, "ordinary packages must not acquire fabricated provenance"
    receipt_path(runtime).write_text(json.dumps({"schema_version": 1}))
    try:
        read_receipt(runtime)
    except ValueError:
        pass
    else:
        raise AssertionError("legacy schema was accepted")

print("PASS west-doctor-receipt-contract")
