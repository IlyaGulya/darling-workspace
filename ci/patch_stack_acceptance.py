#!/usr/bin/env python3
"""Fail-closed capture/staging helpers for canonical hosted acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


class AcceptanceError(RuntimeError):
    pass


ARTIFACT_ALLOWLIST = {
    "immutable-oracle.json",
    "lock-first-manifest.json",
    "lock-first-modules.json",
    "lock-first-evidence.json",
    "acceptance-result.json",
    "cleanup.txt",
    "diagnostics.txt",
    "capture-diagnostics.json",
}
MAX_ARTIFACT_BYTES = 1_000_000
MAX_GENERATED_LOCK_BYTES = 1_000_000


def contained_regular_file(root: Path, relative: str, label: str) -> Path:
    """Return one non-symlink file contained by *root*, fail-closed."""
    candidate = Path(relative)
    fail(not candidate.is_absolute() and ".." not in candidate.parts and candidate.parts,
         f"{label} escapes locks/patch-stack")
    current = root
    for part in candidate.parts:
        current = current / part
        fail(not current.is_symlink(), f"{label} traverses a symlink")
    fail(current.is_file(), f"{label} is not a regular file")
    return current


def generated_lock_profiles(workspace: Path, profile: str) -> list[str]:
    """Resolve the exact generated-lock order from typed composition metadata.

    A composed profile writes one generated lock for every prerequisite it
    applies.  This intentionally follows the lock-first registry and the
    composition graph; it contains no profile or filename special cases.
    """
    locks = workspace / "locks" / "patch-stack"
    registry_path = contained_regular_file(locks, "lock-first-profiles-v1.yml", "profile registry")
    try:
        registry = yaml.safe_load(registry_path.read_text())
    except yaml.YAMLError as error:
        raise AcceptanceError(f"invalid lock-first profile registry: {error}") from error
    fail(isinstance(registry, dict) and set(registry) == {"schema_version", "profiles"}
         and registry.get("schema_version") == 1 and isinstance(registry["profiles"], list),
         "profile registry has invalid typed schema")
    mappings: dict[str, str] = {}
    for item in registry["profiles"]:
        fail(isinstance(item, dict) and set(item) == {"profile", "mapping"},
             "profile registry entry has invalid fields")
        configured, mapping = item["profile"], item["mapping"]
        fail(isinstance(configured, str) and configured and isinstance(mapping, str) and mapping,
             "profile registry entry has invalid scalar")
        fail(configured not in mappings, "profile registry contains duplicate profile")
        mappings[configured] = mapping

    visiting: set[str] = set()
    resolved: list[str] = []

    def visit(current: str) -> None:
        fail(current in mappings, f"{current}: no typed lock-first mapping")
        fail(current not in visiting, "profile composition dependency cycle")
        if current in resolved:
            return
        visiting.add(current)
        mapping_path = contained_regular_file(locks, mappings[current], "profile mapping")
        try:
            mapping = yaml.safe_load(mapping_path.read_text())
        except yaml.YAMLError as error:
            raise AcceptanceError(f"invalid lock-first mapping: {error}") from error
        fail(isinstance(mapping, dict) and mapping.get("schema_version") == 3
             and isinstance(mapping.get("profile"), str) and mapping["profile"] == current
             and isinstance(mapping.get("composition"), str) and mapping["composition"],
             "profile mapping lacks typed composition metadata")
        composition_path = contained_regular_file(locks, mapping["composition"], "profile composition")
        try:
            composition = yaml.safe_load(composition_path.read_text())
        except yaml.YAMLError as error:
            raise AcceptanceError(f"invalid profile composition: {error}") from error
        fail(isinstance(composition, dict) and composition.get("schema_version") == 3
             and isinstance(composition.get("profile"), str) and composition["profile"] == current
             and isinstance(composition.get("prerequisites"), list),
             "profile composition has invalid typed schema")
        try:
            profile_data = yaml.safe_load((workspace / "patches" / current / "patches.yml").read_text())
        except (OSError, yaml.YAMLError) as error:
            raise AcceptanceError(f"invalid profile metadata for generated locks: {error}") from error
        fail(isinstance(profile_data, dict), "profile metadata is invalid")
        declared = [item.get("profile") for item in composition["prerequisites"] if isinstance(item, dict)]
        fail(len(declared) == len(composition["prerequisites"]), "profile composition prerequisite is invalid")
        base_profile = profile_data.get("base-profile")
        if base_profile is None:
            fail(not declared, "profile composition has unexpected prerequisite order")
        else:
            fail(isinstance(base_profile, str) and base_profile and declared == [base_profile],
                 "profile composition prerequisite order differs from typed profile")
        for prerequisite in composition["prerequisites"]:
            fail(isinstance(prerequisite, dict) and isinstance(prerequisite.get("profile"), str)
                 and prerequisite["profile"], "profile composition prerequisite is invalid")
            visit(prerequisite["profile"])
        visiting.remove(current)
        resolved.append(current)

    visit(profile)
    return resolved


def generated_lock_paths(workspace: Path, profile: str) -> list[tuple[str, str]]:
    return [(item, f"patches/{item}/west.lock.yml") for item in generated_lock_profiles(workspace, profile)]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AcceptanceError(f"{repo}: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_raw(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AcceptanceError(f"{repo}: git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def git_optional(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise AcceptanceError(f"{repo}: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def command(workspace: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AcceptanceError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def projects(workspace: Path, top: Path | None = None) -> dict[str, dict[str, Any]]:
    """Index West projects by manifest path, not their display name.

    Patches.yml refers to module paths (for example
    ``darling/src/external/xnu``), while West's project name is separately
    normalized (``darling-src-external-xnu``).
    """
    # Capture obtains this once and passes it here.  Keep the optional form
    # for the standalone transaction-state checker.
    top = top or Path(command(workspace, "west", "topdir"))
    result = {}
    for line in command(workspace, "west", "list", "-f", "{name}\t{path}").splitlines():
        name, relative = line.split("\t", 1)
        if relative in result:
            raise AcceptanceError(f"duplicate West manifest path: {relative}")
        result[relative] = {"name": name, "path": top / relative}
    return result


def assert_clean_odb(repo: Path) -> None:
    if git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise AcceptanceError(f"{repo}: shallow repository")
    if git_optional(repo, "config", "--get", "extensions.partialClone"):
        raise AcceptanceError(f"{repo}: partial clone")
    alternate = Path(git(repo, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternate.is_absolute():
        alternate = repo / alternate
    if alternate.exists():
        raise AcceptanceError(f"{repo}: alternates are forbidden")


def touched_projects(profile_data: dict[str, Any], available: dict[str, dict[str, Any]]) -> set[str]:
    patches = profile_data.get("patches")
    fail(isinstance(patches, list), "profile patches are invalid")
    modules = {"darling"}
    for index, item in enumerate(patches):
        fail(
            isinstance(item, dict)
            and isinstance(item.get("module"), str)
            and item["module"],
            f"profile patch {index} has invalid module",
        )
        modules.add(item["module"])
    unknown = modules - available.keys()
    if unknown:
        raise AcceptanceError(f"unknown profile modules: {sorted(unknown)}")
    return modules


def composed_project_profiles(
    workspace: Path,
    profile: str,
    available: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Map every touched module in the typed stack to its final profile layer."""
    effective: dict[str, str] = {}
    for phase in generated_lock_profiles(workspace, profile):
        try:
            profile_data = yaml.safe_load(
                (workspace / "patches" / phase / "patches.yml").read_text()
            )
        except (OSError, yaml.YAMLError) as error:
            raise AcceptanceError(
                f"{phase}: invalid profile metadata for module capture: {error}"
            ) from error
        fail(isinstance(profile_data, dict), f"{phase}: profile metadata is invalid")
        for module in touched_projects(profile_data, available):
            effective[module] = phase
    fail(effective, f"{profile}: composed profile has no touched modules")
    return effective


def generated_lock_revisions(path: Path) -> dict[str, str]:
    """Load exact project-path revisions from one generated West lock."""
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise AcceptanceError(f"invalid generated profile lock {path}: {error}") from error
    projects_value = value.get("manifest", {}).get("projects") if isinstance(value, dict) else None
    fail(isinstance(projects_value, list), f"generated profile lock has no project list: {path}")
    revisions: dict[str, str] = {}
    for index, project in enumerate(projects_value):
        fail(isinstance(project, dict), f"generated profile lock project {index} is invalid")
        project_path = project.get("path", project.get("name"))
        revision = project.get("revision")
        fail(
            isinstance(project_path, str)
            and project_path
            and project_path not in revisions
            and isinstance(revision, str)
            and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
            f"generated profile lock project {index} has invalid path/revision",
        )
        revisions[project_path] = revision
    return revisions


def verify_generated_module_revisions(
    rows: list[dict[str, Any]], revisions: dict[str, str]
) -> None:
    """Bind every published integration row to the final generated lock."""
    for row in rows:
        module = row["module"]
        fail(
            revisions.get(module) == row["integration_oid"],
            f"{module}: final generated lock revision differs from "
            f"refs/heads/integration/{row['integration_profile']}",
        )


def parse_porcelain(raw: bytes) -> list[tuple[str, str]]:
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        fail(len(item) >= 4 and item[2:3] == b" ", f"malformed porcelain entry: {item!r}")
        entries.append((item[:2].decode(), item[3:].decode()))
    return entries


def allowed_generated_status(entries: list[tuple[str, str]], expected: list[tuple[str, str]]) -> bool:
    """Allow exactly the generated locks declared by the typed dependency graph."""
    return len(entries) == len(expected) and set(entries) == {(" M", path) for _profile, path in expected}


def generated_lock_evidence(workspace: Path, expected: list[tuple[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for profile, relative in expected:
        path = workspace / relative
        fail(path.is_file() and not path.is_symlink(), f"generated profile lock is not a regular file: {relative}")
        value = path.read_bytes()
        fail(len(value) <= MAX_GENERATED_LOCK_BYTES, f"generated profile lock is too large: {relative}")
        try:
            fail(isinstance(yaml.safe_load(value), dict), f"generated profile lock is not YAML mapping: {relative}")
        except yaml.YAMLError as error:
            raise AcceptanceError(f"generated profile lock is invalid YAML: {relative}: {error}") from error
        rows.append({"profile": profile, "path": relative, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()})
    return rows


def parent_clean(repo: Path, nested: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    approved = []
    for xy, path in parse_porcelain(git_raw(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")):
        child = nested.get(path.rstrip("/"))
        if xy == " M" and child and git(repo, "ls-files", "-s", "--", path).startswith("160000 "):
            approved.append({"xy": xy, "path": path, "kind": "modified_gitlink"}); continue
        if xy == "??" and path.endswith("/") and child and child["path"].is_dir() and not child["path"].is_symlink() and git(child["path"], "rev-parse", "--show-toplevel") == str(child["path"]) and git(child["path"], "status", "--porcelain") == "":
            assert_clean_odb(child["path"]); approved.append({"xy": xy, "path": path, "kind": "untracked_nested_repo"}); continue
        raise AcceptanceError(f"dirty parent entry: {(xy, path)}")
    return approved


def capture(workspace: Path, profile: str, modules_path: Path, manifest_path: Path) -> None:
    top = Path(command(workspace, "west", "topdir"))
    available = projects(workspace, top)
    module_profiles = composed_project_profiles(workspace, profile, available)
    # The run is only trustworthy if every materialized project has an
    # independent complete object database and the production workspace is
    # clean, not merely the modules that happen to receive mbox patches.
    expected_generated = generated_lock_paths(workspace, profile)
    status_raw = git_raw(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = parse_porcelain(status_raw)
    frozen = workspace / "west.lock.yml"
    # git_raw deliberately converts a missing/broken Git object lookup into
    # AcceptanceError; do not turn that failure into a false cleanliness bit.
    frozen_ok = frozen.read_bytes() == git_raw(workspace, "show", "HEAD:west.lock.yml")
    generated_rows: list[dict[str, Any]] = []
    generated_error: str | None = None
    try:
        generated_rows = generated_lock_evidence(workspace, expected_generated)
    except AcceptanceError as error:
        generated_error = str(error)
    diagnostic = {"phase": "capture", "manifest_status_entries": [{"xy": xy, "path": path} for xy, path in entries], "expected_generated_locks": [{"profile": item, "path": path} for item, path in expected_generated], "generated_profile_locks": generated_rows, "generated_lock_error": generated_error, "frozen_lock_unchanged": frozen_ok}
    (manifest_path.parent / "capture-diagnostics.json").write_text(json.dumps(diagnostic, sort_keys=True, indent=2) + "\n")
    if generated_error is not None:
        raise AcceptanceError(generated_error)
    fail(allowed_generated_status(entries, expected_generated), f"manifest repo has invalid changes: {entries}")
    fail(frozen_ok, "frozen root manifest changed")
    validated_parents = {}
    for project in available.values():
        repo = project["path"]
        assert_clean_odb(repo)
        nested = {str(p["path"].relative_to(repo)): p for p in available.values() if p["path"] != repo and p["path"].is_relative_to(repo)}
        if repo != workspace and nested:
            validated_parents[str(repo.relative_to(top))] = parent_clean(repo, nested)
        elif repo != workspace:
            fail(git(repo, "status", "--porcelain", "--ignore-submodules=none") == "", f"dirty workspace project: {repo}")
    rows = []
    target_revisions = generated_lock_revisions(workspace / expected_generated[-1][1])
    for module in sorted(module_profiles):
        project = available[module]
        repo = project["path"]
        status = git(repo, "status", "--porcelain", "--ignore-submodules=none")
        relative = str(repo.relative_to(top))
        if relative in validated_parents:
            status = ""
        integration_profile = module_profiles[module]
        ref = f"refs/heads/integration/{integration_profile}"
        integration_oid = git(repo, "rev-parse", ref)
        rows.append({
            "module": module,
            "west_name": project["name"],
            "path": relative,
            "integration_profile": integration_profile,
            "integration_oid": integration_oid,
            "tree": git(repo, "rev-parse", f"{ref}^{{tree}}"),
            "status": status,
        })
    verify_generated_module_revisions(rows, target_revisions)
    modules_path.write_text(json.dumps({"profile": profile, "modules": rows}, sort_keys=True, indent=2) + "\n")
    manifest_path.write_text(json.dumps({
        "workspace_commit": git(workspace, "rev-parse", "HEAD"),
        "frozen_manifest_sha256": hashlib.sha256((workspace / "west.lock.yml").read_bytes()).hexdigest(),
        "generated_profile_locks": generated_rows,
        "validated_nested_children": validated_parents,
    }, sort_keys=True, indent=2) + "\n")


def load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"invalid evidence {path}: {error}") from error


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def assert_no_transaction_state(workspace: Path) -> None:
    for project in projects(workspace).values():
        repo = project["path"]
        refs = git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/west/patch-stack-materialize/",
            "refs/west/patch-stack-results/",
            "refs/west/patch-stack-lock-first/",
        )
        fail(not refs, f"{repo}: transaction refs remain: {refs}")
        worktrees = git(repo, "worktree", "list", "--porcelain")
        fail(
            "west-lock-materialize-" not in worktrees
            and "west-patch-lock-first-" not in worktrees,
            f"{repo}: disposable worktree remains",
        )


def stage(source: Path, artifact: Path) -> None:
    artifact.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and path.name in ARTIFACT_ALLOWLIST:
            shutil.copy2(path, artifact / path.name)
    entries = list(artifact.iterdir())
    names = {path.name for path in entries if path.is_file()}
    forbidden = {path.name for path in entries if not path.is_file()} | (names - ARTIFACT_ALLOWLIST)
    fail(not forbidden, f"forbidden artifact files: {sorted(forbidden)}")
    size = sum(path.stat().st_size for path in entries)
    fail(size <= MAX_ARTIFACT_BYTES, f"artifact too large: {size}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--workspace", type=Path, required=True)
    capture_parser.add_argument("--profile", required=True)
    capture_parser.add_argument("--modules", type=Path, required=True)
    capture_parser.add_argument("--manifest", type=Path, required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--source", type=Path, required=True)
    stage_parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "capture":
            capture(args.workspace, args.profile, args.modules, args.manifest)
        else:
            stage(args.source, args.artifact)
    except AcceptanceError as error:
        print(f"patch-stack acceptance: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
