#!/usr/bin/env python3
"""Test-only archive oracle for lock-first differential acceptance.

This module is intentionally outside ``west_commands``. It is only for a
manual/disposable control side and never creates an integration branch or
mutates the supplied West workspace. Its result is an allowlisted JSON map,
not a production materialization artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml


class OracleError(RuntimeError):
    pass


MAPPING_FIELDS = {"schema_version", "profile", "batch_id", "expected_count", "series"}
ENTRY_FIELDS = {"profile", "module", "patch", "lock"}
# Match the manual acceptance's repository-local identity, but pass it only
# with each disposable command. No source/global/worktree config is changed.
IDENTITY = ("-c", "user.name=West Lock-first Acceptance", "-c", "user.email=west-lock-first@example.invalid")


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise OracleError(message)


def git(repo: Path, *args: str, capture: bool = True, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise OracleError(f"{repo}: git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip() if capture else ""


def west(workspace: Path, *args: str) -> str:
    result = subprocess.run(["west", *args], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise OracleError(f"west {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def load_mapping(path: Path, profile: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise OracleError(f"invalid oracle mapping: {error}") from error
    fail(isinstance(data, dict) and set(data) == MAPPING_FIELDS and data.get("schema_version") == 2, "oracle mapping must use exact schema_version 2")
    fail(data.get("profile") == profile and isinstance(data.get("batch_id"), str) and data["batch_id"], "oracle mapping profile or batch is invalid")
    series = data.get("series")
    fail(isinstance(data.get("expected_count"), int) and data["expected_count"] > 0 and isinstance(series, list) and len(series) == data["expected_count"], "oracle mapping count is invalid")
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(series):
        fail(isinstance(entry, dict) and set(entry) == ENTRY_FIELDS, f"oracle mapping entry {index} fields are invalid")
        fail(all(isinstance(entry.get(key), str) and entry[key] for key in ENTRY_FIELDS), f"oracle mapping entry {index} scalar is invalid")
        fail(entry["profile"] == profile, f"oracle mapping entry {index} profile differs")
        key = (entry["module"], entry["patch"])
        fail(key not in seen, f"oracle mapping entry {index} duplicates module+patch")
        seen.add(key)
    return data


def projects(workspace: Path) -> dict[str, Path]:
    top = Path(west(workspace, "topdir"))
    result: dict[str, Path] = {}
    for line in west(workspace, "list", "-f", "{path}\t{abspath}").splitlines():
        relative, absolute = line.split("\t", 1)
        if relative in result:
            raise OracleError(f"duplicate West project path: {relative}")
        result[relative] = Path(absolute)
    fail("darling" in result, "oracle workspace has no darling project")
    fail(top == workspace.parent or (workspace / ".west").exists(), "oracle workspace is not a West manifest repository")
    return result


def profile_stack(workspace: Path, profile: str) -> list[str]:
    result: list[str] = []
    current = profile
    while current:
        fail(current not in result, "profile base cycle")
        result.append(current)
        data = yaml.safe_load((workspace / "patches" / current / "patches.yml").read_text())
        fail(isinstance(data, dict) and isinstance(data.get("patches"), list), f"{current}: invalid patch profile")
        base = data.get("base-profile")
        fail(base is None or isinstance(base, str), f"{current}: invalid base profile")
        current = base
    return list(reversed(result))


def grouped_profile_entries(workspace: Path, profile: str, mapping_path: Path) -> tuple[dict[str, Any], OrderedDict[str, list[dict[str, Any]]]]:
    mapping = load_mapping(mapping_path, profile)
    profile_data = yaml.safe_load((workspace / "patches" / profile / "patches.yml").read_text())
    patches = profile_data.get("patches") if isinstance(profile_data, dict) else None
    fail(isinstance(patches, list), f"{profile}: patches are invalid")
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for patch in patches:
        fail(isinstance(patch, dict) and isinstance(patch.get("module"), str) and isinstance(patch.get("path"), str), f"{profile}: patch is invalid")
        grouped.setdefault(patch["module"], []).append(patch)
    expected = [(module, patch["path"]) for module, entries in grouped.items() for patch in entries]
    observed = [(entry["module"], entry["patch"]) for entry in mapping["series"]]
    fail(observed == expected, f"{profile}: oracle mapping differs from grouped execution order")
    return mapping, grouped


def generated_lock_metadata(workspace: Path, profile: str, targets: dict[str, Path]) -> dict[str, Any]:
    """Reproduce generated-lock bytes in oracle evidence without writing it."""
    frozen = workspace / "west.lock.yml"
    fail(frozen.is_file() and not frozen.is_symlink(), "oracle frozen manifest is unavailable")
    try:
        data = yaml.safe_load(frozen.read_text())
    except yaml.YAMLError as error:
        raise OracleError(f"oracle frozen manifest is invalid: {error}") from error
    fail(isinstance(data, dict) and isinstance(data.get("manifest"), dict) and isinstance(data["manifest"].get("projects"), list), "oracle frozen manifest has no project list")
    revisions = {module: git(target, "rev-parse", "HEAD") for module, target in targets.items()}
    for project in data["manifest"]["projects"]:
        fail(isinstance(project, dict), "oracle frozen manifest project is invalid")
        path = project.get("path", project.get("name"))
        if path in revisions:
            project["revision"] = revisions[path]
    payload = yaml.safe_dump(data, sort_keys=False, width=1000).encode()
    return {
        "profile": profile,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "frozen_manifest_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
    }


def apply(workspace: Path, profile: str, mapping_path: Path, output: Path) -> None:
    """Run archive control only in disposable worktrees and publish JSON once."""
    fail(not output.exists() and not output.is_symlink(), "oracle output already exists")
    configured = load_mapping(mapping_path, profile)
    target_profile_data = yaml.safe_load((workspace / "patches" / profile / "patches.yml").read_text())
    fail(isinstance(target_profile_data, dict) and isinstance(target_profile_data.get("integration-date"), str), f"{profile}: integration-date is invalid")
    all_projects = projects(workspace)
    stack = profile_stack(workspace, profile)
    # The current target mapping is exact; base profile mappings are obtained
    # through the same supplied lock-first registry convention.
    mapping_root = mapping_path.parent
    profiles_registry = yaml.safe_load((mapping_root / "lock-first-profiles-v1.yml").read_text())
    fail(isinstance(profiles_registry, dict) and profiles_registry.get("schema_version") == 1, "oracle profile registry is invalid")
    by_profile = {entry.get("profile"): entry.get("mapping") for entry in profiles_registry.get("profiles", []) if isinstance(entry, dict)}
    grouped_stack: OrderedDict[str, list[tuple[str, dict[str, Any]]]] = OrderedDict()
    for stacked in stack:
        selected_mapping = mapping_path if stacked == profile else mapping_root / str(by_profile.get(stacked, ""))
        _mapping, grouped = grouped_profile_entries(workspace, stacked, selected_mapping)
        for module, patches in grouped.items():
            grouped_stack.setdefault(module, []).extend((stacked, patch) for patch in patches)
    modules = {"darling", *grouped_stack}
    fail(modules <= set(all_projects), f"oracle has unknown modules: {sorted(modules - set(all_projects))}")
    root = Path(tempfile.mkdtemp(prefix="west-test-legacy-oracle-"))
    worktrees: list[tuple[Path, Path]] = []
    payload: dict[str, Any] | None = None
    try:
        targets: dict[str, Path] = {}
        for module in sorted(modules, key=lambda value: (len(Path(value).parts), value)):
            source = all_projects[module]
            target = root / module
            target.parent.mkdir(parents=True, exist_ok=True)
            revision = git(source, "rev-parse", "HEAD")
            git(source, "worktree", "add", "--quiet", "--detach", str(target), revision, capture=False)
            worktrees.append((source, target)); targets[module] = target
        for module, entries in grouped_stack.items():
            target = targets[module]
            for stacked, patch in entries:
                patch_path = workspace / "patches" / stacked / patch["path"]
                fail(patch_path.is_file() and not patch_path.is_symlink(), f"{stacked}/{patch['path']}: archive is unavailable")
                git(target, *IDENTITY, "am", "--3way", "--committer-date-is-author-date", str(patch_path), capture=False)
        # The superproject records nested child heads only in the disposable
        # oracle worktree; this never creates an integration ref in a source
        # repository. It allows a direct tree comparison with the candidate.
        darling = targets["darling"]
        nested = [module for module in grouped_stack if module != "darling"]
        if nested:
            git(darling, "add", *(str(Path(module).relative_to("darling")) for module in nested), capture=False)
            env = os.environ.copy(); env["GIT_AUTHOR_DATE"] = target_profile_data["integration-date"]; env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
            git(darling, *IDENTITY, "commit", "-m", f"Test-only legacy oracle {profile}", capture=False, env=env)
        rows = [{"module": module, "path": str(target.relative_to(root)), "commit": git(target, "rev-parse", "HEAD"), "tree": git(target, "rev-parse", "HEAD^{tree}")} for module, target in sorted(targets.items())]
        generated = generated_lock_metadata(workspace, profile, targets)
        payload = {"mode": "test-only-legacy-oracle", "profile": profile,
                   "batch_id": configured["batch_id"], "expected_count": configured["expected_count"],
                   "modules": rows, "generated_profile_lock": generated,
                   "cleanup": {"root": "removed", "worktrees": "removed"}}
    finally:
        errors: list[str] = []
        for source, target in reversed(worktrees):
            result = subprocess.run(["git", "worktree", "remove", "--force", str(target)], cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode:
                errors.append(result.stderr.strip())
        shutil.rmtree(root, ignore_errors=True)
        if errors:
            raise OracleError("oracle worktree cleanup failed: " + "; ".join(errors))
    fail(payload is not None, "oracle did not produce a result")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        temporary.replace(output)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise OracleError(f"oracle evidence write failed: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        apply(args.workspace.resolve(), args.profile, args.mapping.resolve(), args.output.resolve())
    except OracleError as error:
        print(f"test-only legacy oracle: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
