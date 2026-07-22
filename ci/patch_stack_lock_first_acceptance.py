#!/usr/bin/env python3
"""Typed, fail-closed comparison for lock-first batch acceptance.

This intentionally does not replace ``patch_stack_shadow_acceptance.py``:
that helper remains the single-series legacy-shadow oracle.  This module
validates the distinct Batch 1 aggregate evidence against every declared
schema-v2 lock and against the actual lock-first Git worktree.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from patch_stack_shadow_acceptance import AcceptanceError, git

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_materialize


OID = re.compile(r"^[0-9a-f]{40}$")
ENTRY_FIELDS = {"patch", "base", "source", "canonical_tree", "applied_commit", "applied_tree", "verdict"}
MAPPING_FIELDS = {"schema_version", "profile", "batch_id", "expected_count", "series"}
SERIES_FIELDS = {"profile", "module", "patch", "lock"}


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"invalid JSON evidence {path}: {error}") from error
    fail(isinstance(value, dict), f"{path}: evidence is not an object")
    return value


def oid(value: object, label: str) -> str:
    fail(isinstance(value, str) and OID.fullmatch(value), f"{label}: not a lowercase SHA-1")
    return value


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        fail(not current.is_symlink(), f"{label}: symlink component is forbidden: {current}")


def contained(root: Path, relative: object, label: str) -> Path:
    fail(isinstance(relative, str) and relative and not Path(relative).is_absolute(), f"{label}: not a relative path")
    relative_path = Path(relative)
    fail(".." not in relative_path.parts, f"{label}: path escapes workspace")
    _reject_symlink_components(root, label)
    raw = root.absolute() / relative_path
    _reject_symlink_components(raw, label)
    candidate = raw.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise AcceptanceError(f"{label}: path escapes workspace") from error
    return candidate


def load_batch(mapping_path: Path, available_modules: set[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(mapping_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise AcceptanceError(f"invalid lock-first mapping {mapping_path}: {error}") from error
    fail(isinstance(data, dict) and set(data) == MAPPING_FIELDS and data.get("schema_version") == 2, "lock-first mapping must use exact schema_version 2")
    profile, batch_id, expected_count, series = data.get("profile"), data.get("batch_id"), data.get("expected_count"), data.get("series")
    fail(isinstance(profile, str) and profile == "homebrew", "lock-first mapping profile must be homebrew")
    fail(isinstance(batch_id, str) and batch_id, "lock-first mapping batch_id is invalid")
    fail(isinstance(expected_count, int) and not isinstance(expected_count, bool) and expected_count > 0, "lock-first mapping expected_count is invalid")
    fail(isinstance(series, list) and expected_count == len(series), "lock-first mapping expected_count differs from series length")
    root = mapping_path.parent.resolve()
    patches: set[str] = set()
    locks: set[str] = set()
    batch: list[dict[str, str]] = []
    for index, entry in enumerate(series):
        fail(isinstance(entry, dict) and set(entry) == SERIES_FIELDS, f"mapping entry {index}: invalid fields")
        fail(all(isinstance(entry[field], str) and entry[field] for field in SERIES_FIELDS), f"mapping entry {index}: invalid value")
        fail(entry["profile"] == profile, f"mapping entry {index}: profile must match mapping")
        fail(entry["module"] in available_modules, f"mapping entry {index}: module is not present in module maps")
        fail(entry["patch"] not in patches, f"mapping entry {index}: duplicate patch")
        patches.add(entry["patch"])
        relative = Path(entry["lock"])
        fail(not relative.is_absolute() and ".." not in relative.parts, f"mapping entry {index}: lock escapes mapping root")
        path = contained(root, entry["lock"], f"mapping entry {index} lock")
        fail(path.is_file(), f"mapping entry {index}: lock is not a regular contained file")
        fail(str(path) not in locks, f"mapping entry {index}: duplicate lock")
        locks.add(str(path))
        batch.append({**entry, "lock_path": str(path)})
    return {"profile": profile, "batch_id": batch_id, "expected_count": expected_count, "series": batch}


def lock_values(entry: dict[str, str]) -> dict[str, str]:
    path = Path(entry["lock_path"])
    try:
        lock = patch_stack_materialize.load_lock(path)
    except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
        raise AcceptanceError(f"{entry['patch']}: invalid schema-v2 lock: {error}") from error
    fail(isinstance(lock, dict) and lock.get("schema_version") == 2, f"{entry['patch']}: lock is not schema-v2")
    upstream = lock.get("upstream")
    fail(isinstance(upstream, dict), f"{entry['patch']}: missing upstream lock data")
    return {
        "base": oid(upstream.get("base_commit"), f"{entry['patch']} base"),
        "source": oid(lock.get("source_commit"), f"{entry['patch']} source"),
        "canonical_tree": oid(lock.get("expected_tree"), f"{entry['patch']} expected tree"),
    }


def module_rows(value: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    rows = value.get("modules")
    fail(isinstance(rows, list) and rows, f"{label}: missing module rows")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        fail(isinstance(row, dict) and set(row) == {"module", "west_name", "path", "integration_oid", "tree", "status"}, f"{label}: invalid module row")
        module = row.get("module")
        fail(isinstance(module, str) and module and module not in result, f"{label}: duplicate or invalid module")
        fail(isinstance(row.get("path"), str) and row["path"], f"{label}: invalid module path")
        oid(row.get("integration_oid"), f"{label} {module} integration OID")
        oid(row.get("tree"), f"{label} {module} integration tree")
        fail(row.get("status") == "", f"{label} {module}: dirty module")
        result[module] = row
    return result


def verify_manifest(value: dict[str, Any], label: str) -> None:
    generated = value.get("generated_profile_lock")
    fail(isinstance(generated, dict), f"{label}: generated lock metadata missing")
    fail(isinstance(generated.get("size"), int) and 0 <= generated["size"] <= 1_000_000, f"{label}: generated lock size invalid")
    oid_hash = generated.get("sha256")
    fail(isinstance(oid_hash, str) and re.fullmatch(r"[0-9a-f]{64}", oid_hash), f"{label}: generated lock hash invalid")
    frozen = value.get("frozen_manifest_sha256")
    fail(isinstance(frozen, str) and re.fullmatch(r"[0-9a-f]{64}", frozen), f"{label}: frozen manifest hash invalid")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise AcceptanceError(f"{repo}: git merge-base --is-ancestor failed: {result.stderr.strip()}")


def verify_actual_maps(workspace: Path, rows: dict[str, dict[str, Any]], profile: str, label: str) -> None:
    for module, row in rows.items():
        repo = contained(workspace, row["path"], f"{label} {module}")
        fail(repo.is_dir() and not repo.is_symlink(), f"{label} {module}: workspace repository missing")
        ref = f"refs/heads/integration/{profile}"
        integration = git(repo, "rev-parse", ref)
        fail(integration == row["integration_oid"], f"{label} {module}: integration ref differs from map")
        fail(git(repo, "rev-parse", f"{ref}^{{tree}}") == row["tree"], f"{label} {module}: integration tree differs from map")


def assert_no_transaction_state(workspace: Path, rows: dict[str, dict[str, Any]], transaction_root: Path, label: str) -> None:
    for module, row in rows.items():
        repo = contained(workspace, row["path"], f"{label} {module}")
        refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/west/patch-stack-materialize/", "refs/west/patch-stack-results/", "refs/west/patch-stack-lock-first/")
        fail(not refs, f"{label} {module}: transaction refs remain: {refs}")
        worktrees = git(repo, "worktree", "list", "--porcelain")
        fail("west-lock-materialize-" not in worktrees and "west-patch-lock-first-" not in worktrees, f"{label} {module}: disposable worktree remains")
    leftovers = [path.name for pattern in ("west-lock-materialize-*", "west-patch-lock-first-*") for path in transaction_root.glob(pattern)]
    fail(not leftovers, f"{label}: disposable roots remain: {sorted(leftovers)}")


def compare_lock_first(
    control_path: Path,
    lock_first_path: Path,
    control_manifest_path: Path,
    lock_first_manifest_path: Path,
    evidence_path: Path,
    mapping_path: Path,
    control_workspace: Path,
    lock_first_workspace: Path,
    transaction_root: Path,
    result_path: Path,
) -> None:
    fail(not result_path.exists() and not result_path.is_symlink(), "compare result path already exists")
    control, lock_first = load_json(control_path), load_json(lock_first_path)
    control_manifest, lock_first_manifest = load_json(control_manifest_path), load_json(lock_first_manifest_path)
    fail(control == lock_first, "control and lock-first module maps differ")
    fail(control_manifest == lock_first_manifest, "control and lock-first manifests differ")
    profile = control.get("profile")
    fail(profile == "homebrew" and lock_first.get("profile") == profile, "module maps have an invalid profile")
    verify_manifest(control_manifest, "control manifest")
    rows = module_rows(control, "control module map")
    verify_actual_maps(control_workspace, rows, profile, "control")
    verify_actual_maps(lock_first_workspace, rows, profile, "lock-first")

    batch_metadata = load_batch(mapping_path, set(rows))
    batch = batch_metadata["series"]
    evidence = load_json(evidence_path)
    fail(set(evidence) == {"verdict", "batch_id", "expected_count", "patches", "series"} and evidence.get("verdict") == "VALID", "lock-first evidence has invalid top-level fields")
    series = evidence.get("series")
    fail(evidence.get("batch_id") == batch_metadata["batch_id"], "lock-first evidence batch_id differs from mapping")
    fail(evidence.get("expected_count") == batch_metadata["expected_count"], "lock-first evidence expected_count differs from mapping")
    fail(isinstance(series, list) and len(series) == batch_metadata["expected_count"], "lock-first evidence does not contain expected_count entries")
    expected_patches = [entry["patch"] for entry in batch]
    fail(evidence.get("patches") == expected_patches, "lock-first evidence patches differ from mapping")
    observed_patches = []
    previous: tuple[Path, str] | None = None
    for mapping, observed in zip(batch, series, strict=True):
        fail(isinstance(observed, dict) and set(observed) == ENTRY_FIELDS, f"{mapping['patch']}: evidence fields are invalid")
        fail(observed.get("verdict") == "VALID", f"{mapping['patch']}: evidence verdict is not VALID")
        patch = observed.get("patch")
        fail(isinstance(patch, str), f"{mapping['patch']}: evidence patch invalid")
        observed_patches.append(patch)
        expected = lock_values(mapping)
        for field, expected_value in expected.items():
            fail(observed.get(field) == expected_value, f"{mapping['patch']}: {field} differs from schema-v2 lock")
        applied_commit = oid(observed.get("applied_commit"), f"{mapping['patch']} applied commit")
        applied_tree = oid(observed.get("applied_tree"), f"{mapping['patch']} applied tree")
        row = rows.get(mapping["module"])
        fail(row is not None, f"{mapping['patch']}: module missing from module map")
        repo = contained(lock_first_workspace, row["path"], f"{mapping['patch']} repository")
        fail(git(repo, "cat-file", "-e", f"{applied_commit}^{{commit}}") == "", f"{mapping['patch']}: applied commit missing")
        fail(git(repo, "rev-parse", f"{applied_commit}^{{tree}}") == applied_tree, f"{mapping['patch']}: applied tree is not the commit tree")
        integration = row["integration_oid"]
        fail(is_ancestor(repo, applied_commit, integration), f"{mapping['patch']}: applied commit is not an ancestor of integration")
        if previous is not None:
            previous_repo, previous_commit = previous
            fail(previous_repo == repo and is_ancestor(repo, previous_commit, applied_commit), f"{mapping['patch']}: applied commits are not in ancestry order")
        previous = (repo, applied_commit)
    fail(observed_patches == expected_patches, "lock-first evidence patches do not exactly match the ordered batch")
    fail(len(set(observed_patches)) == batch_metadata["expected_count"], "lock-first evidence contains duplicate patches")
    assert_no_transaction_state(control_workspace, rows, transaction_root, "control")
    assert_no_transaction_state(lock_first_workspace, rows, transaction_root, "lock-first")
    result_path.write_text(json.dumps({"verdict": "VALID", "module_count": len(rows), "lock_first_evidence": evidence_path.name}, sort_keys=True, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    compare = sub.add_parser("compare-lock-first")
    for name in ("control", "lock-first", "control-manifest", "lock-first-manifest", "evidence", "mapping", "control-workspace", "lock-first-workspace", "transaction-root", "result"):
        compare.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        compare_lock_first(args.control, args.lock_first, args.control_manifest, args.lock_first_manifest, args.evidence, args.mapping, args.control_workspace, args.lock_first_workspace, args.transaction_root, args.result)
    except AcceptanceError as error:
        print(f"patch-stack lock-first acceptance: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
