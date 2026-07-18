"""Read-only validation for one canonical Git patch-stack lock.

This module deliberately never fetches, updates refs, or changes the index or
working tree.  It is usable directly by tests and through ``west patch
preflight``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
EXIT_VALID, EXIT_INVALID, EXIT_MISSING, EXIT_DIRTY, EXIT_TOOL = range(0, 5)
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "patch-stack-lock-v1.schema.json"


class GitToolError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    run = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return run.returncode, run.stdout.strip(), run.stderr.strip()


def _check(name: str, status: str, observed: Any = None, expected: Any = None, detail: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "observed": observed, "expected": expected, "detail": detail}


def _exists(repo: Path, oid: str) -> bool:
    rc, _, stderr = _git(repo, "cat-file", "-e", f"{oid}^{{commit}}")
    if rc == 0:
        return True
    # Depending on the Git version, a missing object is reported either as
    # status 1 or status 128 with this specific diagnostic. Do not turn any
    # other Git/tool failure into a fabricated "missing object" result.
    if rc == 1 or (rc == 128 and "not a valid object name" in stderr.lower()):
        return False
    raise GitToolError(f"git cat-file -e {oid} failed ({rc}): {stderr}")


def _required_git(repo: Path, *args: str) -> str:
    rc, stdout, stderr = _git(repo, *args)
    if rc:
        raise GitToolError(f"git {' '.join(args)} failed ({rc}): {stderr}")
    return stdout


def load_lock(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text())
    # Validate the checked-in v1 JSON Schema without a runtime dependency.
    # This mirrors its closed-object, required-field, const and SHA patterns.
    json.loads(SCHEMA_PATH.read_text())
    required = {"schema_version", "project", "upstream", "mirror", "source_commit", "ordered_commits", "expected_tree"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("lock does not satisfy patch-stack-lock-v1 schema")
    oid = re.compile(r"^[0-9a-f]{40}$")
    if not all(oid.fullmatch(value[key]) for key in ("source_commit", "expected_tree")) or not all(oid.fullmatch(item) for item in value["ordered_commits"]):
        raise ValueError("commit and tree OIDs must be full lowercase SHA-1")
    if not oid.fullmatch(value["upstream"]["base_commit"]) or not oid.fullmatch(value["mirror"]["immutable_oid"]):
        raise ValueError("commit and tree OIDs must be full lowercase SHA-1")
    expected_ref = f"refs/patch-stack/v1/sources/{value['source_commit']}"
    if value["mirror"]["immutable_ref"] != expected_ref:
        raise ValueError(f"mirror.immutable_ref must equal {expected_ref}")
    if value["mirror"]["immutable_oid"] != value["source_commit"]:
        raise ValueError("mirror.immutable_oid must equal source_commit")
    if not isinstance(value["project"], dict) or not isinstance(value["upstream"], dict) or not isinstance(value["mirror"], dict):
        raise ValueError("project, upstream, and mirror must be mappings")
    for key in ("name", "path"):
        if not isinstance(value["project"].get(key), str) or not value["project"][key]:
            raise ValueError(f"project.{key} must be a non-empty string")
    for section, keys in (("upstream", ("url", "base_commit")), ("mirror", ("url", "immutable_ref", "immutable_oid"))):
        for key in keys:
            if not isinstance(value[section].get(key), str) or not value[section][key]:
                raise ValueError(f"{section}.{key} must be a non-empty string")
    if not all(isinstance(value[k], str) and value[k] for k in ("source_commit", "expected_tree")):
        raise ValueError("source_commit and expected_tree must be non-empty strings")
    if not isinstance(value["ordered_commits"], list) or not value["ordered_commits"] or not all(isinstance(x, str) and x for x in value["ordered_commits"]):
        raise ValueError("ordered_commits must be a non-empty string list")
    return value


def inspect(repo: Path, lock_path: Path) -> dict[str, Any]:
    try:
        return _inspect(repo, lock_path)
    except (GitToolError, OSError) as exc:
        inputs = {"repo": str(repo), "lock": str(lock_path), "schema_version": SCHEMA_VERSION}
        return _result(inputs, None, [_check("git_tool", "FAIL", detail=str(exc))], "ERROR", EXIT_TOOL)


def _inspect(repo: Path, lock_path: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    inputs = {"repo": str(repo), "lock": str(lock_path), "schema_version": SCHEMA_VERSION}
    try:
        lock = load_lock(lock_path)
        checks.append(_check("lock_schema", "PASS"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(_check("lock_schema", "FAIL", detail=str(exc)))
        return _result(inputs, None, checks, "ERROR", EXIT_TOOL)
    except Exception as exc:
        checks.append(_check("lock_schema", "FAIL", detail=str(exc)))
        return _result(inputs, None, checks, "INVALID", EXIT_INVALID)
    try:
        if not repo.is_dir() or _git(repo, "rev-parse", "--git-dir")[0] != 0:
            checks.append(_check("repository", "FAIL", detail="not a Git repository"))
            return _result(inputs, lock, checks, "ERROR", EXIT_TOOL)
    except OSError as exc:
        checks.append(_check("repository", "FAIL", detail=str(exc)))
        return _result(inputs, lock, checks, "ERROR", EXIT_TOOL)
    head = _required_git(repo, "rev-parse", "HEAD")
    rc_ref, branch, ref_err = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if rc_ref not in (0, 1):
        raise GitToolError(f"git symbolic-ref --short -q HEAD failed ({rc_ref}): {ref_err}")
    checks.append(_check("head", "PASS", head, detail=branch if rc_ref == 0 else "detached"))
    dirty = _required_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    checks.append(_check("worktree_clean", "PASS" if not dirty else "FAIL", dirty.splitlines(), [], "tracked and untracked files are both checked"))
    alternates_path = Path(_required_git(repo, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates_path.is_absolute():
        alternates_path = repo / alternates_path
    checks.append(_check("alternates", "FAIL" if alternates_path.exists() and alternates_path.read_text().strip() else "PASS"))
    shallow = _required_git(repo, "rev-parse", "--is-shallow-repository") == "true"
    config_rc, config_value, config_err = _git(repo, "config", "--get", "extensions.partialClone")
    if config_rc not in (0, 1):
        raise GitToolError(f"git config --get extensions.partialClone failed ({config_rc}): {config_err}")
    partial = bool(config_value)
    checks.append(_check("shallow_clone", "FAIL" if shallow else "PASS", shallow))
    checks.append(_check("partial_clone", "FAIL" if partial else "PASS", partial))
    expected_path = Path(lock["project"]["path"])
    checks.append(_check("project_path", "PASS" if repo.resolve() == expected_path.resolve() else "FAIL", str(repo.resolve()), str(expected_path.resolve())))
    declared = [lock["upstream"]["base_commit"], lock["source_commit"], *lock["ordered_commits"], lock["mirror"]["immutable_oid"]]
    missing = [oid for oid in dict.fromkeys(declared) if not _exists(repo, oid)]
    checks.append(_check("declared_objects", "PASS" if not missing else "UNKNOWN", missing, [], "no fetch attempted"))
    if missing:
        return _result(inputs, lock, checks, "INCOMPLETE", EXIT_MISSING)
    ref = lock["mirror"]["immutable_ref"]
    rc, resolved, ref_err = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if rc not in (0, 128):
        raise GitToolError(f"git rev-parse immutable ref failed ({rc}): {ref_err}")
    checks.append(_check("immutable_ref", "PASS" if rc == 0 and resolved == lock["mirror"]["immutable_oid"] else ("INCOMPLETE" if rc else "FAIL"), resolved if rc == 0 else None, lock["mirror"]["immutable_oid"]))
    ordered = lock["ordered_commits"]
    linear = ordered[-1] == lock["source_commit"] and _required_git(repo, "rev-list", "--count", f"{lock['upstream']['base_commit']}..{lock['source_commit']}") == str(len(ordered))
    for parent, child in zip([lock["upstream"]["base_commit"], *ordered], ordered):
        parents = _required_git(repo, "show", "-s", "--format=%P", child)
        linear = linear and parents == parent
    merges = [oid for oid in ordered if len(_required_git(repo, "show", "-s", "--format=%P", oid).split()) != 1]
    checks.append(_check("linear_ordered_commits", "PASS" if linear and not merges else "FAIL", {"count": len(ordered), "merges": merges}, {"source": lock["source_commit"]}))
    tree = _required_git(repo, "rev-parse", f"{lock['source_commit']}^{{tree}}")
    checks.append(_check("expected_tree", "PASS" if tree == lock["expected_tree"] else "FAIL", tree, lock["expected_tree"]))
    metadata = []
    for oid in ordered:
        value = _required_git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI", oid)
        metadata.append({"commit": oid, "complete": all(value.split("\x00"))})
    checks.append(_check("author_committer_metadata", "PASS" if all(x["complete"] for x in metadata) else "FAIL", metadata))
    incomplete = [c for c in checks if c["status"] in {"UNKNOWN", "INCOMPLETE"}]
    bad = [c for c in checks if c["status"] == "FAIL"]
    dirty_bad = any(c["name"] == "worktree_clean" and c["status"] == "FAIL" for c in checks)
    if incomplete:
        return _result(inputs, lock, checks, "INCOMPLETE", EXIT_MISSING)
    return _result(inputs, lock, checks, "VALID" if not bad else "INVALID", EXIT_DIRTY if dirty_bad and len(bad) == 1 else (EXIT_INVALID if bad else EXIT_VALID))


def _result(inputs: dict[str, Any], lock: dict[str, Any] | None, checks: list[dict[str, Any]], verdict: str, code: int) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "inputs": inputs, "lock": lock, "checks": checks, "overall_verdict": verdict, "next_safe_action": "proceed only when VALID; otherwise inspect evidence or repair explicitly outside this command", "exit_code": code}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only canonical patch-stack preflight")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inspect(args.repo, args.lock)
    except Exception as exc:
        result = _result({"repo": str(args.repo), "lock": str(args.lock), "schema_version": SCHEMA_VERSION}, None, [_check("internal", "FAIL", detail=str(exc))], "ERROR", EXIT_TOOL)
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print(f"patch-stack preflight: {result['overall_verdict']}")
        for check in result["checks"]:
            print(f"{check['status']:7} {check['name']} {check['detail']}")
        print(f"next safe action: {result['next_safe_action']}")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
