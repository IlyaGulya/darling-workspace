"""Typed, opt-in canonical replacement for one legacy patch application.

This module deliberately contains no profile or patch-name literals.  The
allowlist is data in ``locks/patch-stack/lock-first-series-v1.yml`` and is
validated before the normal patch lifecycle mutates a worktree.
"""
from __future__ import annotations

import shutil
import os
import subprocess
import tempfile
import uuid
import json
import re
from pathlib import Path
from typing import Any

import patch_stack_materialize
import patch_stack_shadow


class LockFirstError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "locks" / "patch-stack" / "lock-first-series-v1.yml"


def _cherry_pick(repo: Path, commit: str) -> None:
    """Replay an immutable commit through Git's native mbox machinery.

    The input remains the declared immutable commit; the temporary mbox is
    generated locally with ``format-patch``.  This intentionally shares the
    exact message parsing and whitespace behavior of the legacy ``git am``
    oracle without reading a legacy patch archive.
    """
    mbox = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=tempfile.gettempdir(), delete=False) as stream:
            mbox = stream.name
            generated = subprocess.run(
                ["git", "format-patch", "--stdout", "--no-stat", "--full-index", f"{commit}^!"],
                cwd=repo, stdout=stream, stderr=subprocess.PIPE,
            )
        if generated.returncode:
            raise LockFirstError(f"git format-patch {commit} failed ({generated.returncode}): {generated.stderr.decode().strip()}")
        result = subprocess.run(
            ["git", "am", "--3way", "--committer-date-is-author-date", mbox], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise LockFirstError(f"git am for immutable {commit} failed ({result.returncode}): {result.stderr.strip()}")
    finally:
        if mbox is not None:
            Path(mbox).unlink(missing_ok=True)


def plan(profile: str, patches: list[dict[str, Any]], mapping_path: Path = MAPPING) -> list[dict[str, str]]:
    """Return an ordered, uniquely matched batch before any mutation."""
    try:
        entries = [entry for entry in patch_stack_shadow._mapping(mapping_path) if entry["profile"] == profile]
    except patch_stack_shadow.ShadowError as error:
        raise LockFirstError(str(error)) from error
    if not entries:
        raise LockFirstError(f"{profile}: requires at least one typed lock-first entry")
    seen: set[tuple[str, str]] = set()
    positions: list[int] = []
    locks_root = mapping_path.parent.resolve()
    resolved: list[dict[str, str]] = []
    for entry in entries:
        key = (entry["module"], entry["patch"])
        if key in seen:
            raise LockFirstError(f"{profile}: duplicate typed lock-first entry: {entry['patch']}")
        seen.add(key)
        matches = [index for index, patch in enumerate(patches) if (patch.get("module"), patch.get("path")) == key]
        if len(matches) != 1:
            raise LockFirstError(f"{profile}: allowlisted lock-first patch must occur exactly once: {entry['patch']}")
        positions.append(matches[0])
        relative = Path(entry["lock"])
        if relative.is_absolute() or ".." in relative.parts:
            raise LockFirstError("lock-first lock must be a contained relative path")
        candidate = locks_root / relative
        lock_path = candidate.resolve()
        if locks_root not in lock_path.parents or candidate.is_symlink() or not lock_path.is_file():
            raise LockFirstError("lock-first lock escapes locks/patch-stack")
        try:
            lock = patch_stack_materialize.load_lock(lock_path)
        except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
            raise LockFirstError(f"{entry['patch']}: invalid immutable lock: {error}") from error
        if lock["source_commit"] != lock["mirror"]["source_oid"] or lock["upstream"]["base_commit"] != lock["mirror"]["base_oid"]:
            raise LockFirstError(f"{entry['patch']}: incompatible immutable lock")
        resolved.append({**entry, "lock_path": str(lock_path), "profile_index": str(matches[0])})
    if positions != sorted(positions):
        raise LockFirstError(f"{profile}: lock-first entries are not in profile order")
    return resolved


_OID = re.compile(r"^[0-9a-f]{40}$")
_EVIDENCE_FIELDS = {"patch", "base", "source", "canonical_tree", "applied_commit", "applied_tree", "verdict"}


def write_batch_evidence(path: Path, results: list[dict[str, Any]], expected_patches: list[str]) -> None:
    """Publish aggregate per-series evidence atomically, or leave no temp file."""
    if path.exists() or path.is_symlink():
        raise LockFirstError("lock-first evidence output already exists")
    if not isinstance(expected_patches, list) or not expected_patches:
        raise LockFirstError("lock-first evidence requires a non-empty expected batch")
    if any(not isinstance(patch, str) or not patch for patch in expected_patches) or len(set(expected_patches)) != len(expected_patches):
        raise LockFirstError("lock-first evidence expected patches are invalid or duplicate")
    if not isinstance(results, list) or not results:
        raise LockFirstError("lock-first evidence requires a non-empty result batch")
    if any(not isinstance(entry, dict) for entry in results):
        raise LockFirstError("lock-first evidence entry is not an object")
    if [entry.get("patch") for entry in results] != expected_patches:
        raise LockFirstError("lock-first evidence patches do not exactly match the ordered batch")
    seen: set[str] = set()
    for entry in results:
        if set(entry) != _EVIDENCE_FIELDS or entry["patch"] in seen:
            raise LockFirstError("lock-first evidence entry has invalid fields or duplicate patch")
        seen.add(entry["patch"])
        if entry["verdict"] != "VALID" or not all(
            isinstance(entry[key], str) and _OID.fullmatch(entry[key])
            for key in ("base", "source", "canonical_tree", "applied_commit", "applied_tree")
        ):
            raise LockFirstError("lock-first evidence entry has invalid OID or verdict")
    payload = {"verdict": "VALID", "series": results}
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        temporary.replace(path)
    except Exception as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Preserve the original publication failure.  There can be no
            # transaction artifact below a non-directory parent.
            pass
        raise LockFirstError(f"lock-first evidence write failed: {error}") from error


def materialize_into(
    repo: Path,
    lock_first_plan: dict[str, str],
    legacy_patch: Path,
    oracle_evidence: Path | None = None,
) -> dict[str, Any]:
    """Verify both canonical and legacy inputs, then advance ``repo`` to source.

    ``patch_stack_materialize`` proves the immutable graph in this repository.
    The independent shadow runner is the legacy-mbox equivalence oracle; it
    never accesses the integration worktree.  The temporary result ref is
    removed before returning, so lock-first leaves no canonical publication
    state behind in a production checkout.
    """
    lock_path = Path(lock_first_plan["lock_path"])
    lock = patch_stack_materialize.load_lock(lock_path)
    transaction = uuid.uuid4().hex
    result_ref = f"refs/west/patch-stack-results/lock-first/{transaction}"
    fetched_ref = f"refs/west/patch-stack-lock-first/{transaction}"
    evidence = oracle_evidence or Path(tempfile.gettempdir()) / f"west-patch-lock-first-{transaction}.json"
    source: str | None = None
    root = Path(tempfile.gettempdir()) / f"west-patch-lock-first-{transaction}"
    try:
        oracle = patch_stack_shadow.run_shadow(
            shadow_plan=lock_first_plan,
            legacy_patch=legacy_patch,
            evidence_path=evidence,
        )
        # The production Darling parent can legitimately report an untracked
        # nested West project.  Do not weaken materializer cleanliness for
        # that layout: validate in a fresh independent object database.
        root.mkdir()
        canonical = root / "canonical"
        patch_stack_materialize._git(root, "init", "-q", str(canonical))
        patch_stack_materialize._git(canonical, "remote", "add", "immutable", lock["mirror"]["url"])
        patch_stack_materialize._git(
            canonical, "fetch", "--no-tags", "immutable",
            f"{lock['mirror']['base_ref']}:refs/heads/seed",
        )
        patch_stack_materialize._git(canonical, "checkout", "--detach", "refs/heads/seed")
        result = patch_stack_materialize.materialize(
            canonical, lock_path, result_ref=result_ref,
        )
        source = result["fetched"]["source_oid"]
        if result["verdict"] != "VALID" or result["resulting_tree"] != oracle["legacy_resulting_tree"]:
            raise LockFirstError("canonical materialization and legacy oracle differ")
        patch_stack_materialize._git(
            repo, "fetch", "--no-tags", "--no-recurse-submodules", lock["mirror"]["url"],
            f"{lock['mirror']['source_ref']}:{fetched_ref}",
        )
        if patch_stack_materialize._oid(repo, fetched_ref) != source:
            raise LockFirstError("production immutable source fetch differs from validated source")
        # The profile can intentionally apply a retained patch onto a newer
        # effective base than the lock's historical upstream base.  Preserve
        # that lifecycle by replaying the immutable, validated commits with
        # ordinary Git rather than resetting the production checkout to the
        # historical source tip (which would discard intervening upstream).
        for commit in result["ordered_commits"]:
            _cherry_pick(repo, commit)
        return {
            "patch": lock_first_plan["patch"],
            "base": lock["upstream"]["base_commit"],
            "source": source,
            "canonical_tree": result["resulting_tree"],
            "applied_commit": patch_stack_materialize._oid(repo, "HEAD"),
            "applied_tree": patch_stack_materialize._git(repo, "rev-parse", "HEAD^{tree}"),
            "verdict": "VALID",
            "oracle": oracle,
        }
    except (patch_stack_shadow.ShadowError, patch_stack_materialize.MaterializeError) as error:
        raise LockFirstError(str(error)) from error
    finally:
        # Never remove an existing/concurrently replaced ref: only delete the
        # exact transaction result created by this invocation.
        try:
            if source is not None:
                outcome = patch_stack_materialize._rollback_result(root / "canonical", result_ref, source)
                if outcome not in ("removed", "already-absent"):
                    raise LockFirstError(f"lock-first result-ref cleanup failed: {outcome}")
            patch_stack_materialize._delete_ref(repo, fetched_ref)
            if root.exists():
                shutil.rmtree(root)
        finally:
            if oracle_evidence is None:
                evidence.unlink(missing_ok=True)
