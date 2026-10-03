"""Negative contract: corrupt or stale deployed bytes fail the doctor.

The doctor's default verification compares the deployed runtime artifacts
against the deployment receipt written by the deploy path (workspace commit,
component revisions, and one sha256 per deployed file). This contract proves
the failure direction:

* a receipt that matches the deployed bytes passes;
* corrupting a deployed artifact fails and names it;
* a build artifact that moved on since the deploy fails (deployed is stale);
* a prefix with deployed artifacts but no receipt fails;
* the historical md5 baseline comparison remains available as an explicit mode
  and still fails on a wrong digest.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import types
from argparse import Namespace
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
sys.path.insert(0, str(ROOT / "west_commands"))

from west_commands.deploy_receipt import build_receipt, receipt_path, write_receipt
from west_commands.doctor import DarlingDoctor


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=c@example.invalid",
         "-c", "user.name=contract", "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def arguments(prefix: Path, **overrides) -> Namespace:
    values = dict(
        prefix=str(prefix),
        build_dir=str(prefix.parent / "build"),
        expect_dyld_md5=None,
        expect_mldr_md5=None,
        expect_dserver_md5=None,
        no_baseline_file=False,
        receipt_mode="current",
        deploy_receipt=None,
        extra_prefix=[],
    )
    values.update(overrides)
    return Namespace(**values)


def check(repo: Path, prefix: Path, **overrides) -> tuple[int, list[str]]:
    doctor = DarlingDoctor.__new__(DarlingDoctor)
    doctor.fail = 0
    doctor.manifest = types.SimpleNamespace(repo_abspath=str(repo))
    doctor._checks = []
    doctor._legacy_lines = []
    doctor._current_section = "overview"
    doctor.inf = lambda message: None
    doctor.wrn = lambda message: None
    doctor.err = lambda message: None
    doctor._check_baseline(arguments(prefix, **overrides))
    problems = [
        row["message"] for row in doctor._checks if row["state"] == "problem"
    ]
    return doctor.fail, problems


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    repo = root / "manifest"
    commit = make_repo(repo)
    build = root / "build"
    build.mkdir()
    prefix = root / "prefix"

    dyld_destination = prefix / "libexec/darling/usr/lib/dyld"
    dyld_destination.parent.mkdir(parents=True, exist_ok=True)
    shared_dyld = prefix / "usr/lib/dyld"
    shared_dyld.parent.mkdir(parents=True, exist_ok=True)
    dyld_source = build / "src/external/dyld/dyld"
    dyld_source.parent.mkdir(parents=True, exist_ok=True)
    dyld_source.write_bytes(b"BUILT-DYLD-BYTES")
    shutil.copy2(dyld_source, dyld_destination)
    shutil.copy2(dyld_source, shared_dyld)

    receipt = build_receipt(
        manifest_repo=repo,
        topdir=root,
        build_dir=build,
        prefix=prefix,
        deployed=[(dyld_source, dyld_destination), (dyld_source, shared_dyld)],
    )
    assert receipt["workspace"]["manifest_commit"] == commit
    write_receipt(prefix, receipt)

    fail, problems = check(repo, prefix)
    assert fail == 0, f"matching deploy receipt must pass: {problems}"

    # Corrupt one deployed copy: the receipt still describes the built bytes.
    shared_dyld.write_bytes(b"CORRUPTED")
    fail, problems = check(repo, prefix)
    assert fail == 1, "corrupted deployed bytes must fail"
    assert any("deployed sha256" in message and "usr/lib/dyld" in message for message in problems), problems

    # Restore deployed bytes, then change the BUILD artifact: the deployed copy
    # is now stale relative to the build that is about to be deployed.
    shutil.copy2(dyld_source, shared_dyld)
    fail, problems = check(repo, prefix)
    assert fail == 0, f"restored deployed bytes must pass: {problems}"
    dyld_source.write_bytes(b"REBUILT-DYLD-BYTES")
    fail, problems = check(repo, prefix)
    assert fail == 1, "stale deployed artifact must fail"
    assert any("build artifact changed since deploy" in message for message in problems), problems

    # A prefix with deployed artifacts but no receipt must fail closed.
    receipt_path(prefix).unlink()
    fail, problems = check(repo, prefix)
    assert fail == 1, "deployed artifacts without a receipt must fail"
    assert any("no deployment receipt" in message for message in problems), problems

    # An empty, undeployed prefix is not a failure (bootstrap runs the doctor
    # before the first deploy).
    empty_prefix = root / "empty-prefix"
    empty_prefix.mkdir()
    fail, problems = check(repo, empty_prefix)
    assert fail == 0, f"undeployed prefix must not fail: {problems}"

    # Historical regression mode is still selectable and still fails on drift.
    write_receipt(prefix, receipt)
    fail, _ = check(repo, prefix, expect_dyld_md5="00000000000000000000000000000000")
    assert fail == 1, "historical md5 mode must still fail on digest drift"

print("PASS west-doctor-receipt-contract")
