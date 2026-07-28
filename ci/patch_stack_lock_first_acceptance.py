#!/usr/bin/env python3
"""Typed, fail-closed comparison for lock-first batch acceptance.

The generic capture/staging helper remains in ``patch_stack_acceptance.py``.
This module validates versioned aggregate evidence against every declared
schema-v2 lock and against the actual lock-first Git worktree.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from patch_stack_acceptance import AcceptanceError, git

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_materialize
import patch_stack_profile_composition


OID = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_SCHEMA_VERSION = 2
ENTRY_FIELDS = {"module", "patch", "base", "source", "canonical_tree", "applied_commit", "applied_tree", "verdict"}
MAPPING_V2_FIELDS = {"schema_version", "profile", "batch_id", "expected_count", "series"}
MAPPING_V3_FIELDS = {*MAPPING_V2_FIELDS, "composition"}
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


def write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AcceptanceError(f"acceptance result write failed: {error}") from error


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
    fields = set(data) if isinstance(data, dict) else set()
    fail(isinstance(data, dict) and ((data.get("schema_version") == 2 and fields == MAPPING_V2_FIELDS) or (data.get("schema_version") == 3 and fields == MAPPING_V3_FIELDS)), "lock-first mapping must use exact schema_version 2 or 3")
    profile, batch_id, expected_count, series = data.get("profile"), data.get("batch_id"), data.get("expected_count"), data.get("series")
    fail(isinstance(profile, str) and profile, "lock-first mapping profile is invalid")
    fail(isinstance(batch_id, str) and batch_id, "lock-first mapping batch_id is invalid")
    fail(isinstance(expected_count, int) and not isinstance(expected_count, bool) and expected_count > 0, "lock-first mapping expected_count is invalid")
    fail(isinstance(series, list) and expected_count == len(series), "lock-first mapping expected_count differs from series length")
    root = mapping_path.parent.resolve()
    series_keys: set[tuple[str, str]] = set()
    locks: set[str] = set()
    batch: list[dict[str, str]] = []
    for index, entry in enumerate(series):
        fail(isinstance(entry, dict) and set(entry) == SERIES_FIELDS, f"mapping entry {index}: invalid fields")
        fail(all(isinstance(entry[field], str) and entry[field] for field in SERIES_FIELDS), f"mapping entry {index}: invalid value")
        fail(entry["profile"] == profile, f"mapping entry {index}: profile must match mapping")
        fail(entry["module"] in available_modules, f"mapping entry {index}: module is not present in module maps")
        key = (entry["module"], entry["patch"])
        fail(key not in series_keys, f"mapping entry {index}: duplicate module+patch")
        series_keys.add(key)
        relative = Path(entry["lock"])
        fail(not relative.is_absolute() and ".." not in relative.parts, f"mapping entry {index}: lock escapes mapping root")
        path = contained(root, entry["lock"], f"mapping entry {index} lock")
        fail(path.is_file(), f"mapping entry {index}: lock is not a regular contained file")
        fail(str(path) not in locks, f"mapping entry {index}: duplicate lock")
        locks.add(str(path))
        batch.append({**entry, "lock_path": str(path)})
    module_order = list(dict.fromkeys(entry["module"] for entry in batch))
    result = {"profile": profile, "batch_id": batch_id, "expected_count": expected_count,
              "module_order": module_order, "series": batch}
    if data["schema_version"] == 3:
        composition_name = data.get("composition")
        fail(isinstance(composition_name, str) and composition_name and not Path(composition_name).is_absolute() and ".." not in Path(composition_name).parts and len(Path(composition_name).parts) == 1, "lock-first composition path is invalid")
        composition_path = contained(root, composition_name, "lock-first composition")
        fail(composition_path.is_file(), "lock-first composition is not a regular contained file")
        try:
            composition = patch_stack_profile_composition.bind(
                composition_path, mapping_path=mapping_path, mapping=data, entries=batch,
            )
        except patch_stack_profile_composition.ProfileCompositionError as error:
            raise AcceptanceError(f"invalid profile composition: {error}") from error
        modules = []
        for module in module_order:
            modules.append({
                "module": module,
                "starting": composition["starts"][module],
                "series": [
                    {"patch": entry["patch"], "tree": composition["boundaries"][(module, entry["patch"])]}
                    for entry in batch if entry["module"] == module
                ],
                "final_tree": composition["finals"][module],
                "integration_final_tree": composition["integration_finals"][module],
            })
        result["profile_composition"] = {
            "schema_version": composition["schema_version"], "path": composition["path"],
            "prerequisites": composition["prerequisites"], "frozen_manifest": composition["frozen_manifest"],
            "modules": modules,
        }
    return result


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


def verify_profile_composition_evidence(evidence: dict[str, Any], batch_metadata: dict[str, Any]) -> None:
    """Require the deterministic boundary manifest when mapping schema v3 declares it."""
    expected = batch_metadata.get("profile_composition")
    fields = {"evidence_schema_version", "verdict", "batch_id", "expected_count", "module_order", "series_order", "series"}
    if expected is None:
        fail(set(evidence) == fields, "lock-first evidence v2 has invalid top-level fields")
        return
    fields.add("profile_composition")
    fail(set(evidence) == fields, "lock-first evidence composition manifest fields are invalid")
    fail(evidence.get("profile_composition") == expected, "lock-first evidence profile composition differs from typed lock")


def module_rows(value: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    rows = value.get("modules")
    fail(isinstance(rows, list) and rows, f"{label}: missing module rows")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        fail(isinstance(row, dict) and set(row) == {"module", "west_name", "path", "integration_profile", "integration_oid", "tree", "status"}, f"{label}: invalid module row")
        module = row.get("module")
        fail(isinstance(module, str) and module and module not in result, f"{label}: duplicate or invalid module")
        fail(isinstance(row.get("path"), str) and row["path"], f"{label}: invalid module path")
        fail(
            isinstance(row.get("integration_profile"), str)
            and row["integration_profile"],
            f"{label} {module}: invalid integration profile",
        )
        oid(row.get("integration_oid"), f"{label} {module} integration OID")
        oid(row.get("tree"), f"{label} {module} integration tree")
        fail(row.get("status") == "", f"{label} {module}: dirty module")
        result[module] = row
    return result


def verify_manifest(value: dict[str, Any], label: str) -> None:
    fail(
        set(value)
        == {
            "workspace_commit",
            "frozen_manifest_sha256",
            "generated_profile_locks",
            "validated_nested_children",
        },
        f"{label}: manifest fields are invalid",
    )
    oid(value.get("workspace_commit"), f"{label} workspace commit")
    frozen = value.get("frozen_manifest_sha256")
    fail(
        isinstance(frozen, str) and re.fullmatch(r"[0-9a-f]{64}", frozen),
        f"{label}: frozen manifest hash invalid",
    )
    fail(
        isinstance(value.get("validated_nested_children"), dict),
        f"{label}: validated nested children are invalid",
    )
    generated = value.get("generated_profile_locks")
    fail(
        isinstance(generated, list) and generated,
        f"{label}: generated lock metadata missing",
    )
    seen: set[tuple[str, str]] = set()
    for row in generated:
        fail(
            isinstance(row, dict)
            and set(row) == {"profile", "path", "size", "sha256"},
            f"{label}: generated lock row is invalid",
        )
        profile, path = row.get("profile"), row.get("path")
        fail(
            isinstance(profile, str)
            and profile
            and isinstance(path, str)
            and path == f"patches/{profile}/west.lock.yml"
            and (profile, path) not in seen,
            f"{label}: generated lock identity is invalid",
        )
        seen.add((profile, path))
        fail(
            isinstance(row.get("size"), int)
            and not isinstance(row["size"], bool)
            and 0 < row["size"] <= 1_000_000,
            f"{label}: generated lock size invalid",
        )
        fail(
            isinstance(row.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
            f"{label}: generated lock hash invalid",
        )


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
        ref = f"refs/heads/integration/{row['integration_profile']}"
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


def verify_candidate_evidence(
    evidence_path: Path,
    batch_metadata: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    workspace: Path,
) -> None:
    """Prove immutable per-series evidence against a real candidate workspace."""
    batch = batch_metadata["series"]
    evidence = load_json(evidence_path)
    observed_version = evidence.get("evidence_schema_version")
    fail(observed_version == EVIDENCE_SCHEMA_VERSION, f"lock-first evidence schema version mismatch: expected {EVIDENCE_SCHEMA_VERSION}, got {observed_version!r}")
    verify_profile_composition_evidence(evidence, batch_metadata)
    fail(evidence.get("verdict") == "VALID", "lock-first evidence verdict is not VALID")
    series = evidence.get("series")
    fail(evidence.get("batch_id") == batch_metadata["batch_id"], "lock-first evidence batch_id differs from mapping")
    fail(evidence.get("expected_count") == batch_metadata["expected_count"], "lock-first evidence expected_count differs from mapping")
    fail(isinstance(series, list) and len(series) == batch_metadata["expected_count"], "lock-first evidence does not contain expected_count entries")
    expected_series = [{"module": entry["module"], "patch": entry["patch"]} for entry in batch]
    fail(evidence.get("module_order") == batch_metadata["module_order"], "lock-first evidence module order differs from mapping")
    fail(evidence.get("series_order") == expected_series, "lock-first evidence series order differs from mapping")
    observed_series: list[dict[str, str]] = []
    previous: dict[str, tuple[Path, str]] = {}
    for mapping, observed in zip(batch, series, strict=True):
        fail(isinstance(observed, dict) and set(observed) == ENTRY_FIELDS, f"{mapping['patch']}: evidence fields are invalid")
        fail(observed.get("verdict") == "VALID", f"{mapping['patch']}: evidence verdict is not VALID")
        module, patch = observed.get("module"), observed.get("patch")
        fail(module == mapping["module"] and isinstance(patch, str), f"{mapping['patch']}: evidence module+patch invalid")
        observed_series.append({"module": module, "patch": patch})
        expected = lock_values(mapping)
        for field, expected_value in expected.items():
            fail(observed.get(field) == expected_value, f"{mapping['patch']}: {field} differs from schema-v2 lock")
        applied_commit = oid(observed.get("applied_commit"), f"{mapping['patch']} applied commit")
        applied_tree = oid(observed.get("applied_tree"), f"{mapping['patch']} applied tree")
        row = rows.get(mapping["module"])
        fail(row is not None, f"{mapping['patch']}: module missing from module map")
        repo = contained(workspace, row["path"], f"{mapping['patch']} repository")
        fail(git(repo, "cat-file", "-e", f"{applied_commit}^{{commit}}") == "", f"{mapping['patch']}: applied commit missing")
        fail(git(repo, "rev-parse", f"{applied_commit}^{{tree}}") == applied_tree, f"{mapping['patch']}: applied tree is not the commit tree")
        integration = row["integration_oid"]
        fail(is_ancestor(repo, applied_commit, integration), f"{mapping['patch']}: applied commit is not an ancestor of integration")
        if module in previous:
            previous_repo, previous_commit = previous[module]
            fail(previous_repo == repo and is_ancestor(repo, previous_commit, applied_commit), f"{mapping['patch']}: applied commits are not in per-module ancestry order")
        previous[module] = (repo, applied_commit)
    fail(observed_series == expected_series, "lock-first evidence series do not exactly match the grouped ordered batch")
    observed_keys = [(entry["module"], entry["patch"]) for entry in observed_series]
    fail(len(set(observed_keys)) == batch_metadata["expected_count"], "lock-first evidence contains duplicate module+patch")


def compare_immutable_oracle(
    oracle_path: Path,
    candidate_path: Path,
    candidate_manifest_path: Path,
    evidence_path: Path,
    mapping_path: Path,
    candidate_workspace: Path,
    transaction_root: Path,
    result_path: Path,
) -> None:
    """Compare the independent clean-ODB cherry-pick oracle with candidate."""
    fail(not result_path.exists() and not result_path.is_symlink(), "compare result path already exists")
    oracle = load_json(oracle_path)
    fail(set(oracle) == {
        "oracle_schema_version", "mode", "profile", "profile_order", "batches",
        "modules", "generated_profile_locks", "frozen_manifest_sha256",
        "clean_odb", "cleanup", "verdict",
    }, "immutable oracle fields are invalid")
    fail(oracle.get("oracle_schema_version") == 2, "immutable oracle schema version mismatch")
    fail(oracle.get("mode") == "immutable-cherry-pick-oracle", "control mode must be immutable-cherry-pick-oracle")
    fail(oracle.get("verdict") == "VALID", "immutable oracle verdict is not VALID")
    fail(
        oracle.get("cleanup")
        == {"root": "removed", "worktrees": "removed", "refs": "removed"},
        "immutable oracle cleanup is incomplete",
    )
    clean_odb = oracle.get("clean_odb")
    fail(
        isinstance(clean_odb, dict)
        and set(clean_odb)
        == {
            "module_count", "immutable_fetch_transactions", "alternates",
            "shallow", "partial",
        }
        and isinstance(clean_odb["module_count"], int)
        and clean_odb["module_count"] > 0
        and clean_odb["immutable_fetch_transactions"] == clean_odb["module_count"]
        and clean_odb["alternates"] == clean_odb["shallow"] == clean_odb["partial"] == 0,
        "immutable oracle clean-ODB evidence is incomplete",
    )
    candidate = load_json(candidate_path)
    profile = oracle.get("profile")
    fail(isinstance(profile, str) and profile and candidate.get("profile") == profile, "oracle/candidate profile differs")
    rows = module_rows(candidate, "candidate module map")
    batch_metadata = load_batch(mapping_path, set(rows))
    batches = oracle.get("batches")
    fail(isinstance(batches, list) and batches, "immutable oracle batches are missing")
    fail(
        oracle.get("profile_order") == [batch.get("profile") for batch in batches],
        "immutable oracle profile order differs from batches",
    )
    target_batch = batches[-1]
    fail(
        isinstance(target_batch, dict)
        and set(target_batch)
        == {
            "profile", "batch_id", "expected_count", "module_order",
            "series_order", "series", "verdict",
        },
        "immutable oracle target batch fields are invalid",
    )
    fail(target_batch.get("profile") == profile, "immutable oracle target profile differs")
    fail(target_batch.get("batch_id") == batch_metadata["batch_id"], "immutable oracle batch differs from mapping")
    fail(target_batch.get("expected_count") == batch_metadata["expected_count"], "immutable oracle count differs from mapping")
    fail(target_batch.get("module_order") == batch_metadata["module_order"], "immutable oracle module order differs")
    expected_series = [
        {"module": entry["module"], "patch": entry["patch"]}
        for entry in batch_metadata["series"]
    ]
    fail(target_batch.get("series_order") == expected_series, "immutable oracle series order differs")
    oracle_series = target_batch.get("series")
    fail(
        isinstance(oracle_series, list)
        and len(oracle_series) == batch_metadata["expected_count"]
        and all(
            isinstance(entry, dict)
            and set(entry) == ENTRY_FIELDS
            and entry.get("verdict") == "VALID"
            for entry in oracle_series
        ),
        "immutable oracle series evidence is invalid",
    )
    for mapping, observed in zip(
        batch_metadata["series"], oracle_series, strict=True
    ):
        fail(
            observed["module"] == mapping["module"]
            and observed["patch"] == mapping["patch"],
            f"{mapping['patch']}: immutable oracle identity differs",
        )
        for field, expected_value in lock_values(mapping).items():
            fail(
                observed[field] == expected_value,
                f"{mapping['patch']}: immutable oracle {field} differs "
                "from schema-v2 lock",
            )
        oid(
            observed["applied_commit"],
            f"{mapping['patch']} immutable oracle applied commit",
        )
        oid(
            observed["applied_tree"],
            f"{mapping['patch']} immutable oracle applied tree",
        )
    oracle_rows = oracle.get("modules")
    fail(isinstance(oracle_rows, list) and len(oracle_rows) == len(rows), "immutable oracle module count differs")
    observed_trees: dict[str, str] = {}
    for row in oracle_rows:
        fail(isinstance(row, dict) and set(row) == {"module", "commit", "tree"}, "immutable oracle module row is invalid")
        module = row.get("module")
        fail(isinstance(module, str) and module in rows and module not in observed_trees, "immutable oracle module is invalid or duplicate")
        oid(row.get("commit"), f"immutable oracle {module} commit")
        observed_trees[module] = oid(row.get("tree"), f"immutable oracle {module} tree")
    fail(observed_trees == {module: row["tree"] for module, row in rows.items()}, "immutable oracle and canonical module trees differ")
    manifest = load_json(candidate_manifest_path)
    verify_manifest(manifest, "candidate manifest")
    generated = oracle.get("generated_profile_locks")
    fail(
        isinstance(generated, list)
        and generated == manifest.get("generated_profile_locks"),
        "immutable oracle and canonical generated locks differ",
    )
    fail(
        oracle.get("frozen_manifest_sha256")
        == manifest.get("frozen_manifest_sha256"),
        "immutable oracle and canonical frozen manifest differ",
    )
    verify_actual_maps(candidate_workspace, rows, profile, "candidate")
    verify_candidate_evidence(evidence_path, batch_metadata, rows, candidate_workspace)
    assert_no_transaction_state(candidate_workspace, rows, transaction_root, "candidate")
    write_result(
        result_path,
        {
            "verdict": "VALID",
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "batch_id": batch_metadata["batch_id"],
            "expected_count": batch_metadata["expected_count"],
            "module_order": batch_metadata["module_order"],
            "module_count": len(rows),
            "control_mode": "immutable-cherry-pick-oracle",
            "candidate_mode": "default-lock-first",
            "lock_first_evidence": evidence_path.name,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    oracle = sub.add_parser("compare-immutable-oracle")
    for name in ("oracle", "candidate", "candidate-manifest", "evidence", "mapping", "candidate-workspace", "transaction-root", "result"):
        oracle.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        compare_immutable_oracle(
            args.oracle,
            args.candidate,
            args.candidate_manifest,
            args.evidence,
            args.mapping,
            args.candidate_workspace,
            args.transaction_root,
            args.result,
        )
    except AcceptanceError as error:
        print(f"patch-stack lock-first acceptance: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
