#!/usr/bin/env python3
"""Typed, fail-closed comparison for lock-first batch acceptance.

The generic capture/staging helper remains in ``patch_stack_acceptance.py``.
This module validates versioned aggregate evidence against every declared
schema-v2 lock and against the actual lock-first Git worktree.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
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
CANDIDATE_CACHE_SCHEMA_VERSION = 2


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


def _git_identity(workspace: Path, revision: str, label: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise AcceptanceError(
            f"{label}: cannot resolve {revision}: {result.stderr.strip()}"
        )
    value = result.stdout.strip()
    oid(value, f"{label} {revision}")
    return value


def verify_manifest(
    value: dict[str, Any],
    label: str,
    workspace: Path | None = None,
) -> None:
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
    if workspace is not None:
        fail(
            value["workspace_commit"] == _git_identity(workspace, "HEAD", label),
            f"{label}: workspace commit is not the candidate HEAD",
        )
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
            and set(row)
            == {"profile", "path", "size", "sha256", "semantic_sha256"},
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
        fail(
            isinstance(row.get("semantic_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", row["semantic_sha256"]),
            f"{label}: generated lock semantic hash invalid",
        )
        if workspace is not None:
            generated_path = contained(
                workspace, row["path"], f"{label} generated lock"
            )
            fail(
                generated_path.is_file() and not generated_path.is_symlink(),
                f"{label}: generated lock file is unavailable",
            )
            generated_bytes = generated_path.read_bytes()
            fail(
                len(generated_bytes) == row["size"]
                and hashlib.sha256(generated_bytes).hexdigest() == row["sha256"],
                f"{label}: generated lock file integrity differs",
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
) -> dict[str, Any]:
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
    return evidence


def _series_applied_trees(series: list[dict[str, Any]]) -> dict[str, str]:
    trees: dict[str, str] = {}
    for entry in series:
        module = entry.get("module")
        applied_tree = entry.get("applied_tree")
        fail(
            isinstance(module, str)
            and module
            and isinstance(applied_tree, str)
            and re.fullmatch(r"[0-9a-f]{40}", applied_tree) is not None,
            "series applied-tree evidence is invalid",
        )
        trees[module] = applied_tree
    return trees


def _verify_parent_gitlink_publication(
    repo: Path,
    content_tree: str,
    integration_tree: str,
    expected_gitlinks: dict[str, str],
    label: str,
) -> None:
    result = subprocess.run(
        ["git", "diff-tree", "--raw", "-r", content_tree, integration_tree],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fail(
        result.returncode == 0,
        f"{label} tree comparison failed: {result.stderr.strip()}",
    )
    observed: set[str] = set()
    for line in filter(None, result.stdout.splitlines()):
        fields = line.split("\t", 1)
        fail(len(fields) == 2, f"{label} diff is malformed")
        metadata, path = fields
        tokens = metadata.split()
        modes = [tokens[0].lstrip(":"), tokens[1]] if len(tokens) >= 2 else []
        fail(
            path in expected_gitlinks and modes == ["160000", "160000"],
            f"{label} changed non-managed content {path}",
        )
        actual_gitlink = git(repo, "ls-tree", integration_tree, "--", path).split()
        fail(
            len(actual_gitlink) >= 3
            and actual_gitlink[0] == "160000"
            and actual_gitlink[1] == "commit"
            and actual_gitlink[2] == expected_gitlinks[path],
            f"{label} gitlink differs from managed child: {path}",
        )
        observed.add(path)
    fail(
        observed == set(expected_gitlinks),
        f"{label} did not publish the exact managed child set",
    )


def _verify_candidate_parent_integration(
    workspace: Path,
    parent: dict[str, Any],
    children: dict[str, dict[str, Any]],
    content_tree: str,
) -> None:
    parent_repo = contained(workspace, parent["path"], "candidate parent repository")
    parent_path = Path(parent["path"])
    expected_gitlinks = {
        str(Path(row["path"]).relative_to(parent_path)): row["integration_oid"]
        for row in children.values()
    }
    _verify_parent_gitlink_publication(
        parent_repo,
        content_tree,
        parent["tree"],
        expected_gitlinks,
        "candidate parent integration",
    )


def _verify_semantic_module_equivalence(
    workspace: Path,
    oracle_trees: dict[str, str],
    rows: dict[str, dict[str, Any]],
    oracle_series: list[dict[str, Any]],
    candidate_series: list[dict[str, Any]],
) -> None:
    candidate_trees = {module: row["tree"] for module, row in rows.items()}
    mismatches = {
        module
        for module, tree in oracle_trees.items()
        if candidate_trees.get(module) != tree
    }
    if not mismatches:
        return
    oracle_content = _series_applied_trees(oracle_series)
    candidate_content = _series_applied_trees(candidate_series)
    for module in mismatches:
        parent_path = Path(rows[module]["path"])
        children = {
            child_module: row
            for child_module, row in rows.items()
            if child_module != module
            and Path(row["path"]).is_relative_to(parent_path)
        }
        fail(
            children
            and oracle_content.get(module) == candidate_content.get(module),
            "immutable oracle and canonical module trees differ",
        )
        fail(
            all(
                oracle_trees.get(child_module) == child_row["tree"]
                for child_module, child_row in children.items()
            ),
            "immutable oracle and canonical nested module trees differ",
        )
        _verify_candidate_parent_integration(
            workspace, rows[module], children, candidate_content[module]
        )


def _generated_lock_semantics(rows: object, label: str) -> list[dict[str, str]]:
    fail(isinstance(rows, list) and rows, f"{label} generated locks are missing")
    expected_fields = {"profile", "path", "semantic_sha256"}
    result: list[dict[str, str]] = []
    for row in rows:
        fail(
            isinstance(row, dict) and set(row) == expected_fields,
            f"{label} generated lock row is invalid",
        )
        profile, path = row.get("profile"), row.get("path")
        fail(
            isinstance(profile, str)
            and profile
            and isinstance(path, str)
            and path == f"patches/{profile}/west.lock.yml",
            f"{label} generated lock identity is invalid",
        )
        fail(
            isinstance(row["semantic_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", row["semantic_sha256"]) is not None,
            f"{label} generated lock semantic hash is invalid",
        )
        result.append(
            {
                "profile": profile,
                "path": path,
                "semantic_sha256": row["semantic_sha256"],
            }
        )
    return result


def _nested_managed_modules(
    module: str,
    managed: set[str],
    candidate_rows: dict[str, dict[str, Any]],
) -> list[str]:
    parent = Path(candidate_rows[module]["path"])
    descendants: list[str] = []
    for child in managed - {module}:
        child_path = Path(candidate_rows[child]["path"])
        try:
            child_path.relative_to(parent)
        except ValueError:
            continue
        descendants.append(child)
    nested: list[str] = []
    for child in descendants:
        child_path = Path(candidate_rows[child]["path"])
        if any(
            child != ancestor
            and child_path.is_relative_to(Path(candidate_rows[ancestor]["path"]))
            for ancestor in descendants
        ):
            continue
        nested.append(child)
    return sorted(nested)


def _candidate_generated_lock_semantics(
    raw_rows: object,
    batches: list[dict[str, Any]],
    manifest_workspace: Path,
    candidate_workspace: Path,
    candidate_rows: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    fail(isinstance(raw_rows, list) and raw_rows, "candidate generated locks are missing")
    cumulative_modules: dict[str, set[str]] = {}
    managed_so_far: set[str] = set()
    for batch in batches:
        fail(
            isinstance(batch, dict)
            and isinstance(batch.get("profile"), str)
            and batch["profile"]
            and isinstance(batch.get("module_order"), list)
            and all(
                isinstance(module, str) and module
                for module in batch["module_order"]
            ),
            "oracle generated-lock batch is invalid",
        )
        fail(
            batch["profile"] not in cumulative_modules,
            "oracle contains duplicate generated-lock profiles",
        )
        managed_so_far.update(batch["module_order"])
        cumulative_modules[batch["profile"]] = set(managed_so_far)
    result: list[dict[str, str]] = []
    expected_fields = {"profile", "path", "size", "sha256", "semantic_sha256"}
    for raw_row in raw_rows:
        fail(
            isinstance(raw_row, dict) and set(raw_row) == expected_fields,
            "candidate generated lock row is invalid",
        )
        profile, path = raw_row["profile"], raw_row["path"]
        fail(
            isinstance(profile, str)
            and profile in cumulative_modules
            and isinstance(path, str)
            and path == f"patches/{profile}/west.lock.yml",
            "candidate generated lock identity is invalid",
        )
        lock_path = contained(
            manifest_workspace, path, "candidate generated lock"
        )
        try:
            lock = yaml.safe_load(lock_path.read_bytes())
        except (OSError, yaml.YAMLError) as exc:
            fail(False, f"candidate generated lock is unreadable: {exc}")
        try:
            lock = json.loads(json.dumps(lock))
        except (TypeError, ValueError) as exc:
            fail(False, f"candidate generated lock cannot be normalized: {exc}")
        fail(
            isinstance(lock, dict)
            and isinstance(lock.get("manifest"), dict)
            and isinstance(lock["manifest"].get("projects"), list),
            f"{path}: candidate generated lock schema is invalid",
        )
        projects = lock["manifest"]["projects"]
        projects_by_path: dict[str, dict[str, Any]] = {}
        for project in projects:
            project_path = (
                project.get("path", project.get("name"))
                if isinstance(project, dict)
                else None
            )
            fail(
                isinstance(project_path, str) and project_path,
                f"{path}: generated lock project is invalid",
            )
            fail(
                project_path not in projects_by_path,
                f"{path}: duplicate generated lock project path {project_path}",
            )
            projects_by_path[project_path] = project
        managed = cumulative_modules[profile]
        fail(
            managed
            and managed <= set(candidate_rows),
            f"{path}: generated lock references an unknown managed module",
        )
        locked_revisions: dict[str, str] = {}
        for module in managed:
            module_path = candidate_rows[module]["path"]
            project = projects_by_path.get(module_path)
            fail(project is not None, f"{path}: generated lock omits {module}")
            revision = project.get("revision")
            fail(
                isinstance(revision, str)
                and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
                f"{path}: generated lock revision is invalid for {module}",
            )
            locked_revisions[module] = revision
        for module in managed:
            module_path = candidate_rows[module]["path"]
            project = projects_by_path[module_path]
            revision = locked_revisions[module]
            repo = contained(
                candidate_workspace,
                module_path,
                f"candidate generated-lock repository {module}",
            )
            fail(
                (repo / ".git").exists(),
                f"candidate workspace repository missing: {module_path}",
            )
            nested = _nested_managed_modules(module, managed, candidate_rows)
            if nested:
                content_tree = git(repo, "rev-parse", f"{revision}^1^{{tree}}")
                integration_tree = git(repo, "rev-parse", f"{revision}^{{tree}}")
                parent_path = Path(module_path)
                expected_gitlinks: dict[str, str] = {}
                for child in nested:
                    relative = str(
                        Path(candidate_rows[child]["path"]).relative_to(parent_path)
                    )
                    expected_gitlinks[relative] = locked_revisions[child]
                _verify_parent_gitlink_publication(
                    repo,
                    content_tree,
                    integration_tree,
                    expected_gitlinks,
                    f"{path}: {module} integration",
                )
                project["revision"] = content_tree
            else:
                project["revision"] = git(repo, "rev-parse", f"{revision}^{{tree}}")
        semantic_payload = yaml.safe_dump(
            lock,
            sort_keys=False,
            width=1000,
        ).encode()
        semantic_sha256 = hashlib.sha256(semantic_payload).hexdigest()
        fail(
            semantic_sha256 == raw_row["semantic_sha256"],
            f"{path}: candidate generated lock semantic hash mismatch",
        )
        result.append(
            {
                "profile": profile,
                "path": path,
                "semantic_sha256": semantic_sha256,
            }
        )
    return result


def compare_immutable_oracle(
    oracle_path: Path,
    candidate_path: Path,
    candidate_manifest_path: Path,
    evidence_path: Path,
    mapping_path: Path,
    candidate_workspace: Path,
    transaction_root: Path,
    result_path: Path,
    expected_modules_path: Path | None = None,
    expected_manifest_path: Path | None = None,
    manifest_workspace: Path | None = None,
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
    if expected_modules_path is not None:
        expected_modules = load_json(expected_modules_path)
        fail(
            candidate == expected_modules,
            "candidate module map differs from the canonical captured module map",
        )
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
    fail(target_batch.get("verdict") == "VALID", "immutable oracle target batch verdict is not VALID")
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
    fail(
        isinstance(oracle_rows, list) and len(oracle_rows) == len(rows),
        "immutable oracle module count differs",
    )
    oracle_trees: dict[str, str] = {}
    for row in oracle_rows:
        fail(isinstance(row, dict) and set(row) == {"module", "commit", "tree"}, "immutable oracle module row is invalid")
        module = row.get("module")
        fail(isinstance(module, str) and module in rows and module not in oracle_trees, "immutable oracle module is invalid or duplicate")
        oid(row.get("commit"), f"immutable oracle {module} commit")
        oracle_trees[module] = oid(row.get("tree"), f"immutable oracle {module} tree")
    manifest = load_json(candidate_manifest_path)
    verify_manifest(
        manifest,
        "candidate manifest",
        manifest_workspace or candidate_workspace,
    )
    if expected_manifest_path is not None:
        expected_manifest = load_json(expected_manifest_path)
        verify_manifest(expected_manifest, "expected manifest")
        fail(
            manifest == expected_manifest,
            "candidate manifest differs from the canonical captured manifest",
        )
    generated = _generated_lock_semantics(
        oracle.get("generated_profile_locks"), "immutable oracle"
    )
    candidate_generated = _candidate_generated_lock_semantics(
        manifest.get("generated_profile_locks"),
        batches,
        manifest_workspace or candidate_workspace,
        candidate_workspace,
        rows,
    )
    fail(
        generated == candidate_generated,
        "immutable oracle and canonical generated locks differ",
    )
    fail(
        oracle.get("frozen_manifest_sha256")
        == manifest.get("frozen_manifest_sha256"),
        "immutable oracle and canonical frozen manifest differ",
    )
    verify_actual_maps(candidate_workspace, rows, profile, "candidate")
    candidate_evidence = verify_candidate_evidence(
        evidence_path, batch_metadata, rows, candidate_workspace
    )
    _verify_semantic_module_equivalence(
        candidate_workspace,
        oracle_trees,
        rows,
        oracle_series,
        candidate_evidence["series"],
    )
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


def seed_source_refs(
    manifest_workspace: Path,
    source_workspace: Path,
    candidate_workspace: Path,
    profile: str,
) -> None:
    """Copy only manifest-declared source branches into the disposable candidate."""
    manifest_path = contained(
        manifest_workspace,
        f"patches/{profile}/patches.yml",
        "seed profile manifest",
    )
    try:
        manifest = yaml.safe_load(manifest_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise AcceptanceError(
            f"invalid seed profile manifest {manifest_path}: {error}"
        ) from error
    patches = manifest.get("patches") if isinstance(manifest, dict) else None
    fail(isinstance(patches, list), "seed profile manifest has no patch list")
    refs: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(patches):
        fail(isinstance(entry, dict), f"seed patch {index}: invalid entry")
        module = entry.get("module")
        branch = entry.get("source-branch")
        source = entry.get("source-commit")
        fail(
            isinstance(module, str) and module,
            f"seed patch {index}: invalid module",
        )
        fail(
            isinstance(branch, str) and branch,
            f"seed patch {index}: invalid source branch",
        )
        source = oid(source, f"seed patch {index} source commit")
        ref = f"refs/heads/{branch}"
        checked = subprocess.run(
            ["git", "check-ref-format", ref],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fail(
            checked.returncode == 0,
            f"seed patch {index}: invalid source branch",
        )
        key = (module, branch)
        previous = refs.get(key)
        fail(
            previous is None or previous == source,
            f"seed patch {index}: source branch has conflicting commits",
        )
        refs[key] = source
    fail(refs, "seed profile manifest has no source refs")
    modules: dict[str, list[tuple[str, str]]] = {}
    for (module, branch), expected in sorted(refs.items()):
        modules.setdefault(module, []).append((branch, expected))
    for module, branches in modules.items():
        source_repo = contained(
            source_workspace, module, f"seed source repository {module}"
        )
        candidate_repo = contained(
            candidate_workspace,
            module,
            f"seed candidate repository {module}",
        )
        fail(
            (source_repo / ".git").exists(),
            f"seed source repository is missing: {module}",
        )
        fail(
            (candidate_repo / ".git").exists(),
            f"seed candidate repository is missing: {module}",
        )
        for branch, expected in branches:
            observed = git(
                source_repo, "rev-parse", f"refs/heads/{branch}"
            )
            fail(
                observed == expected,
                "seed source branch differs from declared commit: "
                f"{module}:{branch}",
            )
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(candidate_repo),
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--force",
                str(source_repo),
                *(
                    f"refs/heads/{branch}:refs/heads/{branch}"
                    for branch, _expected in branches
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fail(
            fetched.returncode == 0,
            f"seed fetch failed for {module}: {fetched.stderr.strip()}",
        )
        for branch, expected in branches:
            seeded = git(
                candidate_repo, "rev-parse", f"refs/heads/{branch}"
            )
            fail(
                seeded == expected,
                f"seeded source branch differs: {module}:{branch}",
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_cache_parent(cache: Path, key: str) -> Path:
    fail(
        cache.is_absolute()
        and re.fullmatch(r"[0-9a-f]{64}", key) is not None
        and cache.name == f"candidate-{key}",
        "candidate cache path does not match its key",
    )
    parent = cache.parent
    _reject_symlink_components(parent, "candidate cache parent")
    fail(parent.exists(), "candidate cache parent is missing")
    metadata = parent.lstat()
    fail(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0,
        "candidate cache parent is unsafe",
    )
    return parent


def _cache_file(cache: Path, relative: object, label: str) -> Path:
    path = contained(cache, relative, label)
    fail(path.is_file() and not path.is_symlink(), f"{label}: unavailable")
    return path


def _validate_cached_file(
    cache: Path, row: object, label: str
) -> tuple[Path, dict[str, Any]]:
    fail(
        isinstance(row, dict)
        and set(row) >= {"cache_path", "size", "sha256"},
        f"{label}: invalid file binding",
    )
    path = _cache_file(cache, row["cache_path"], label)
    size = row["size"]
    digest = row["sha256"]
    fail(
        isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and path.stat().st_size == size
        and _sha256_file(path) == digest,
        f"{label}: content differs",
    )
    return path, row


def _load_candidate_cache(
    cache: Path, key: str, profile: str
) -> dict[str, Any] | None:
    _candidate_cache_parent(cache, key)
    if not cache.exists() and not cache.is_symlink():
        return None
    fail(cache.is_dir() and not cache.is_symlink(), "candidate cache is unsafe")
    metadata = cache.lstat()
    fail(
        metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0,
        "candidate cache permissions are unsafe",
    )
    index_path = _cache_file(cache, "index.json", "candidate cache index")
    index = load_json(index_path)
    fail(
        set(index)
        == {
            "schema_version",
            "kind",
            "key",
            "profile",
            "candidate_manifest",
            "lock_evidence",
            "module_map",
            "modules",
            "generated_locks",
        }
        and index.get("schema_version") == CANDIDATE_CACHE_SCHEMA_VERSION
        and index.get("kind") == "west-dev-materialized-candidate"
        and index.get("key") == key
        and index.get("profile") == profile,
        "candidate cache identity differs",
    )
    _validate_cached_file(
        cache, index["candidate_manifest"], "candidate cache manifest"
    )
    _validate_cached_file(
        cache, index["module_map"], "candidate cache module map"
    )
    _validate_cached_file(
        cache, index["lock_evidence"], "candidate cache lock evidence"
    )
    modules = index.get("modules")
    fail(isinstance(modules, list) and modules, "candidate cache modules are missing")
    seen_modules: set[str] = set()
    for position, row in enumerate(modules):
        fail(
            isinstance(row, dict)
            and set(row)
            == {
                "module",
                "path",
                "base",
                "commit",
                "tree",
                "ref",
                "cache_path",
                "size",
                "sha256",
            },
            f"candidate cache module {position}: invalid binding",
        )
        module = row.get("module")
        path = row.get("path")
        fail(
            isinstance(module, str)
            and module
            and module not in seen_modules
            and isinstance(path, str)
            and path == module
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts,
            f"candidate cache module {position}: invalid module",
        )
        oid(row.get("base"), f"candidate cache module {position} base")
        oid(row.get("commit"), f"candidate cache module {position} commit")
        oid(row.get("tree"), f"candidate cache module {position} tree")
        fail(
            row.get("ref") == f"refs/west/dev-candidate-cache/{key}/{position}",
            f"candidate cache module {position}: invalid ref",
        )
        _validate_cached_file(
            cache, row, f"candidate cache module {position} bundle"
        )
        seen_modules.add(module)
    generated = index.get("generated_locks")
    fail(
        isinstance(generated, list) and generated,
        "candidate cache generated locks are missing",
    )
    seen_generated: set[str] = set()
    for position, row in enumerate(generated):
        fail(
            isinstance(row, dict)
            and set(row) == {"path", "cache_path", "size", "sha256"},
            f"candidate cache generated lock {position}: invalid binding",
        )
        path = row.get("path")
        fail(
            isinstance(path, str)
            and path
            and path not in seen_generated
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts,
            f"candidate cache generated lock {position}: invalid path",
        )
        _validate_cached_file(
            cache, row, f"candidate cache generated lock {position}"
        )
        seen_generated.add(path)
    return index


def _write_private_file(
    root: Path, relative: str, data: bytes
) -> dict[str, Any]:
    path = contained(root, relative, "candidate cache output")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return {
        "cache_path": relative,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _frozen_project_revisions(manifest_workspace: Path) -> dict[str, str]:
    try:
        value = yaml.safe_load((manifest_workspace / "west.lock.yml").read_text())
    except (OSError, yaml.YAMLError) as error:
        raise AcceptanceError(f"candidate base manifest is invalid: {error}") from error
    projects = (
        value.get("manifest", {}).get("projects")
        if isinstance(value, dict) and isinstance(value.get("manifest"), dict)
        else None
    )
    fail(isinstance(projects, list) and projects, "candidate base manifest has no projects")
    result: dict[str, str] = {}
    for position, row in enumerate(projects):
        name = row.get("name") if isinstance(row, dict) else None
        path = row.get("path", name) if isinstance(row, dict) else None
        revision = row.get("revision") if isinstance(row, dict) else None
        fail(
            isinstance(path, str)
            and path
            and path not in result
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts,
            f"candidate base project {position}: invalid path",
        )
        result[path] = oid(revision, f"candidate base project {position} revision")
    return result


def hydrate_candidate_cache(
    manifest_workspace: Path,
    candidate_workspace: Path,
    profile: str,
    cache: Path,
    key: str,
    lock_evidence_path: Path,
    modules_path: Path,
    candidate_manifest_path: Path,
    west_command: list[str],
) -> None:
    """Hydrate immutable integration commits, otherwise perform a real replay."""

    fail(
        bool(west_command) and all(isinstance(value, str) and value for value in west_command),
        "candidate replay West command is missing",
    )
    index = _load_candidate_cache(cache, key, profile)
    if index is None:
        applied = subprocess.run(
            [
                *west_command,
                "patch",
                "apply",
                "--profile",
                profile,
                "--lock-first-evidence",
                str(lock_evidence_path),
            ],
            cwd=manifest_workspace,
            stdin=subprocess.DEVNULL,
        )
        fail(applied.returncode == 0, f"candidate replay failed with rc {applied.returncode}")
        print("candidate cache: miss; replayed immutable stack")
        return

    modules = sorted(
        index["modules"],
        key=lambda row: (-len(Path(row["path"]).parts), row["path"]),
    )
    for position, row in enumerate(modules):
        repository = contained(
            candidate_workspace,
            row["path"],
            f"candidate cache destination module {position}",
        )
        bundle, _binding = _validate_cached_file(
            cache, row, f"candidate cache module {position} bundle"
        )
        verified = subprocess.run(
            ["git", "bundle", "verify", str(bundle)],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fail(
            verified.returncode == 0,
            f"candidate cache module {row['module']}: bundle prerequisites differ: "
            f"{verified.stderr.strip()}",
        )
        fetched = subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle),
                row["ref"],
            ],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fail(
            fetched.returncode == 0,
            f"candidate cache module {row['module']}: fetch failed: "
            f"{fetched.stderr.strip()}",
        )
        checked = subprocess.run(
            [
                "git",
                "checkout",
                "-B",
                f"integration/{profile}",
                row["commit"],
            ],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fail(
            checked.returncode == 0
            and git(repository, "rev-parse", "HEAD") == row["commit"]
            and git(repository, "rev-parse", "HEAD^{tree}") == row["tree"],
            f"candidate cache module {row['module']}: checkout differs",
        )

    for position, row in enumerate(index["generated_locks"]):
        source, _binding = _validate_cached_file(
            cache, row, f"candidate cache generated lock {position}"
        )
        destination = contained(
            manifest_workspace,
            row["path"],
            f"candidate cache generated lock destination {position}",
        )
        if destination.exists() or destination.is_symlink():
            fail(
                destination.is_file() and not destination.is_symlink(),
                f"candidate cache generated lock destination {position}: unsafe",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            destination.name + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_bytes(source.read_bytes())
            temporary.chmod(0o644)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    evidence_source, _binding = _validate_cached_file(
        cache, index["lock_evidence"], "candidate cache lock evidence"
    )
    fail(
        not lock_evidence_path.exists() and not lock_evidence_path.is_symlink(),
        "candidate cache evidence destination already exists",
    )
    lock_evidence_path.parent.mkdir(parents=True, exist_ok=True)
    lock_evidence_path.write_bytes(evidence_source.read_bytes())
    for binding, destination, label in (
        (index["module_map"], modules_path, "candidate cache module map"),
        (
            index["candidate_manifest"],
            candidate_manifest_path,
            "candidate cache manifest",
        ),
    ):
        source, _row = _validate_cached_file(cache, binding, label)
        fail(
            not destination.exists() and not destination.is_symlink(),
            f"{label} destination already exists",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    print("candidate cache: hit; hydrated immutable stack")


def publish_candidate_cache(
    manifest_workspace: Path,
    candidate_workspace: Path,
    profile: str,
    cache: Path,
    key: str,
    lock_evidence_path: Path,
    modules_path: Path,
    candidate_manifest_path: Path,
) -> None:
    """Publish only the integration object delta and generated evidence."""

    parent = _candidate_cache_parent(cache, key)
    existing = _load_candidate_cache(cache, key, profile)
    if existing is not None:
        print("candidate cache: existing immutable entry retained")
        return
    modules_document = load_json(modules_path)
    fail(
        modules_document.get("profile") == profile
        and isinstance(modules_document.get("modules"), list)
        and modules_document["modules"],
        "candidate cache module map is invalid",
    )
    candidate_manifest = load_json(candidate_manifest_path)
    generated = candidate_manifest.get("generated_profile_locks")
    fail(isinstance(generated, list) and generated, "candidate cache generated locks are missing")
    base_revisions = _frozen_project_revisions(manifest_workspace)
    staging = parent / f".{cache.name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o700)
    temporary = staging / cache.name
    temporary.mkdir(mode=0o700)
    try:
        manifest_data = candidate_manifest_path.read_bytes()
        manifest_binding = _write_private_file(
            temporary, "candidate-manifest.json", manifest_data
        )
        module_map_binding = _write_private_file(
            temporary, "modules.json", modules_path.read_bytes()
        )
        evidence_data = lock_evidence_path.read_bytes()
        evidence_binding = _write_private_file(
            temporary, "lock-first-evidence.json", evidence_data
        )
        module_bindings: list[dict[str, Any]] = []
        for position, row in enumerate(modules_document["modules"]):
            module = row.get("module") if isinstance(row, dict) else None
            path = row.get("path") if isinstance(row, dict) else None
            commit = row.get("integration_oid") if isinstance(row, dict) else None
            tree = row.get("tree") if isinstance(row, dict) else None
            fail(
                isinstance(module, str)
                and module
                and path == module
                and path in base_revisions,
                f"candidate cache module {position}: invalid captured module",
            )
            commit = oid(commit, f"candidate cache module {position} commit")
            tree = oid(tree, f"candidate cache module {position} tree")
            base = base_revisions[path]
            repository = contained(
                candidate_workspace, path, f"candidate cache source module {position}"
            )
            fail(
                git(repository, "rev-parse", "HEAD") == commit
                and git(repository, "rev-parse", "HEAD^{tree}") == tree
                and subprocess.run(
                    ["git", "merge-base", "--is-ancestor", base, commit],
                    cwd=repository,
                    stdin=subprocess.DEVNULL,
                ).returncode
                == 0,
                f"candidate cache module {module}: source identity differs",
            )
            ref = f"refs/west/dev-candidate-cache/{key}/{position}"
            bundle_relative = f"bundles/{position}.bundle"
            bundle = temporary / bundle_relative
            bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            git(repository, "update-ref", ref, commit)
            try:
                bundled = subprocess.run(
                    [
                        "git",
                        "bundle",
                        "create",
                        "--version=3",
                        str(bundle),
                        ref,
                        f"^{base}",
                    ],
                    cwd=repository,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            finally:
                git(repository, "update-ref", "-d", ref)
            fail(
                bundled.returncode == 0,
                f"candidate cache module {module}: bundle failed: "
                f"{bundled.stderr.strip()}",
            )
            bundle.chmod(0o600)
            verified = subprocess.run(
                ["git", "bundle", "verify", str(bundle)],
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            fail(
                verified.returncode == 0,
                f"candidate cache module {module}: produced bundle is invalid",
            )
            module_bindings.append(
                {
                    "module": module,
                    "path": path,
                    "base": base,
                    "commit": commit,
                    "tree": tree,
                    "ref": ref,
                    "cache_path": bundle_relative,
                    "size": bundle.stat().st_size,
                    "sha256": _sha256_file(bundle),
                }
            )
        generated_bindings: list[dict[str, Any]] = []
        for position, row in enumerate(generated):
            path = row.get("path") if isinstance(row, dict) else None
            fail(
                isinstance(path, str)
                and path
                and not Path(path).is_absolute()
                and ".." not in Path(path).parts,
                f"candidate cache generated lock {position}: invalid path",
            )
            source = contained(
                manifest_workspace, path, f"candidate cache generated lock {position}"
            )
            data = source.read_bytes()
            fail(
                len(data) == row.get("size")
                and hashlib.sha256(data).hexdigest() == row.get("sha256"),
                f"candidate cache generated lock {position}: capture differs",
            )
            binding = _write_private_file(
                temporary, f"generated/{position}.lock", data
            )
            generated_bindings.append({"path": path, **binding})
        index = {
            "schema_version": CANDIDATE_CACHE_SCHEMA_VERSION,
            "kind": "west-dev-materialized-candidate",
            "key": key,
            "profile": profile,
            "candidate_manifest": manifest_binding,
            "module_map": module_map_binding,
            "lock_evidence": evidence_binding,
            "modules": module_bindings,
            "generated_locks": generated_bindings,
        }
        index_path = temporary / "index.json"
        index_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
        index_path.chmod(0o600)
        _load_candidate_cache(temporary, key, profile)
        try:
            temporary.rename(cache)
        except FileExistsError:
            _load_candidate_cache(cache, key, profile)
        print("candidate cache: published immutable entry")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _git_status_paths(repo: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fail(
        result.returncode == 0,
        f"{repo}: cannot read source status: {result.stderr.decode(errors='replace').strip()}",
    )
    fields = result.stdout.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        fail(len(field) >= 4 and field[2:3] == b" ", f"{repo}: malformed Git status")
        status = field[:2]
        paths.add(
            field[3:].decode("utf-8", errors="surrogateescape").rstrip("/")
        )
        if b"R" in status or b"C" in status:
            fail(index < len(fields) and fields[index], f"{repo}: malformed rename status")
            paths.add(
                fields[index].decode("utf-8", errors="surrogateescape").rstrip("/")
            )
            index += 1
    return paths


def _create_shared_worktree(
    source: Path,
    carrier: Path,
    destination: Path,
    revision: str,
    label: str,
    allowed_dirty_paths: set[str] | None = None,
    *,
    independent_objects: bool = False,
) -> None:
    fail(
        source.is_dir() and not source.is_symlink() and (source / ".git").exists(),
        f"{label}: source repository is unavailable",
    )
    fail(
        git(source, "rev-parse", "HEAD") == revision,
        f"{label}: source HEAD differs from frozen revision",
    )
    dirty = _git_status_paths(source)
    allowed = allowed_dirty_paths or set()
    fail(
        dirty <= allowed,
        f"{label}: source repository is dirty outside nested projects: "
        f"{sorted(dirty - allowed)}",
    )
    carrier.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone_argv = ["git", "clone", "--bare"]
    clone_argv.append("--no-local" if independent_objects else "--shared")
    clone_argv.extend(("--", str(source), str(carrier)))
    cloned = subprocess.run(
        clone_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fail(
        cloned.returncode == 0,
        f"{label}: object carrier failed: {cloned.stderr.strip()}",
    )
    checked = subprocess.run(
        [
            "git",
            f"--git-dir={carrier}",
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(destination),
            revision,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fail(
        checked.returncode == 0,
        f"{label}: worktree checkout failed: {checked.stderr.strip()}",
    )
    fail(
        git(destination, "rev-parse", "HEAD") == revision
        and not git(destination, "status", "--porcelain"),
        f"{label}: worktree repository identity differs",
    )


def clone_tier_workspace(
    manifest_workspace: Path,
    source_workspace: Path,
    destination_workspace: Path,
    profile: str,
    candidate_manifest_path: Path,
) -> None:
    """Clone an applied candidate into an isolated tier workspace."""
    fail(
        destination_workspace.is_absolute(),
        "tier destination workspace must be absolute",
    )
    generated = load_json(candidate_manifest_path).get("generated_profile_locks")
    fail(isinstance(generated, list) and generated, "tier generated locks are missing")
    generated_paths: list[Path] = []
    for index, row in enumerate(generated):
        path = row.get("path") if isinstance(row, dict) else None
        size = row.get("size") if isinstance(row, dict) else None
        digest = row.get("sha256") if isinstance(row, dict) else None
        fail(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts
            and isinstance(size, int)
            and not isinstance(size, bool)
            and size > 0
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            f"tier generated lock {index}: invalid identity",
        )
        generated_path = contained(
            manifest_workspace, path, f"tier generated lock {index}"
        )
        fail(
            generated_path.is_file() and not generated_path.is_symlink(),
            f"tier generated lock {index}: unavailable",
        )
        data = generated_path.read_bytes()
        fail(
            len(data) == size and hashlib.sha256(data).hexdigest() == digest,
            f"tier generated lock {index}: content differs from evidence",
        )
        generated_paths.append(generated_path)
    target_lock = contained(
        manifest_workspace,
        f"patches/{profile}/west.lock.yml",
        "tier frozen manifest",
    )
    fail(target_lock in generated_paths, "tier profile lock is not generated evidence")
    try:
        frozen = yaml.safe_load(target_lock.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise AcceptanceError(f"tier frozen manifest is invalid: {error}") from error
    projects = (
        frozen.get("manifest", {}).get("projects")
        if isinstance(frozen, dict)
        and isinstance(frozen.get("manifest"), dict)
        else None
    )
    fail(isinstance(projects, list) and projects, "tier frozen manifest has no projects")
    rows: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for index, project in enumerate(projects):
        name = project.get("name") if isinstance(project, dict) else None
        path = project.get("path", name) if isinstance(project, dict) else None
        revision = project.get("revision") if isinstance(project, dict) else None
        fail(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts
            and path not in seen,
            f"tier project {index}: invalid or duplicate path",
        )
        rows.append((Path(path), oid(revision, f"tier project {index} revision")))
        seen.add(path)
    rows.sort(key=lambda row: (len(row[0].parts), row[0].as_posix()))
    allowed_dirty: dict[Path, set[str]] = {}
    for path, revision in rows:
        allowed_dirty[path] = {
            child.relative_to(path).as_posix()
            for child, _child_revision in rows
            if child != path and child.is_relative_to(path)
        }
        source = contained(
            source_workspace, path.as_posix(), f"tier source project {path}"
        )
        fail(
            git(source, "rev-parse", "HEAD") == revision,
            f"tier source project {path}: HEAD differs from frozen revision",
        )
        dirty = _git_status_paths(source)
        fail(
            dirty <= allowed_dirty[path],
            f"tier source project {path}: dirty outside nested projects: "
            f"{sorted(dirty - allowed_dirty[path])}",
        )
    manifest_revision = _git_identity(
        manifest_workspace, "HEAD", "tier manifest source"
    )
    independent_objects = bool(os.environ.get("WEST_MATERIALIZED_WORKSPACE_LOCK"))
    workspace_index = {
        "schema_version": 1,
        "kind": "west-acceptance-tier-workspace",
        "profile": profile,
        "manifest_revision": manifest_revision,
        "projects": [
            {"path": path.as_posix(), "revision": revision}
            for path, revision in rows
        ],
        "generated_locks": [
            {
                "path": source.relative_to(manifest_workspace).as_posix(),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
            for source in generated_paths
        ],
    }
    if destination_workspace.exists() or destination_workspace.is_symlink():
        fail(
            destination_workspace.is_dir() and not destination_workspace.is_symlink(),
            "tier destination workspace is not a real directory",
        )
        marker = destination_workspace / "tier-workspace-index.json"
        fail(
            marker.is_file() and not marker.is_symlink(),
            "tier destination workspace is incomplete",
        )
        fail(
            load_json(marker) == workspace_index,
            "tier destination workspace identity differs",
        )
        destination_manifest = destination_workspace / "darling-workspace"
        fail(
            _git_identity(destination_manifest, "HEAD", "tier manifest destination")
            == manifest_revision,
            "tier manifest destination HEAD differs",
        )
        allowed_manifest_dirty = {
            source.relative_to(manifest_workspace).as_posix()
            for source in generated_paths
        }
        fail(
            _git_status_paths(destination_manifest) <= allowed_manifest_dirty,
            "tier manifest destination is dirty outside generated locks",
        )
        for path, revision in rows:
            destination = contained(
                destination_workspace,
                path.as_posix(),
                f"tier destination project {path}",
            )
            fail(
                git(destination, "rev-parse", "HEAD") == revision,
                f"tier destination project {path}: HEAD differs from frozen revision",
            )
            dirty = _git_status_paths(destination)
            fail(
                dirty <= allowed_dirty[path],
                f"tier destination project {path}: dirty outside nested projects: "
                f"{sorted(dirty - allowed_dirty[path])}",
            )
        for source in generated_paths:
            relative = source.relative_to(manifest_workspace)
            destination = contained(
                destination_manifest,
                relative.as_posix(),
                f"tier generated lock {relative}",
            )
            fail(
                destination.is_file()
                and not destination.is_symlink()
                and destination.read_bytes() == source.read_bytes(),
                f"tier generated lock {relative}: cached content differs",
            )
        print(f"tier workspace: reused {len(rows)} frozen projects")
        return
    destination_workspace.mkdir(parents=True)
    carrier_root = destination_workspace / ".west-tier-repositories"
    destination_manifest = destination_workspace / "darling-workspace"
    rows_by_depth: dict[int, list[tuple[int, Path, str]]] = {}
    for index, (path, revision) in enumerate(rows):
        rows_by_depth.setdefault(len(path.parts), []).append((index, path, revision))

    def create_project(index: int, path: Path, revision: str) -> None:
        _create_shared_worktree(
            contained(
                source_workspace, path.as_posix(), f"tier source project {path}"
            ),
            carrier_root / f"project-{index:04d}.git",
            contained(
                destination_workspace,
                path.as_posix(),
                f"tier destination project {path}",
            ),
            revision,
            f"tier project {path}",
            allowed_dirty[path],
            independent_objects=independent_objects,
        )

    first_depth = min(rows_by_depth)
    for depth in sorted(rows_by_depth):
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(rows_by_depth[depth]) + (depth == first_depth))
        ) as pool:
            futures = [
                pool.submit(create_project, index, path, revision)
                for index, path, revision in rows_by_depth[depth]
            ]
            if depth == first_depth:
                futures.append(
                    pool.submit(
                        _create_shared_worktree,
                        manifest_workspace,
                        carrier_root / "manifest.git",
                        destination_manifest,
                        manifest_revision,
                        "tier manifest",
                        {
                            source.relative_to(manifest_workspace).as_posix()
                            for source in generated_paths
                        },
                        independent_objects=independent_objects,
                    )
                )
            for future in futures:
                future.result()
    west_root = destination_workspace / ".west"
    west_root.mkdir()
    (west_root / "config").write_text(
        "[manifest]\npath = darling-workspace\nfile = west.yml\n",
        encoding="utf-8",
    )
    for source in generated_paths:
        relative = source.relative_to(manifest_workspace)
        destination = contained(
            destination_manifest,
            relative.as_posix(),
            f"tier generated lock {relative}",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    write_result(
        destination_workspace / "tier-workspace-index.json", workspace_index
    )
    print(f"tier workspace: cloned {len(rows)} frozen projects")


def locked_clone_tier_workspace(
    manifest_workspace: Path,
    source_workspace: Path,
    destination_workspace: Path,
    profile: str,
    candidate_manifest_path: Path,
) -> None:
    raw_lock = os.environ.get("WEST_MATERIALIZED_WORKSPACE_LOCK")
    if not raw_lock:
        clone_tier_workspace(
            manifest_workspace,
            source_workspace,
            destination_workspace,
            profile,
            candidate_manifest_path,
        )
        return
    lock_path = Path(raw_lock)
    fail(lock_path.is_absolute(), "tier workspace lock must be absolute")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    building_marker = destination_workspace.with_name(
        f".{destination_workspace.name}.building"
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if building_marker.exists() or building_marker.is_symlink():
            fail(
                building_marker.is_file()
                and not building_marker.is_symlink()
                and building_marker.read_text(encoding="utf-8")
                == "west-acceptance-tier-workspace\n",
                "tier workspace building marker is invalid",
            )
            if destination_workspace.exists() or destination_workspace.is_symlink():
                fail(
                    destination_workspace.is_dir()
                    and not destination_workspace.is_symlink(),
                    "incomplete tier workspace is not a real directory",
                )
                shutil.rmtree(destination_workspace)
            building_marker.unlink()
        if destination_workspace.exists() or destination_workspace.is_symlink():
            clone_tier_workspace(
                manifest_workspace,
                source_workspace,
                destination_workspace,
                profile,
                candidate_manifest_path,
            )
            return
        building_marker.write_text(
            "west-acceptance-tier-workspace\n", encoding="utf-8"
        )
        clone_tier_workspace(
            manifest_workspace,
            source_workspace,
            destination_workspace,
            profile,
            candidate_manifest_path,
        )
        building_marker.unlink()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)



def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    compare = sub.add_parser("compare-immutable-oracle")
    for name in ("oracle", "candidate", "candidate-manifest", "evidence", "mapping", "candidate-workspace", "transaction-root", "result"):
        compare.add_argument(f"--{name}", type=Path, required=True)
    compare.add_argument("--expected-modules", type=Path)
    compare.add_argument("--expected-manifest", type=Path)
    compare.add_argument("--manifest-workspace", type=Path)
    seed = sub.add_parser("seed-source-refs")
    seed.add_argument("--source-workspace", type=Path, required=True)
    seed.add_argument("--candidate-workspace", type=Path, required=True)
    seed.add_argument("--profile", required=True)
    clone = sub.add_parser("clone-tier-workspace")
    clone.add_argument("--source-workspace", type=Path, required=True)
    clone.add_argument("--destination-workspace", type=Path, required=True)
    clone.add_argument("--profile", required=True)
    clone.add_argument("--candidate-manifest", type=Path, required=True)
    hydrate = sub.add_parser("materialize-candidate")
    for name in (
        "manifest-workspace",
        "candidate-workspace",
        "cache",
        "lock-evidence",
        "modules",
        "candidate-manifest",
    ):
        hydrate.add_argument(f"--{name}", type=Path, required=True)
    hydrate.add_argument("--profile", required=True)
    hydrate.add_argument("--key", required=True)
    hydrate.add_argument(
        "--west-command", nargs=argparse.REMAINDER, required=True
    )
    publish = sub.add_parser("publish-candidate-cache")
    for name in (
        "manifest-workspace",
        "candidate-workspace",
        "cache",
        "lock-evidence",
        "modules",
        "candidate-manifest",
    ):
        publish.add_argument(f"--{name}", type=Path, required=True)
    publish.add_argument("--profile", required=True)
    publish.add_argument("--key", required=True)
    args = parser.parse_args()
    try:
        if args.action == "seed-source-refs":
            seed_source_refs(
                ROOT,
                args.source_workspace,
                args.candidate_workspace,
                args.profile,
            )
        elif args.action == "clone-tier-workspace":
            locked_clone_tier_workspace(
                ROOT,
                args.source_workspace,
                args.destination_workspace,
                args.profile,
                args.candidate_manifest,
            )
        elif args.action == "materialize-candidate":
            hydrate_candidate_cache(
                args.manifest_workspace,
                args.candidate_workspace,
                args.profile,
                args.cache,
                args.key,
                args.lock_evidence,
                args.modules,
                args.candidate_manifest,
                args.west_command,
            )
        elif args.action == "publish-candidate-cache":
            publish_candidate_cache(
                args.manifest_workspace,
                args.candidate_workspace,
                args.profile,
                args.cache,
                args.key,
                args.lock_evidence,
                args.modules,
                args.candidate_manifest,
            )
        else:
            compare_immutable_oracle(
                args.oracle,
                args.candidate,
                args.candidate_manifest,
                args.evidence,
                args.mapping,
                args.candidate_workspace,
                args.transaction_root,
                args.result,
                args.expected_modules,
                args.expected_manifest,
                args.manifest_workspace,
            )
    except AcceptanceError as error:
        print(f"patch-stack lock-first acceptance: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
