#!/usr/bin/env python3
"""Fail-closed evidence helpers for the manual hosted shadow acceptance tier."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


class AcceptanceError(RuntimeError):
    pass


ARTIFACT_ALLOWLIST = {
    "control-manifest.json",
    "control-modules.json",
    "shadow-manifest.json",
    "shadow-modules.json",
    "shadow-evidence.json",
    "acceptance-result.json",
    "cleanup.txt",
    "diagnostics.txt",
    "capture-diagnostics.json",
}
MAX_ARTIFACT_BYTES = 1_000_000
MAX_GENERATED_LOCK_BYTES = 1_000_000


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
    modules = {"darling"} | {item["module"] for item in profile_data["patches"]}
    unknown = modules - available.keys()
    if unknown:
        raise AcceptanceError(f"unknown profile modules: {sorted(unknown)}")
    return modules


def parse_porcelain(raw: bytes) -> list[tuple[str, str]]:
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        fail(len(item) >= 4 and item[2:3] == b" ", f"malformed porcelain entry: {item!r}")
        entries.append((item[:2].decode(), item[3:].decode()))
    return entries


def allowed_generated_status(entries: list[tuple[str, str]], profile: str) -> bool:
    return len(entries) == 1 and entries[0][1] == f"patches/{profile}/west.lock.yml" and entries[0][0] == " M"


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
    profile_data = yaml.safe_load((workspace / "patches" / profile / "patches.yml").read_text())
    top = Path(command(workspace, "west", "topdir"))
    available = projects(workspace, top)
    modules = touched_projects(profile_data, available)
    # The run is only trustworthy if every materialized project has an
    # independent complete object database and the production workspace is
    # clean, not merely the modules that happen to receive mbox patches.
    generated = workspace / "patches" / profile / "west.lock.yml"
    status_raw = git_raw(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = parse_porcelain(status_raw)
    generated_bytes = generated.read_bytes() if generated.is_file() and not generated.is_symlink() else b""
    frozen = workspace / "west.lock.yml"
    # git_raw deliberately converts a missing/broken Git object lookup into
    # AcceptanceError; do not turn that failure into a false cleanliness bit.
    frozen_ok = frozen.read_bytes() == git_raw(workspace, "show", "HEAD:west.lock.yml")
    diagnostic = {"phase": "capture", "manifest_status_entries": [{"xy": xy, "path": path} for xy, path in entries], "generated_lock": {"exists": generated.exists(), "regular_file": generated.is_file() and not generated.is_symlink(), "size": len(generated_bytes), "sha256": hashlib.sha256(generated_bytes).hexdigest() if generated_bytes else None}, "frozen_lock_unchanged": frozen_ok}
    (manifest_path.parent / "capture-diagnostics.json").write_text(json.dumps(diagnostic, sort_keys=True, indent=2) + "\n")
    fail(generated.is_file() and not generated.is_symlink(), "generated profile lock is not a regular file")
    fail(len(generated_bytes) <= MAX_GENERATED_LOCK_BYTES, "generated profile lock is too large")
    try:
        fail(isinstance(yaml.safe_load(generated_bytes), dict), "generated profile lock is not YAML mapping")
    except yaml.YAMLError as error:
        raise AcceptanceError(f"generated profile lock is invalid YAML: {error}") from error
    expected = f"patches/{profile}/west.lock.yml"
    fail(allowed_generated_status(entries, profile), f"manifest repo has invalid changes: {entries}")
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
    for module in sorted(modules):
        project = available[module]
        repo = project["path"]
        status = git(repo, "status", "--porcelain", "--ignore-submodules=none")
        relative = str(repo.relative_to(top))
        if relative in validated_parents:
            status = ""
        ref = f"refs/heads/integration/{profile}"
        rows.append({
            "module": module,
            "west_name": project["name"],
            "path": relative,
            "integration_oid": git(repo, "rev-parse", ref),
            "tree": git(repo, "rev-parse", f"{ref}^{{tree}}"),
            "status": status,
        })
    modules_path.write_text(json.dumps({"profile": profile, "modules": rows}, sort_keys=True, indent=2) + "\n")
    manifest_path.write_text(json.dumps({
        "workspace_commit": git(workspace, "rev-parse", "HEAD"),
        "frozen_manifest_sha256": hashlib.sha256((workspace / "west.lock.yml").read_bytes()).hexdigest(),
        "generated_profile_lock": {"sha256": hashlib.sha256(generated_bytes).hexdigest(), "size": len(generated_bytes)},
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
        refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/west/patch-stack-materialize/", "refs/west/patch-stack-results/")
        fail(not refs, f"{repo}: transaction refs remain: {refs}")
        worktrees = git(repo, "worktree", "list", "--porcelain")
        fail("west-lock-materialize-" not in worktrees and "west-patch-shadow-" not in worktrees, f"{repo}: disposable worktree remains")


def compare(control: Path, shadow: Path, control_manifest: Path, shadow_manifest: Path, evidence: Path, lock_path: Path, control_workspace: Path, shadow_workspace: Path, result: Path) -> None:
    control_map, shadow_map = load(control), load(shadow)
    fail(control_map == shadow_map, "control and shadow module maps differ")
    fail(load(control_manifest) == load(shadow_manifest), "control and shadow frozen manifests differ")
    generated = load(control_manifest).get("generated_profile_lock", {})
    fail(isinstance(generated.get("sha256"), str) and len(generated["sha256"]) == 64 and isinstance(generated.get("size"), int), "generated profile lock evidence is incomplete")
    rows = control_map.get("modules")
    fail(isinstance(rows, list) and rows, "module map is incomplete")
    for row in rows:
        fail(row.get("status") == "", f"dirty module: {row.get('module')}")
        fail(isinstance(row.get("integration_oid"), str) and len(row["integration_oid"]) == 40, "missing integration ref")
        fail(isinstance(row.get("tree"), str) and len(row["tree"]) == 40, "missing resulting tree")
    candidates = list(evidence.parent.glob("shadow-evidence*.json"))
    fail(candidates == [evidence], "shadow evidence is missing or duplicated")
    value = load(evidence)
    lock = yaml.safe_load(lock_path.read_text())
    fail(value.get("verdict") == "VALID", "shadow verdict is not VALID")
    fail(value.get("fetched_legacy_base_oid") == lock["upstream"]["base_commit"], "legacy base OID mismatch")
    fail(value.get("source_oid") == lock["source_commit"], "source OID mismatch")
    fail(value.get("legacy_mbox_ordered_commits") == lock["ordered_commits"], "legacy mbox chain mismatch")
    fail(value.get("legacy_mbox_commit_count") == len(lock["ordered_commits"]), "legacy mbox count mismatch")
    fail(value.get("legacy_resulting_tree") == lock["expected_tree"], "legacy tree mismatch")
    fail(value.get("canonical_resulting_tree") == lock["expected_tree"], "canonical tree mismatch")
    fail(value.get("cleanup", {}).get("root") == "removed", "shadow cleanup was incomplete")
    assert_no_transaction_state(control_workspace)
    assert_no_transaction_state(shadow_workspace)
    result.write_text(json.dumps({"verdict": "VALID", "module_count": len(rows), "shadow_evidence": evidence.name}, sort_keys=True, indent=2) + "\n")


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
    compare_parser = sub.add_parser("compare")
    for name in ("control", "shadow", "control-manifest", "shadow-manifest", "evidence", "lock", "control-workspace", "shadow-workspace", "result"):
        compare_parser.add_argument(f"--{name}", type=Path, required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--source", type=Path, required=True)
    stage_parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "capture":
            capture(args.workspace, args.profile, args.modules, args.manifest)
        elif args.action == "compare":
            compare(args.control, args.shadow, args.control_manifest, args.shadow_manifest, args.evidence, args.lock, args.control_workspace, args.shadow_workspace, args.result)
        else:
            stage(args.source, args.artifact)
    except AcceptanceError as error:
        print(f"patch-stack shadow acceptance: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
