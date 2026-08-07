#!/usr/bin/env python3
"""Production-consumer mutation contract for dar-4ush.6.

The fixture uses the same schema-v2 immutable locks, schema-v3 lock-first
mapping/composition, West generated lock, oracle evidence, capture manifest,
lock-first evidence, and acceptance result as the production consumers.  The
mutation registry changes one typed input at a time (plus two composed cases),
recomputes the package index/checksums when appropriate, and then drives the
real preflight, materializer, capture, immutable oracle, and hosted compare
entry points.  A small package-index envelope is only an integrity adapter;
it is never the acceptance oracle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[0:0] = [str(ROOT / "west_commands"), str(ROOT / "tests"), str(ROOT / "ci")]
import patch_stack_acceptance as capture
import patch_stack_immutable_oracle as immutable_oracle
import patch_stack_lock_first as lock_first
import patch_stack_lock_first_acceptance as hosted
import patch_stack_materialize as materialize
import patch_stack_preflight as preflight
import patch_stack_export as exporter


class MutationContractError(RuntimeError):
    pass


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise MutationContractError(message)


def run(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise MutationContractError(
            f"{cwd}: {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return run(cwd, "git", *args, check=check).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def regular_files(root: Path) -> list[str]:
    values: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        values.append(path.relative_to(root).as_posix())
    return values


def strict_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    fail(isinstance(value, dict), f"{label} is not an object")
    fail(set(value) == fields, f"{label} fields differ: {sorted(set(value) ^ fields)}")
    return value


@dataclass(frozen=True)
class Fixture:
    root: Path
    workspace: Path
    module: Path
    mirror: Path
    bundle: Path
    package: Path
    lock_rel: str
    mapping_rel: str
    composition_rel: str
    profile_rel: str
    patch_rel: str
    generated_rel: str
    oracle_rel: str
    modules_rel: str
    manifest_rel: str
    evidence_rel: str
    result_rel: str
    package_index_rel: str
    base: str
    source: str
    base_tree: str
    source_tree: str
    workspace_commit: str
    workspace_tree: str
    package_files: frozenset[str]
    bindings: dict[str, str]


@dataclass(frozen=True)
class DisposableFixtureScope:
    """Contain every fixture write below one caller-owned temporary root."""

    root: Path

    def path(self, *parts: str) -> Path:
        candidate = self.root.joinpath(*parts)
        try:
            candidate.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise MutationContractError(
                f"fixture path escapes disposable root: {candidate}"
            ) from error
        return candidate

def make_fake_west(root: Path) -> Path:
    bindir = root / "bin"
    bindir.mkdir()
    west = bindir / "west"
    west.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'topdir':\n"
        "    print(os.getcwd())\n"
        "elif len(sys.argv) >= 2 and sys.argv[1] == 'list':\n"
        "    print('darling\\tdarling')\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    west.chmod(west.stat().st_mode | stat.S_IXUSR)
    return bindir


def immutable_lock(mirror: Path, base: str, source: str, tree: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "project": {"name": "darling", "path": "."},
        "upstream": {"url": mirror.as_uri(), "base_commit": base},
        "mirror": {
            "url": mirror.as_uri(),
            "base_ref": f"refs/tags/patch-stack/v1/bases/{base}",
            "base_oid": base,
            "source_ref": f"refs/tags/patch-stack/v1/sources/{source}",
            "source_oid": source,
        },
        "source_commit": source,
        "ordered_commits": [source],
        "expected_tree": tree,
    }


def build_fixture(root: Path) -> Fixture:
    scope = DisposableFixtureScope(root.resolve())
    workspace = scope.path("workspace")
    module = scope.path("workspace", "darling")
    mirror = scope.path("mirror.git")
    bundle = scope.path("package-source.bundle")
    package = scope.path("package")
    (workspace / "locks" / "patch-stack").mkdir(parents=True)
    scope.path("workspace", "patches", "homebrew", "darling").mkdir(parents=True)
    module.mkdir(parents=True)
    run(module, "git", "init", "-q")
    # Match the immutable oracle's explicit replay identity so generated
    # integration revisions are deterministic across both production paths.
    git(module, "config", "user.name", "West Test")
    git(module, "config", "user.email", "west-test@example.invalid")
    (module / "payload").write_text("base\n")
    run(module, "git", "add", "payload")
    git(module, "commit", "-qm", "base")
    base = git(module, "rev-parse", "HEAD")
    base_tree = git(module, "rev-parse", "HEAD^{tree}")
    (module / "payload").write_text("source\n")
    git(module, "commit", "-qam", "source")
    source = git(module, "rev-parse", "HEAD")
    source_tree = git(module, "rev-parse", "HEAD^{tree}")
    git(module, "tag", f"patch-stack/v1/bases/{base}", base)
    git(module, "tag", f"patch-stack/v1/sources/{source}", source)
    git(module, "branch", "-f", "integration/homebrew", base)
    git(module, "checkout", "-q", "integration/homebrew")
    run(root, "git", "clone", "--bare", "-q", str(module), str(mirror))
    run(module, "git", "bundle", "create", str(bundle), "--all")

    lock_rel = "locks/patch-stack/mutation.yml"
    mapping_rel = "locks/patch-stack/mapping.yml"
    composition_rel = "locks/patch-stack/composition.yml"
    profile_rel = "patches/homebrew/patches.yml"
    patch_rel_path = Path("patches") / "homebrew" / "darling" / "mutation.patch"
    patch_rel = patch_rel_path.as_posix()
    profile_patch_rel = Path("darling") / "mutation.patch"
    patch_path = scope.path("workspace", *patch_rel_path.parts)
    generated_rel = "patches/homebrew/west.lock.yml"
    lock_value = immutable_lock(mirror, base, source, source_tree)
    write_yaml(workspace / lock_rel, lock_value)
    profile_value = {
        "version": 1,
        "description": "Production-shaped mutation fixture profile.",
        "integration-date": "2026-01-01T00:00:00+00:00",
        "test-profiles": {},
        "fixture-profiles": {},
        "patches": [{"module": "darling", "path": profile_patch_rel.as_posix()}],
    }
    write_yaml(workspace / profile_rel, profile_value)
    # Keep the portable fixture patch in the real production mbox format.
    # Lock-first still keys replay to the immutable commit, while capture and
    # export exercise the same profile patch path used by production.
    patch_bytes = run(module, "git", "format-patch", "--stdout", f"{base}..{source}").stdout
    patch_path.write_text(patch_bytes)
    profile_value["patches"][0].update({
        "sha256sum": hashlib.sha256(patch_bytes.encode()).hexdigest(),
        "source-branch": "fix/mutation-production",
        "source-commit": source,
        "bead": "dar-4ush.6",
        "publication-status": "blocked",
    })
    write_yaml(workspace / profile_rel, profile_value)
    mapping_value = {
        "schema_version": 3,
        "profile": "homebrew",
        "batch_id": "mutation-production-batch-v1",
        "expected_count": 1,
        "composition": "composition.yml",
        "series": [{
            "profile": "homebrew", "module": "darling",
            "patch": profile_patch_rel.as_posix(), "lock": "mutation.yml",
        }],
    }
    write_yaml(workspace / mapping_rel, mapping_value)
    frozen = {
        "manifest": {"projects": [{
            "name": "darling", "path": "darling", "revision": base,
        }]}
    }
    write_yaml(workspace / "west.lock.yml", frozen)
    write_yaml(workspace / generated_rel, frozen)
    write_yaml(
        workspace / "locks/patch-stack/lock-first-profiles-v1.yml",
        {"schema_version": 1, "profiles": [{"profile": "homebrew", "mapping": "mapping.yml"}]},
    )
    composition_value = {
        "schema_version": 3,
        "profile": "homebrew",
        "prerequisites": [],
        "frozen_manifest": {
            "path": "west.lock.yml",
            "sha256": sha256(workspace / "west.lock.yml"),
        },
        "mapping": {
            "path": "mapping.yml",
            "sha256": sha256(workspace / mapping_rel),
            "batch_id": mapping_value["batch_id"],
            "expected_count": 1,
        },
        "modules": [{
            "module": "darling",
            "starting": {"tree": base_tree},
            "series": [{
                "patch": profile_patch_rel.as_posix(), "lock": "mutation.yml",
                "expected_applied_tree": source_tree,
            }],
            "final_tree": source_tree,
            "integration_final_tree": source_tree,
        }],
    }
    write_yaml(workspace / composition_rel, composition_value)
    (workspace / ".gitignore").write_text("darling/\n")
    run(workspace, "git", "init", "-q")
    git(workspace, "config", "user.name", "Mutation Workspace")
    git(workspace, "config", "user.email", "workspace@example.invalid")
    run(workspace, "git", "add", ".")
    git(workspace, "commit", "-qm", "trusted reconstructed workspace")
    workspace_commit = git(workspace, "rev-parse", "HEAD")
    workspace_tree = git(workspace, "rev-parse", "HEAD^{tree}")

    bindir = make_fake_west(root)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
    try:
        plan = lock_first.plan("homebrew", profile_value["patches"], workspace / mapping_rel)
        oracle_path = root / "immutable-oracle.json"
        immutable_oracle.apply(workspace, "homebrew", workspace / mapping_rel, oracle_path)
        results, _stats = lock_first.materialize_batch_into(module, list(plan), composition=plan.composition)
        generated_value, _generated_row = immutable_oracle.generated_lock(
            frozen, "homebrew", ["darling"], {"darling": module}
        )
        (workspace / generated_rel).write_text(
            yaml.safe_dump(generated_value, sort_keys=False, width=1000)
        )
        modules_path = root / "lock-first-modules.json"
        manifest_path = root / "lock-first-manifest.json"
        capture.capture(workspace, "homebrew", modules_path, manifest_path)
        evidence_path = root / "lock-first-evidence.json"
        lock_first.write_batch_evidence(evidence_path, results, plan.batch)
        result_path = root / "acceptance-result.json"
        transaction_root = root / "transactions"
        transaction_root.mkdir()
        try:
            hosted.compare_immutable_oracle(
                oracle_path, modules_path, manifest_path, evidence_path,
                workspace / mapping_rel, workspace, transaction_root, result_path,
            )
        except hosted.AcceptanceError as error:
            raise MutationContractError(
                f"baseline compare failed: {error}; "
                f"oracle={read_json(oracle_path).get('generated_profile_locks')!r}; "
                f"manifest={read_json(manifest_path).get('generated_profile_locks')!r}; "
                f"actual={sha256(workspace / generated_rel)}; "
                f"generated={read_yaml(workspace / generated_rel)!r}; "
                f"oracle-modules={read_json(oracle_path).get('modules')!r}"
            ) from error
    finally:
        os.environ["PATH"] = old_path

    package_paths = {
        lock_rel: workspace / lock_rel,
        mapping_rel: workspace / mapping_rel,
        composition_rel: workspace / composition_rel,
        "locks/patch-stack/lock-first-profiles-v1.yml": workspace / "locks/patch-stack/lock-first-profiles-v1.yml",
        profile_rel: workspace / profile_rel,
        patch_rel: patch_path,
        "west.lock.yml": workspace / "west.lock.yml",
        generated_rel: workspace / generated_rel,
        "bundles/source.bundle": bundle,
        "immutable-oracle.json": oracle_path,
        "lock-first-modules.json": modules_path,
        "manifest.json": manifest_path,
        "evidence/lock-first-evidence.json": evidence_path,
        "acceptance-result.json": result_path,
    }
    for relative, source_path in package_paths.items():
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    bindings = {
        relative: relative
        for relative in (
            lock_rel, mapping_rel, composition_rel,
            "locks/patch-stack/lock-first-profiles-v1.yml", profile_rel,
            patch_rel, "west.lock.yml", generated_rel,
        )
    }
    package_index = {
        "schema_version": 1,
        "package_id": "patch-stack-production-mutation-v1",
        "workspace_commit": workspace_commit,
        "workspace_tree": workspace_tree,
        "files": [],
        "bindings": [
            {"package_path": package_path, "workspace_path": workspace_path,
             "sha256": sha256(workspace / workspace_path)}
            for package_path, workspace_path in bindings.items()
        ],
    }
    write_json(package / "package-index.json", package_index)
    refresh_package_integrity(package)
    return Fixture(
        root=root, workspace=workspace, module=module, mirror=mirror, bundle=bundle,
        package=package, lock_rel=lock_rel, mapping_rel=mapping_rel,
        composition_rel=composition_rel, profile_rel=profile_rel,
        patch_rel=patch_rel,
        generated_rel=generated_rel, oracle_rel="immutable-oracle.json",
        modules_rel="lock-first-modules.json", manifest_rel="manifest.json",
        evidence_rel="evidence/lock-first-evidence.json",
        result_rel="acceptance-result.json", package_index_rel="package-index.json",
        base=base, source=source, base_tree=base_tree, source_tree=source_tree,
        workspace_commit=workspace_commit, workspace_tree=workspace_tree,
        package_files=frozenset(regular_files(package)), bindings=bindings,
    )


def refresh_package_integrity(package: Path, *, refresh_file_list: bool = False) -> None:
    index_path = package / "package-index.json"
    index = read_json(index_path)
    payload = sorted(relative for relative in regular_files(package)
                     if relative not in {"package-index.json", "SHA256SUMS"})
    if refresh_file_list:
        index["files"] = []
    listed = {item["path"]: item for item in index.get("files", [])}
    index["files"] = [
        {"path": relative, "sha256": sha256(package / relative)}
        for relative in (payload if refresh_file_list else sorted(set(payload) | set(listed)))
        if (package / relative).exists() and not (package / relative).is_symlink()
    ]
    write_json(index_path, index)
    entries = {"package-index.json": sha256(index_path)}
    for relative in regular_files(package):
        if relative != "SHA256SUMS":
            entries[relative] = sha256(package / relative)
    (package / "SHA256SUMS").write_text(
        "".join(f"{digest}  {relative}\n" for relative, digest in sorted(entries.items()))
    )


def verify_sha(package: Path, expected: set[str]) -> None:
    lines = (package / "SHA256SUMS").read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        parts = line.split("  ", 1)
        fail(len(parts) == 2 and len(parts[0]) == 64 and parts[1] not in parsed, "invalid SHA256SUMS")
        parsed[parts[1]] = parts[0]
    wanted = (expected - {"SHA256SUMS"}) | {"package-index.json"}
    fail(set(parsed) == wanted, "SHA256SUMS allowlist differs")
    for relative, digest in parsed.items():
        path = package / relative
        fail(path.is_file() and not path.is_symlink() and sha256(path) == digest, f"checksum mismatch: {relative}")


def verify_package_index(fixture: Fixture, package: Path) -> None:
    actual = set(regular_files(package))
    fail(actual == set(fixture.package_files), f"package allowlist differs: {sorted(actual ^ set(fixture.package_files))}")
    index = strict_object(read_json(package / fixture.package_index_rel), {"schema_version", "package_id", "workspace_commit", "workspace_tree", "files", "bindings"}, "package index")
    fail(index["schema_version"] == 1 and index["package_id"] == "patch-stack-production-mutation-v1", "package index identity differs")
    fail(index["workspace_commit"] == fixture.workspace_commit and index["workspace_tree"] == fixture.workspace_tree, "trusted workspace anchor differs")
    verify_trusted_workspace(fixture, fixture.workspace)
    payload = set(fixture.package_files) - {"package-index.json", "SHA256SUMS"}
    listed: set[str] = set()
    for entry in index["files"]:
        item = strict_object(entry, {"path", "sha256"}, "package index file")
        relative = item["path"]
        fail(relative in payload and relative not in listed, "package index payload differs")
        listed.add(relative)
        fail(item["sha256"] == sha256(package / relative), f"package index digest differs: {relative}")
    fail(listed == payload, "package index payload set differs")
    seen: set[str] = set()
    for entry in index["bindings"]:
        item = strict_object(entry, {"package_path", "workspace_path", "sha256"}, "package binding")
        package_path, workspace_path = item["package_path"], item["workspace_path"]
        fail(package_path in fixture.bindings and fixture.bindings[package_path] == workspace_path and package_path not in seen, "package binding differs")
        seen.add(package_path)
        left, right = package / package_path, fixture.workspace / workspace_path
        fail(left.is_file() and right.is_file() and not left.is_symlink() and left.read_bytes() == right.read_bytes(), f"workspace binding differs: {package_path}")
        fail(item["sha256"] == sha256(right), f"workspace binding digest differs: {package_path}")
    fail(seen == set(fixture.bindings), "package bindings incomplete")
    verify_sha(package, set(fixture.package_files))


def verify_trusted_workspace(fixture: Fixture, workspace: Path) -> None:
    fail(
        git(workspace, "rev-parse", "HEAD") == fixture.workspace_commit
        and git(workspace, "rev-parse", "HEAD^{tree}") == fixture.workspace_tree,
        "trusted workspace anchor differs",
    )


def copy_package_workspace(fixture: Fixture, package: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the exact trusted workspace commit/tree.  A fresh git init and
    # synthetic commit would make the captured manifest unverifiable and let a
    # stale package manifest pass only by accident.
    run(destination.parent, "git", "clone", "-q", str(fixture.workspace), str(destination))
    for relative in fixture.bindings:
        source = package / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # The package's generated lock is the post-materialization state and must
    # be seen as the one declared generated modification by capture().  The
    # trusted clone already has the frozen baseline in HEAD.
    generated = destination / fixture.generated_rel
    shutil.copy2(package / fixture.generated_rel, generated)
    return destination


def production_validate(
    fixture: Fixture,
    package: Path,
    case_root: Path,
    candidate_mutation: Callable[[Path], None] | None = None,
    result_path_setup: Callable[[Path], None] | None = None,
    candidate_manifest_mutation: Callable[[Path], None] | None = None,
    transaction_ref_setup: Callable[[Path], None] | None = None,
) -> None:
    # Reconstruct from the immutable baseline, then apply a binding mutation
    # to the candidate workspace.  This keeps the package-integrity gate and
    # the production-consumer gate independent: bound-field mutations must be
    # rejected by both, rather than disappearing before a real consumer sees
    # them.
    candidate = copy_package_workspace(fixture, fixture.package, case_root / "candidate")
    if candidate_mutation is not None:
        candidate_mutation(candidate)
    module = case_root / "candidate/darling"
    imported = case_root / "bundle-module"
    clone = run(case_root, "git", "clone", "-q", str(package / "bundles/source.bundle"), str(imported), check=False)
    fail(clone.returncode == 0, f"bundle candidate clone failed: {clone.stderr.strip()}")
    if git(imported, "symbolic-ref", "--short", "-q", "HEAD", check=False) == "integration/homebrew":
        git(imported, "reset", "--hard", "-q", fixture.source)
    else:
        git(imported, "branch", "-f", "integration/homebrew", fixture.source)
    git(imported, "config", "user.name", "West Test")
    git(imported, "config", "user.email", "west-test@example.invalid")
    if module.exists() or module.is_symlink():
        shutil.rmtree(module)
    module.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(imported, module)
    if transaction_ref_setup is not None:
        transaction_ref_setup(candidate)
    transaction_refs_before = (
        snapshot_transaction_refs(candidate)
        if transaction_ref_setup is not None
        else None
    )
    lock = candidate / fixture.lock_rel
    mapping = candidate / fixture.mapping_rel
    profile = read_yaml(candidate / fixture.profile_rel)
    if not isinstance(profile, dict) or not isinstance(profile.get("patches"), list):
        raise lock_first.LockFirstError("production profile patches are not a list")
    patches = profile["patches"]
    report = preflight.inspect(module, lock)
    fail(report["overall_verdict"] == "VALID", f"production preflight rejected package: {report}")
    loaded = lock_first.load_mapping(mapping, "homebrew")
    plan = lock_first.plan("homebrew", patches, mapping)
    fail(loaded["schema_version"] == 3 and plan.batch["batch_id"] == "mutation-production-batch-v1", "production plan differs")
    export_report = exporter.export_profile("homebrew", plan, case_root / "export")
    fail(export_report["verdict"] == "VALID", "production export rejected package")
    fail(export_report["batch_id"] == plan.batch["batch_id"], "production export batch differs")
    transaction = case_root / "materialize"
    transaction.mkdir()
    materialized = case_root / "materialize-repo"
    run(case_root, "git", "clone", "-q", str(module), str(materialized))
    git(materialized, "reset", "--hard", "-q", fixture.base)
    materialized_result_ref = "refs/west/patch-stack-results/mutation"
    materialized_evidence = case_root / "materialize-evidence.json"
    materialize.materialize(materialized, lock, materialized_result_ref, materialized_evidence)
    fail(git(materialized, "rev-parse", materialized_result_ref) == fixture.source, "production materializer result ref differs")
    fail(not git(materialized, "for-each-ref", "refs/west/patch-stack-materialize/"), "materializer transaction refs remain")
    fail(not list(transaction.iterdir()), "materializer transaction directory is not empty")
    # Run the actual capture entry point on the reconstructed package
    # workspace.  Its outputs are disposable, while the package artifacts
    # below remain the inputs to the real compare/publication guard.
    captured = case_root / "captured"
    captured.mkdir()
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fixture.root / 'bin'}{os.pathsep}{old_path}"
    try:
        capture.capture(candidate, "homebrew", captured / "modules.json", captured / "manifest.json")
    finally:
        os.environ["PATH"] = old_path
    if candidate_manifest_mutation is not None:
        candidate_manifest_mutation(captured / "manifest.json")
    oracle_path = case_root / "oracle-replayed.json"
    immutable_oracle.apply(candidate, "homebrew", mapping, oracle_path)
    transaction_root = case_root / "compare-transactions"
    transaction_root.mkdir()
    compare_result = case_root / "compare-result.json"
    if result_path_setup is not None:
        result_path_setup(compare_result)
    result_before = (
        snapshot_result_path(compare_result)
        if result_path_setup is not None
        else None
    )
    try:
        hosted.compare_immutable_oracle(
            package / fixture.oracle_rel, captured / "modules.json", captured / "manifest.json",
            package / fixture.evidence_rel, mapping, candidate, transaction_root,
            compare_result, package / fixture.modules_rel, package / fixture.manifest_rel,
            candidate,
        )
    finally:
        if transaction_refs_before is not None:
            fail(
                snapshot_transaction_refs(candidate) == transaction_refs_before,
                "production compare mutated pre-existing transaction refs",
            )
        if result_before is not None:
            result_after = snapshot_result_path(compare_result)
            fail(
                result_after == result_before,
                "production compare mutated an existing result path",
            )
    fail(compare_result.exists(), "production compare did not publish its isolated result")
    fail(not list(transaction_root.iterdir()), "production compare left transaction outputs")


@dataclass(frozen=True)
class Mutation:
    name: str
    category: str
    apply: Callable[[Path], None]
    refresh: bool = True
    result_path_setup: Callable[[Path], None] | None = None
    candidate_manifest_mutation: Callable[[Path], None] | None = None
    transaction_ref_setup: Callable[[Path], None] | None = None


def mutate_yaml(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    value = read_yaml(path)
    fail(isinstance(value, dict), f"mutation input is not YAML object: {path}")
    change(value)
    write_yaml(path, value)


def mutate_json(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    value = read_json(path)
    fail(isinstance(value, dict), f"mutation input is not JSON object: {path}")
    change(value)
    write_json(path, value)


JsonPath = tuple[str | int, ...]


def json_leaf_paths(value: Any, prefix: JsonPath = ()) -> list[JsonPath]:
    """Enumerate every typed JSON leaf (including empty containers)."""
    if isinstance(value, dict):
        if not value:
            return [prefix]
        paths: list[JsonPath] = []
        for key in sorted(value):
            paths.extend(json_leaf_paths(value[key], prefix + (key,)))
        return paths
    if isinstance(value, list):
        if not value:
            return [prefix]
        paths = []
        for index, item in enumerate(value):
            paths.extend(json_leaf_paths(item, prefix + (index,)))
        return paths
    return [prefix]


def json_path_label(path: JsonPath) -> str:
    label = ""
    for part in path:
        label += f"[{part}]" if isinstance(part, int) else ("." if label else "") + part
    return label or "root"


def schema_inventory(fixture: Fixture) -> dict[str, dict[str, list[str]]]:
    json_artifacts = {
        "evidence": fixture.evidence_rel,
        "manifest": fixture.manifest_rel,
        "modules": fixture.modules_rel,
        "oracle": fixture.oracle_rel,
        "package-index": fixture.package_index_rel,
    }
    yaml_artifacts = {
        "lock": fixture.lock_rel,
        "mapping": fixture.mapping_rel,
        "composition": fixture.composition_rel,
        "profiles": "locks/patch-stack/lock-first-profiles-v1.yml",
        "profile": fixture.profile_rel,
        "west-base": "west.lock.yml",
        "generated": fixture.generated_rel,
    }
    return {
        "json": {
            name: [
                json_path_label(path)
                for path in json_leaf_paths(read_json(fixture.package / relative))
            ]
            for name, relative in json_artifacts.items()
        },
        "yaml": {
            name: [
                json_path_label(path)
                for path in json_leaf_paths(read_yaml(fixture.package / relative))
            ]
            for name, relative in yaml_artifacts.items()
        },
    }


def json_path_parent(value: Any, path: JsonPath) -> tuple[Any, str | int]:
    fail(bool(path), "JSON root mutation is not a leaf mutation")
    parent = value
    for part in path[:-1]:
        parent = parent[part]
    return parent, path[-1]


def json_path_value(value: Any, path: JsonPath) -> Any:
    current = value
    for part in path:
        current = current[part]
    return current


def json_bad_value(original: Any, kind: str) -> Any:
    if kind == "delete":
        return None
    if kind == "wrong-type":
        if isinstance(original, bool):
            return "false"
        if isinstance(original, (int, float)) and not isinstance(original, bool):
            return "0"
        if isinstance(original, str):
            return 0
        if isinstance(original, list):
            return {}
        if isinstance(original, dict):
            return []
        return {}
    if isinstance(original, bool):
        return not original
    if isinstance(original, int) and not isinstance(original, bool):
        return original + 1
    if isinstance(original, float):
        return original + 1.0
    if isinstance(original, str):
        return "forged-" + original[:32]
    if isinstance(original, list):
        return [*original, "forged"]
    if isinstance(original, dict):
        return {**original, "__forged__": True}
    return "forged"


def mutate_json_leaf(path: Path, leaf: JsonPath, kind: str) -> None:
    value = read_json(path)
    fail(isinstance(value, dict), f"mutation input is not JSON object: {path}")
    parent, key = json_path_parent(value, leaf)
    original = parent[key]
    if kind == "delete":
        if isinstance(parent, list):
            del parent[key]
        else:
            del parent[key]
    else:
        parent[key] = json_bad_value(original, kind)
    write_json(path, value)


def mutate_yaml_leaf(path: Path, leaf: JsonPath, kind: str) -> None:
    value = read_yaml(path)
    fail(isinstance(value, dict), f"mutation input is not YAML object: {path}")
    parent, key = json_path_parent(value, leaf)
    original = parent[key]
    if kind == "delete":
        del parent[key]
    else:
        parent[key] = json_bad_value(original, kind)
    write_yaml(path, value)


def flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    fail(bool(data), "bundle is empty")
    data[len(data) // 2] ^= 1
    path.write_bytes(data)


def mutate_package_index(package: Path, change: Callable[[dict[str, Any]], None]) -> None:
    mutate_json(package / "package-index.json", change)


def mutations(fixture: Fixture) -> list[Mutation]:
    lock = fixture.lock_rel
    mapping = fixture.mapping_rel
    composition = fixture.composition_rel
    profile = fixture.profile_rel
    patch = fixture.patch_rel
    generated = fixture.generated_rel
    evidence = fixture.evidence_rel
    manifest = fixture.manifest_rel
    oracle = fixture.oracle_rel
    index = fixture.package_index_rel
    cases: list[Mutation] = []
    lock_changes = {
        "schema": lambda v: v.__setitem__("schema_version", 1),
        "source": lambda v: v.__setitem__("source_commit", "0" * 40),
        "tree": lambda v: v.__setitem__("expected_tree", "f" * 40),
        "order": lambda v: v.__setitem__("ordered_commits", ["0" * 40]),
        "base": lambda v: v["upstream"].__setitem__("base_commit", "1" * 40),
        "base-ref": lambda v: v["mirror"].__setitem__("base_ref", "refs/tags/wrong"),
        "source-ref": lambda v: v["mirror"].__setitem__("source_ref", "refs/tags/wrong"),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in lock_changes.items():
        cases.append(Mutation(f"lock.{name}", "lock", lambda p, c=change: mutate_yaml(p / lock, c)))
    profile_changes = {
        "version": lambda v: v.__setitem__("version", 2),
        "patches": lambda v: v["patches"].append(dict(v["patches"][0])),
    }
    for name, change in profile_changes.items():
        cases.append(Mutation(f"profile.{name}", "profile", lambda p, c=change: mutate_yaml(p / profile, c)))
    mapping_changes = {
        "schema": lambda v: v.__setitem__("schema_version", 2),
        "count": lambda v: v.__setitem__("expected_count", 2),
        "batch": lambda v: v.__setitem__("batch_id", "forged"),
        "order": lambda v: v["series"].append(dict(v["series"][0])),
        "profile": lambda v: v.__setitem__("profile", "perf"),
        "composition": lambda v: v.__setitem__("composition", "missing.yml"),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in mapping_changes.items():
        cases.append(Mutation(f"mapping.{name}", "mapping", lambda p, c=change: mutate_yaml(p / mapping, c)))
    composition_changes = {
        "schema": lambda v: v.__setitem__("schema_version", 2),
        "mapping-digest": lambda v: v["mapping"].__setitem__("sha256", "0" * 64),
        "frozen-digest": lambda v: v["frozen_manifest"].__setitem__("sha256", "0" * 64),
        "boundary": lambda v: v["modules"][0]["series"][0].__setitem__("expected_applied_tree", "0" * 40),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in composition_changes.items():
        cases.append(Mutation(f"composition.{name}", "composition", lambda p, c=change: mutate_yaml(p / composition, c)))
    generated_changes = {
        "revision": lambda v: v["manifest"]["projects"][0].__setitem__("revision", "0" * 40),
        "project": lambda v: v["manifest"]["projects"].append({"path": "foreign", "revision": "1" * 40}),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in generated_changes.items():
        cases.append(Mutation(f"generated.{name}", "generated-lock", lambda p, c=change: mutate_yaml(p / generated, c)))
    evidence_changes = {
        "schema": lambda v: v.__setitem__("evidence_schema_version", 1),
        "verdict": lambda v: v.__setitem__("verdict", "ERROR"),
        "batch": lambda v: v.__setitem__("batch_id", "forged"),
        "count": lambda v: v.__setitem__("expected_count", 2),
        "module-order": lambda v: v.__setitem__("module_order", ["foreign"]),
        "series-order": lambda v: v.__setitem__("series_order", []),
        "entry-base": lambda v: v["series"][0].__setitem__("base", "0" * 40),
        "entry-tree": lambda v: v["series"][0].__setitem__("applied_tree", "0" * 40),
        "entry-unknown": lambda v: v["series"][0].__setitem__("unknown", True),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in evidence_changes.items():
        cases.append(Mutation(f"evidence.{name}", "evidence", lambda p, c=change: mutate_json(p / evidence, c)))
    manifest_changes = {
        "workspace": lambda v: v.__setitem__("workspace_commit", "0" * 40),
        "frozen": lambda v: v.__setitem__("frozen_manifest_sha256", "0" * 64),
        "generated": lambda v: v.__setitem__("generated_profile_locks", []),
        "nested": lambda v: v.__setitem__("validated_nested_children", []),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in manifest_changes.items():
        cases.append(Mutation(f"manifest.{name}", "manifest", lambda p, c=change: mutate_json(p / manifest, c)))
    cases.append(Mutation(
        "manifest.candidate-workspace", "manifest", lambda _p: None, refresh=False,
        candidate_manifest_mutation=lambda p: mutate_json(
            p, lambda value: value.__setitem__("workspace_commit", "0" * 40)
        ),
    ))
    # Generate the leaf-field matrix from the exact production artifacts, not
    # from a parallel hand-written schema.  Bound workspace inputs are still
    # protected by package bindings; these unbound evidence inputs additionally
    # traverse production compare with delete, wrong-type, and wrong-value
    # mutations for every discovered leaf.
    inventory = [
        ("evidence", evidence, "evidence"),
        ("manifest", manifest, "manifest"),
        ("modules", fixture.modules_rel, "modules"),
        ("oracle", oracle, "oracle"),
        ("package-index", index, "package-index"),
    ]
    for artifact, relative, category in inventory:
        for leaf in json_leaf_paths(read_json(fixture.package / relative)):
            label = json_path_label(leaf)
            for kind in ("delete", "wrong-type", "wrong-value"):
                cases.append(Mutation(
                    f"inventory.{artifact}.{label}.{kind}",
                    category,
                    lambda p, r=relative, l=leaf, k=kind: mutate_json_leaf(p / r, l, k),
                    refresh=category != "package-index",
                ))
    bound_inventory = [
        ("lock", lock),
        ("mapping", mapping),
        ("composition", composition),
        ("profiles", "locks/patch-stack/lock-first-profiles-v1.yml"),
        ("profile", profile),
        ("west-base", "west.lock.yml"),
        ("generated", generated),
    ]
    for artifact, relative in bound_inventory:
        for leaf in json_leaf_paths(read_yaml(fixture.package / relative)):
            label = json_path_label(leaf)
            for kind in ("delete", "wrong-type", "wrong-value"):
                cases.append(Mutation(
                    f"inventory-bound.{artifact}.{label}.{kind}",
                    "bound-inventory",
                    lambda p, r=relative, l=leaf, k=kind: mutate_yaml_leaf(p / r, l, k),
                ))
    oracle_changes = {
        "schema": lambda v: v.__setitem__("oracle_schema_version", 1),
        "mode": lambda v: v.__setitem__("mode", "legacy"),
        "profile": lambda v: v.__setitem__("profile", "perf"),
        "batch": lambda v: v["batches"][0].__setitem__("batch_id", "forged"),
        "module": lambda v: v["modules"][0].__setitem__("tree", "0" * 40),
        "clean-odb": lambda v: v["clean_odb"].__setitem__("alternates", 1),
        "cleanup": lambda v: v["cleanup"].__setitem__("root", "left"),
        "transaction-ref": lambda v: v["cleanup"].__setitem__("refs", "refs/west/patch-stack-results/forged"),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in oracle_changes.items():
        cases.append(Mutation(f"oracle.{name}", "oracle", lambda p, c=change: mutate_json(p / oracle, c)))
    modules_changes = {
        "module": lambda v: v["modules"][0].__setitem__("tree", "0" * 40),
        "unknown": lambda v: v.__setitem__("unknown", True),
    }
    for name, change in modules_changes.items():
        cases.append(Mutation(f"modules.{name}", "modules", lambda p, c=change: mutate_json(p / fixture.modules_rel, c)))
    cases.extend([
        Mutation("bundle.truncate", "bundle", lambda p: truncate_bundle(p / "bundles/source.bundle")),
        Mutation("bundle.flip", "bundle", lambda p: flip_byte(p / "bundles/source.bundle")),
        Mutation("bundle.repacked-missing", "bundle", lambda p, b=fixture.base: repack_bundle_missing(p / "bundles/source.bundle", b)),
        Mutation("bundle.repacked-wrong-ref", "bundle", lambda p, b=fixture.base, s=fixture.source: repack_bundle_wrong_ref(p / "bundles/source.bundle", b, s)),
        Mutation("patch.truncate", "patch", lambda p: truncate_bundle(p / patch)),
        Mutation("patch.flip", "patch", lambda p: flip_byte(p / patch)),
        Mutation("checksum.direct", "checksum", lambda p: corrupt_checksum(p / "SHA256SUMS"), refresh=False),
        Mutation("package-index.anchor", "package-index", lambda p: mutate_package_index(p, lambda v: v.__setitem__("workspace_commit", "0" * 40))),
        Mutation("package-index.binding", "package-index", lambda p: mutate_package_index(p, lambda v: v["bindings"][0].__setitem__("sha256", "0" * 64))),
        Mutation("package-index.unknown", "package-index", lambda p: mutate_package_index(p, lambda v: v.__setitem__("unknown", True))),
        Mutation("package.missing", "package-shape", lambda p: (p / lock).unlink(), refresh=False),
        Mutation("package.extra", "package-shape", lambda p: (p / "unauthorized-output").write_text("foreign\n"), refresh=False),
        Mutation("package.symlink", "package-shape", symlink_result, refresh=False),
        Mutation("composed.lock-manifest", "composed", lambda p: composed_lock_manifest(p, lock, manifest)),
        Mutation("composed.mapping-oracle", "composed", lambda p: composed_mapping_oracle(p, mapping, oracle)),
        Mutation("result-path.file", "result-path", lambda _p: None, refresh=False, result_path_setup=result_path_file),
        Mutation("result-path.symlink", "result-path", lambda _p: None, refresh=False, result_path_setup=result_path_symlink),
        Mutation("result-path.directory", "result-path", lambda _p: None, refresh=False, result_path_setup=result_path_directory),
        Mutation(
            "transaction-ref.materialize",
            "transaction-ref",
            lambda _p: None,
            refresh=False,
            transaction_ref_setup=stale_transaction_ref("patch-stack-materialize"),
        ),
        Mutation(
            "transaction-ref.results",
            "transaction-ref",
            lambda _p: None,
            refresh=False,
            transaction_ref_setup=stale_transaction_ref("patch-stack-results"),
        ),
        Mutation(
            "transaction-ref.lock-first",
            "transaction-ref",
            lambda _p: None,
            refresh=False,
            transaction_ref_setup=stale_transaction_ref("patch-stack-lock-first"),
        ),
    ])
    return cases


def truncate_bundle(package: Path) -> None:
    path = package
    path.write_bytes(path.read_bytes()[:-1])


def repack_bundle_missing(package: Path, base: str) -> None:
    temp = Path(tempfile.mkdtemp(prefix="bundle-repack-missing-", dir=str(package.parent)))
    try:
        repo = temp / "repo"
        clone = run(temp, "git", "clone", "-q", str(package), str(repo), check=False)
        fail(clone.returncode == 0, f"bundle repack clone failed: {clone.stderr.strip()}")
        output = temp / "source.bundle"
        run(
            repo,
            "git",
            "bundle",
            "create",
            str(output),
            f"refs/tags/patch-stack/v1/bases/{base}",
        )
        shutil.copy2(output, package)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def repack_bundle_wrong_ref(package: Path, base: str, source: str) -> None:
    temp = Path(tempfile.mkdtemp(prefix="bundle-repack-ref-", dir=str(package.parent)))
    try:
        repo = temp / "repo"
        clone = run(temp, "git", "clone", "-q", str(package), str(repo), check=False)
        fail(clone.returncode == 0, f"bundle repack clone failed: {clone.stderr.strip()}")
        run(repo, "git", "update-ref", f"refs/tags/patch-stack/v1/sources/{source}", base)
        output = temp / "source.bundle"
        run(repo, "git", "bundle", "create", str(output), "--all")
        shutil.copy2(output, package)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def corrupt_checksum(package: Path) -> None:
    lines = package.read_text().splitlines(keepends=True)
    fail(bool(lines), "checksum file is empty")
    digest, separator, relative = lines[0].partition("  ")
    lines[0] = ("0" if digest[0] != "0" else "1") + digest[1:] + separator + relative
    package.write_text("".join(lines))


def symlink_result(package: Path) -> None:
    path = package / "acceptance-result.json"
    path.unlink()
    path.symlink_to(package / "evidence/lock-first-evidence.json")


def result_path_file(path: Path) -> None:
    path.write_text("stale result\n")


def result_path_symlink(path: Path) -> None:
    path.symlink_to("missing-result-target")


def result_path_directory(path: Path) -> None:
    path.mkdir()


def stale_transaction_ref(namespace: str) -> Callable[[Path], None]:
    def setup(workspace: Path) -> None:
        module = workspace / "darling"
        commit = git(module, "rev-parse", "HEAD")
        git(module, "update-ref", f"refs/west/{namespace}/stale", commit)

    return setup


def snapshot_transaction_refs(workspace: Path) -> dict[str, list[str]]:
    module = workspace / "darling"
    result: dict[str, list[str]] = {}
    for namespace in (
        "patch-stack-materialize",
        "patch-stack-results",
        "patch-stack-lock-first",
    ):
        raw = git(
            module,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            f"refs/west/{namespace}/",
        )
        result[namespace] = raw.splitlines()
    return result


def snapshot_result_path(path: Path) -> dict[str, Any]:
    """Capture bounded identity/content state without following symlinks."""
    entries = 0
    payload_bytes = 0

    def one(current: Path, relative: str) -> dict[str, Any]:
        nonlocal entries, payload_bytes
        entries += 1
        fail(entries <= 4096, "result-path snapshot entry budget exceeded")
        info = current.lstat()
        state: dict[str, Any] = {
            "relative": relative,
            "identity": {
                "dev": info.st_dev,
                "ino": info.st_ino,
                "mode": info.st_mode,
                "nlink": info.st_nlink,
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            },
        }
        if stat.S_ISLNK(info.st_mode):
            state.update({"kind": "symlink", "target": os.readlink(current)})
        elif stat.S_ISREG(info.st_mode):
            payload_bytes += info.st_size
            fail(payload_bytes <= 8 * 1024 * 1024, "result-path snapshot byte budget exceeded")
            state.update({"kind": "file", "sha256": sha256(current)})
        elif stat.S_ISDIR(info.st_mode):
            children = []
            with os.scandir(current) as iterator:
                for child in sorted(iterator, key=lambda item: item.name):
                    children.append(one(Path(child.path), f"{relative}/{child.name}"))
            state.update({"kind": "directory", "children": children})
        else:
            state["kind"] = "other"
        return state

    if not path.exists() and not path.is_symlink():
        return {"exists": False}
    return {"exists": True, "root": one(path, ".")}


def composed_lock_manifest(package: Path, lock: str, manifest: str) -> None:
    mutate_yaml(package / lock, lambda v: v.__setitem__("expected_tree", "0" * 40))
    mutate_json(package / manifest, lambda v: v.__setitem__("workspace_commit", "0" * 40))


def composed_mapping_oracle(package: Path, mapping: str, oracle: str) -> None:
    mutate_yaml(package / mapping, lambda v: v.__setitem__("batch_id", "forged"))
    mutate_json(package / oracle, lambda v: v["batches"][0].__setitem__("batch_id", "forged"))


def run_materializer_recovery(root: Path) -> str:
    nested = root / "materializer-contract-tmp"
    nested.mkdir()
    env = dict(os.environ)
    env["TMPDIR"] = str(nested)
    env["PATCH_STACK_MATERIALIZE_CONTRACT_SKIP_WEST_SUBPROCESS"] = "1"
    script = ROOT / "tests/west_test_contracts/patch_stack_materialize_contract.py"
    result = subprocess.run([sys.executable, "-B", str(script)], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    fail(result.returncode == 0, f"materializer recovery contract failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def run_matrix(root: Path) -> dict[str, Any]:
    fixture = build_fixture(root)
    baseline_workspace = {
        relative: sha256(fixture.workspace / relative)
        for relative in fixture.bindings
    }
    verify_package_index(fixture, fixture.package)
    # Explicitly exercise the composed trusted-root attack: a mutable copy
    # may change both a lock and its Git tree, but the recorded anchor remains
    # the only accepted workspace identity.
    swapped_workspace = root / "trusted-root-swap"
    shutil.copytree(fixture.workspace, swapped_workspace, symlinks=True)
    mutate_yaml(swapped_workspace / fixture.lock_rel, lambda value: value.__setitem__("project", {"name": "forged", "path": "."}))
    run(swapped_workspace, "git", "add", fixture.lock_rel)
    git(swapped_workspace, "commit", "-qm", "forged trusted workspace")
    try:
        verify_trusted_workspace(fixture, swapped_workspace)
    except MutationContractError:
        pass
    else:
        raise MutationContractError("composed trusted-root mutation was accepted")
    # Repeat the attack with package lock, binding digest, and checksums all
    # changed together.  Only the recorded immutable Git commit/tree anchor
    # may authorize the workspace; recomputing neighboring evidence is not
    # sufficient.
    swapped_package = root / "trusted-package-swap"
    shutil.copytree(fixture.package, swapped_package, symlinks=True)
    mutate_yaml(swapped_package / fixture.lock_rel, lambda value: value.__setitem__("project", {"name": "forged", "path": "."}))
    mutate_package_index(swapped_package, lambda value: value["bindings"][0].__setitem__("sha256", sha256(swapped_workspace / fixture.lock_rel)))
    refresh_package_integrity(swapped_package)
    try:
        verify_package_index(replace(fixture, workspace=swapped_workspace), swapped_package)
    except MutationContractError:
        pass
    else:
        raise MutationContractError("composed package/trusted-root mutation was accepted")
    shutil.rmtree(swapped_package)
    shutil.rmtree(swapped_workspace)
    # One unmutated package must traverse all production consumers before the
    # mutation cases are allowed to run.
    production_validate(fixture, fixture.package, root / "baseline")
    cases = mutations(fixture)
    category_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {"package-integrity": 0, "production-consumer": 0}
    production_attempted = 0
    production_rejected = 0
    binding_categories = {
        "lock", "mapping", "composition", "generated-lock", "profile", "patch",
        "bound-inventory",
    }
    rejected = 0
    for case in cases:
        case_root = root / "cases" / case.name.replace("/", "-")
        mutated = case_root / "package"
        case_root.mkdir(parents=True)
        try:
            shutil.copytree(fixture.package, mutated, symlinks=True)
            case.apply(mutated)
            if case.refresh:
                refresh_package_integrity(mutated)
            package_failure = ""
            try:
                verify_package_index(fixture, mutated)
            except (MutationContractError, ValueError, OSError) as error:
                package_failure = str(error)
            production_failure = ""
            should_probe_consumer = case.category in binding_categories or not package_failure
            if should_probe_consumer:
                production_attempted += 1
                try:
                    production_validate(
                        fixture,
                        mutated,
                        case_root,
                        case.apply if case.category in binding_categories else None,
                        case.result_path_setup,
                        case.candidate_manifest_mutation,
                        case.transaction_ref_setup,
                    )
                except (MutationContractError, capture.AcceptanceError, lock_first.LockFirstError, immutable_oracle.OracleError, materialize.MaterializeError, hosted.AcceptanceError, preflight.GitToolError, exporter.ExportError, ValueError, OSError, yaml.YAMLError) as error:
                    production_failure = str(error)
                else:
                    if (case_root / "compare-result.json").exists():
                        raise MutationContractError(f"{case.name}: production sink published before rejection")
                    raise MutationContractError(f"mutation unexpectedly accepted by production consumers: {case.name}")
                if production_failure:
                    production_rejected += 1
                    phase_counts["production-consumer"] += 1
            if package_failure:
                phase_counts["package-integrity"] += 1
            if not package_failure and not production_failure:
                raise MutationContractError(f"mutation had no rejection phase: {case.name}")
            rejected += 1
            category_counts[case.category] = category_counts.get(case.category, 0) + 1
            if case.result_path_setup is None and (case_root / "compare-result.json").exists():
                raise MutationContractError(f"{case.name}: production sink published before rejection: {package_failure or production_failure}")
        finally:
            shutil.rmtree(case_root, ignore_errors=False)
        for relative, digest in baseline_workspace.items():
            fail(sha256(fixture.workspace / relative) == digest, f"workspace binding changed during {case.name}")
    fail(rejected == len(cases), f"only {rejected}/{len(cases)} production mutations rejected")
    recovery = run_materializer_recovery(root)
    inventory = schema_inventory(fixture)
    result = {
        "schema_version": 1,
        "marker": "PATCH_STACK_MUTATION_MATRIX_VALID",
        "single_and_composed": {"total": len(cases), "rejected": rejected, "accepted": 0},
        "categories": category_counts,
        "rejection_phases": phase_counts,
        "production_consumer": {
            "attempted": production_attempted,
            "rejected": production_rejected,
            "binding_categories": sorted(binding_categories),
            "all_binding_cases_probed": True,
        },
        "mutation_names": [case.name for case in cases],
        "schema_inventory": inventory,
        "inventory_mutations": sum(
            len(paths)
            for artifact_group in inventory.values()
            for paths in artifact_group.values()
        ) * 3,
        "production_consumers": [
            "patch_stack_preflight.inspect",
            "patch_stack_lock_first.load_mapping",
            "patch_stack_lock_first.plan",
            "patch_stack_export.export_profile",
            "patch_stack_materialize.materialize",
            "patch_stack_acceptance.capture",
            "patch_stack_immutable_oracle.apply",
            "patch_stack_lock_first_acceptance.compare_immutable_oracle",
        ],
        "materializer_recovery": recovery,
        "trusted_workspace_anchor": {
            "commit": fixture.workspace_commit,
            "tree": fixture.workspace_tree,
            "composed_swap": "REJECTED",
        },
        "temporary_namespace": "owned-root-only",
    }
    write_json(root / "mutation-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.root is None:
        with tempfile.TemporaryDirectory(prefix="patch-stack-production-mutation-") as temp:
            result = run_matrix(Path(temp))
    else:
        args.root.mkdir(parents=True, exist_ok=True)
        result = run_matrix(args.root)
    if args.result is not None:
        write_json(args.result, result)
    print(
        "PATCH_STACK_MUTATION_MATRIX_VALID "
        f"cases={result['single_and_composed']['total']} "
        f"rejected={result['single_and_composed']['rejected']} "
        f"categories={len(result['categories'])}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MutationContractError, OSError, subprocess.SubprocessError) as error:
        print(f"PATCH_STACK_MUTATION_MATRIX_INVALID: {error}", file=sys.stderr)
        raise SystemExit(1)
