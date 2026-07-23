"""Fail-closed schema-v2 lock materialization into a local result ref."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from patch_stack_preflight import inspect, load_lock


class MaterializeError(RuntimeError):
    pass


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _git(repo: Path, *args: str) -> str:
    result = _run(repo, *args)
    if result.returncode:
        raise MaterializeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _common_dir(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def _oid(repo: Path, revision: str) -> str:
    return _git(repo, "rev-parse", f"{revision}^{{commit}}")


def _ref_state(repo: Path, ref: str) -> bool:
    result = _run(repo, "show-ref", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise MaterializeError(f"git show-ref failed ({result.returncode}): {result.stderr.strip()}")


def _delete_ref(repo: Path, ref: str) -> str:
    result = _run(repo, "update-ref", "-d", ref)
    if result.returncode == 0:
        return "removed"
    if result.returncode == 1:
        return "already-absent"
    else:
        raise MaterializeError(f"git update-ref -d {ref} failed ({result.returncode}): {result.stderr.strip()}")


def _check_clean_shape(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1"):
        raise MaterializeError("worktree is dirty")
    common = _common_dir(repo)
    if (common / "objects" / "info" / "alternates").exists():
        raise MaterializeError("alternates are forbidden")
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise MaterializeError("shallow clone is forbidden")
    partial = _run(repo, "config", "--get", "extensions.partialClone")
    if partial.returncode == 0 and partial.stdout.strip():
        raise MaterializeError("partial clone is forbidden")
    if partial.returncode not in (0, 1):
        raise MaterializeError(f"git config failed ({partial.returncode}): {partial.stderr.strip()}")
    if _git(repo, "replace", "-l"):
        raise MaterializeError("replace objects are forbidden")


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup(repo: Path, worktree: Path | None, root: Path | None, refs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    if worktree is not None:
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree))
            result["worktree"] = "removed"
        except Exception as error:
            result["worktree"] = f"failed: {error}"
    if root is not None and result.get("worktree", "removed") == "removed":
        try:
            if root.exists():
                shutil.rmtree(root)
            result["worktree_directory"] = "removed"
        except Exception as error:
            result["worktree_directory"] = f"failed: {error}"
    elif root is not None:
        result["worktree_directory"] = "skipped: worktree removal failed"
    for ref in refs:
        try:
            result[ref] = _delete_ref(repo, ref)
        except Exception as error:
            result[ref] = f"failed: {error}"
    return result


def _cleanup_ok(cleanup: dict[str, str]) -> bool:
    return bool(cleanup) and all(value in ("removed", "already-absent") for value in cleanup.values())


def _rollback_result(repo: Path, ref: str, source: str) -> str:
    """Delete only the ref this transaction created; never overwrite another result."""
    try:
        if not _ref_state(repo, ref):
            return "already-absent"
        if _oid(repo, ref) != source:
            return "preserved: no longer points at transaction source"
        return _delete_ref(repo, ref)
    except Exception as error:
        return f"failed: {error}"


def _fetchable_preflight(preflight: dict[str, Any]) -> bool:
    """Permit only a clean preflight whose sole gap is fetched immutable input."""
    if preflight["overall_verdict"] == "VALID":
        return True
    if preflight["overall_verdict"] != "INCOMPLETE":
        return False
    allowed = {
        "declared_objects": {"UNKNOWN"},
        "immutable_base_tag": {"INCOMPLETE"},
        "immutable_source_tag": {"INCOMPLETE"},
    }
    saw_incomplete = False
    for check in preflight["checks"]:
        name, status = check["name"], check["status"]
        if status == "PASS":
            continue
        if status in allowed.get(name, set()):
            saw_incomplete = True
            continue
        return False
    return saw_incomplete


def validate_fetched_lock(repo: Path, lock: dict[str, Any], base_ref: str, source_ref: str) -> dict[str, Any]:
    """Validate one schema-v2 lock against immutable refs already fetched.

    Batch callers deliberately fetch a union of immutable refs once per
    repository.  Keeping this graph/metadata/tree proof here prevents that
    optimization from weakening the single-lock materializer's invariants.
    """
    if lock.get("schema_version") != 2:
        raise MaterializeError("materialize-lock accepts schema_version 2 only")
    base, source = _oid(repo, base_ref), _oid(repo, source_ref)
    if base != lock["upstream"]["base_commit"] or source != lock["source_commit"]:
        raise MaterializeError("fetched immutable ref OID differs from lock")
    ordered = _git(repo, "rev-list", "--reverse", f"{base}..{source}").splitlines()
    if ordered != lock["ordered_commits"]:
        raise MaterializeError("ordered commits are not the exact linear range")
    for index, commit in enumerate(ordered):
        parents = _git(repo, "show", "-s", "--format=%P", commit).split()
        if parents != ([base] if index == 0 else [ordered[index - 1]]):
            raise MaterializeError("merge or nonlinear ordered stack")
        metadata = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI", commit).split("\x00")
        if len(metadata) != 6 or not all(metadata):
            raise MaterializeError("incomplete author/committer metadata")
    tree = _git(repo, "show", "-s", "--format=%T", source)
    if tree != lock["expected_tree"]:
        raise MaterializeError("expected tree differs from source commit")
    return {"base_oid": base, "source_oid": source, "ordered_commits": ordered, "resulting_tree": tree}


def materialize(repo: Path, lock_path: Path, result_ref: str | None = None, evidence_path: Path | None = None) -> dict[str, Any]:
    """Materialize a canonical graph without applying mbox patches or touching HEAD.

    The command is transactional: disposable refs/worktree are removed before
    the create-only result ref update. A failed cleanup or evidence write rolls
    back a newly-created result ref by comparing it to the expected source OID.
    """
    repo, lock_path = repo.resolve(), lock_path.resolve()
    try:
        lock_bytes, lock = lock_path.read_bytes(), load_lock(lock_path)
    except (OSError, ValueError) as error:
        raise MaterializeError(f"invalid lock input: {error}") from error
    if lock["schema_version"] != 2:
        raise MaterializeError("materialize-lock accepts schema_version 2 only")
    source = lock["source_commit"]
    result_ref = result_ref or f"refs/west/patch-stack-results/{source}"
    if not result_ref.startswith("refs/west/patch-stack-results/") or _run(repo, "check-ref-format", result_ref).returncode:
        raise MaterializeError("invalid result ref")
    common = _common_dir(repo)
    evidence_path = evidence_path or common / "patch-stack-materialization" / f"{source}.json"
    transaction = uuid.uuid4().hex
    temporary = f"refs/west/patch-stack-materialize/{transaction}"
    base_ref, source_ref = temporary + "/base", temporary + "/source"
    evidence: dict[str, Any] = {"status": "ERROR", "verdict": "ERROR", "transaction_id": transaction,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(), "source_commit": source,
        "ordered_commits": lock["ordered_commits"], "result_ref": result_ref,
        "result_ref_status": "not-created", "fetched": {}, "resulting_tree": None,
        "cleanup": {}, "evidence": "pending"}
    worktree: Path | None = None
    root: Path | None = None
    created_result = False
    primary_error: BaseException | None = None
    try:
        _check_clean_shape(repo)
        if _ref_state(repo, result_ref):
            raise MaterializeError(f"result ref already exists: {result_ref}")
        preflight = inspect(repo, lock_path)
        if not _fetchable_preflight(preflight):
            raise MaterializeError(f"pre-fetch preflight {preflight['overall_verdict']}")
        mirror = lock["mirror"]
        _git(repo, "fetch", "--no-tags", mirror["url"], f"{mirror['base_ref']}:{base_ref}", f"{mirror['source_ref']}:{source_ref}")
        validated = validate_fetched_lock(repo, lock, base_ref, source_ref)
        base, fetched = validated["base_oid"], validated["source_oid"]
        ordered, tree = validated["ordered_commits"], validated["resulting_tree"]
        evidence["fetched"] = {"base_oid": base, "source_oid": fetched}
        # The root is recoverable from transaction_id without globbing over
        # other concurrent materializers' disposable worktrees.
        root = Path(tempfile.gettempdir()) / f"west-lock-materialize-{transaction}"
        root.mkdir()
        worktree = root / "source"
        _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), source_ref)
        if _oid(worktree, "HEAD") != source or _git(worktree, "rev-parse", "HEAD^{tree}") != tree:
            raise MaterializeError("disposable worktree result mismatch")
        evidence["resulting_tree"] = tree
        evidence["cleanup"] = _cleanup(repo, worktree, root, [base_ref, source_ref])
        worktree = None
        root = None
        if not _cleanup_ok(evidence["cleanup"]):
            raise MaterializeError("cleanup failed before result publication")
        try:
            _git(repo, "update-ref", result_ref, source, "")
            created_result = True
        except BaseException as error:
            # Detect an interrupted Git invocation that may have created the
            # ref before Python receives SIGINT. Ordinary create-only failure
            # must not inspect/delete a concurrent result ref.
            if not isinstance(error, Exception):
                created_result = _ref_state(repo, result_ref) and _oid(repo, result_ref) == source
            raise
        evidence["result_ref_status"] = "created"
        evidence["status"] = "VALID"
        evidence["verdict"] = "VALID"
    except BaseException as error:
        primary_error = error
        evidence["error"] = str(error)
        if not evidence["cleanup"]:
            evidence["cleanup"] = _cleanup(repo, worktree, root, [base_ref, source_ref])
            worktree = None
            root = None
        if created_result:
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
            created_result = False
        evidence["verdict"] = "ERROR"
    try:
        if created_result and (not _cleanup_ok(evidence["cleanup"])):
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
        evidence["evidence"] = "written"
        _write_evidence(evidence_path, evidence)
    except Exception as error:
        evidence["evidence"] = f"failed: {error}"
        if created_result:
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
        if primary_error is None:
            primary_error = MaterializeError(f"evidence write failed: {error}")
    if primary_error is not None:
        raise primary_error
    return evidence
