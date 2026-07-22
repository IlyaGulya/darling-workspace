"""Typed, opt-in canonical materialization for an ordered legacy batch.

This module deliberately contains no profile or patch-name literals.  The
allowlist is data in ``locks/patch-stack/lock-first-series-v2.yml`` and is
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
import yaml


class LockFirstError(RuntimeError):
    pass


class LockFirstPlan(list[dict[str, str]]):
    """Ordered plan carrying the typed batch identity used for evidence."""

    def __init__(self, entries: list[dict[str, str]], metadata: dict[str, Any]):
        super().__init__(entries)
        series = [{"module": entry["module"], "patch": entry["patch"]} for entry in entries]
        self.batch = {
            "batch_id": metadata["batch_id"],
            "expected_count": metadata["expected_count"],
            "series_order": series,
            "module_order": list(dict.fromkeys(entry["module"] for entry in entries)),
        }


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "locks" / "patch-stack" / "lock-first-series-v2.yml"


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


_MAPPING_FIELDS = {"schema_version", "profile", "batch_id", "expected_count", "series"}
_SERIES_FIELDS = {"profile", "module", "patch", "lock"}


def migrate_mapping_v1(data: object, *, batch_id: str = "migrated-v1") -> dict[str, Any]:
    """Return the explicit schema-v2 form of a legacy single-profile mapping.

    Runtime use is schema-v2 only; this small, pure helper makes migration
    reviewable and lets contracts prove that no count is embedded in code.
    """
    if (not isinstance(data, dict) or set(data) != {"schema_version", "series"}
            or data.get("schema_version") != 1 or not isinstance(data.get("series"), list)):
        raise LockFirstError("lock-first v1 mapping is malformed")
    if not isinstance(batch_id, str) or not batch_id:
        raise LockFirstError("lock-first migration batch_id is invalid")
    series = data["series"]
    if not series:
        raise LockFirstError("lock-first v1 mapping must contain entries")
    profiles: set[str] = set()
    for index, entry in enumerate(series):
        if not isinstance(entry, dict) or set(entry) != _SERIES_FIELDS:
            raise LockFirstError(f"lock-first v1 mapping entry {index} is malformed")
        if not all(isinstance(entry[field], str) and entry[field] for field in _SERIES_FIELDS):
            raise LockFirstError(f"lock-first v1 mapping entry {index} has an empty scalar")
        profiles.add(entry["profile"])
    if len(profiles) != 1:
        raise LockFirstError("lock-first v1 mapping must contain one non-empty profile")
    return {"schema_version": 2, "profile": profiles.pop(), "batch_id": batch_id,
            "expected_count": len(series), "series": series}


def load_mapping(mapping_path: Path = MAPPING, profile: str | None = None) -> dict[str, Any]:
    try:
        data = yaml.safe_load(mapping_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise LockFirstError(f"invalid lock-first mapping: {error}") from error
    if not isinstance(data, dict) or set(data) != _MAPPING_FIELDS or data.get("schema_version") != 2:
        raise LockFirstError("lock-first mapping must use exact schema_version 2")
    mapping_profile, batch_id, expected_count, series = (data["profile"], data["batch_id"], data["expected_count"], data["series"])
    if not isinstance(mapping_profile, str) or not mapping_profile or not isinstance(batch_id, str) or not batch_id:
        raise LockFirstError("lock-first mapping profile or batch_id is invalid")
    if profile is not None and mapping_profile != profile:
        raise LockFirstError(f"lock-first mapping profile differs: {mapping_profile}")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count < 1:
        raise LockFirstError("lock-first mapping expected_count is invalid")
    if not isinstance(series, list) or expected_count != len(series):
        raise LockFirstError("lock-first mapping expected_count differs from series length")
    for index, entry in enumerate(series):
        if not isinstance(entry, dict) or set(entry) != _SERIES_FIELDS or entry.get("profile") != mapping_profile:
            raise LockFirstError(f"lock-first mapping entry {index} is invalid")
        if not all(isinstance(entry[field], str) and entry[field] for field in _SERIES_FIELDS):
            raise LockFirstError(f"lock-first mapping entry {index} has an empty scalar")
    return data


def plan(
    profile: str,
    patches: list[dict[str, Any]],
    mapping_path: Path = MAPPING,
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> LockFirstPlan:
    """Return an ordered, uniquely matched batch before any mutation."""
    metadata = load_mapping(mapping_path, profile)
    entries = metadata["series"]
    seen: set[tuple[str, str]] = set()
    if grouped is None:
        grouped = {}
        for patch in patches:
            grouped.setdefault(patch["module"], []).append(patch)
    execution_order = [
        (module, patch["path"])
        for module, module_patches in grouped.items()
        for patch in module_patches
    ]
    positions: list[int] = []
    locks_root = mapping_path.parent.resolve()
    for component in (mapping_path.parent.absolute(),):
        if component.is_symlink():
            raise LockFirstError("lock-first mapping root may not be a symlink")
    resolved: list[dict[str, str]] = []
    resolved_locks: set[Path] = set()
    for entry in entries:
        key = (entry["module"], entry["patch"])
        if key in seen:
            raise LockFirstError(f"{profile}: duplicate typed lock-first entry: {entry['patch']}")
        seen.add(key)
        matches = [index for index, candidate in enumerate(execution_order) if candidate == key]
        if len(matches) != 1:
            raise LockFirstError(f"{profile}: allowlisted lock-first patch must occur exactly once: {entry['patch']}")
        positions.append(matches[0])
        relative = Path(entry["lock"])
        if relative.is_absolute() or ".." in relative.parts:
            raise LockFirstError("lock-first lock must be a contained relative path")
        candidate = locks_root / relative
        current = locks_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise LockFirstError("lock-first lock may not contain a symlink")
        lock_path = candidate.resolve()
        if locks_root not in lock_path.parents or candidate.is_symlink() or not lock_path.is_file():
            raise LockFirstError("lock-first lock escapes locks/patch-stack")
        if lock_path in resolved_locks:
            raise LockFirstError("lock-first mapping resolves duplicate lock files")
        resolved_locks.add(lock_path)
        try:
            lock = patch_stack_materialize.load_lock(lock_path)
        except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
            raise LockFirstError(f"{entry['patch']}: invalid immutable lock: {error}") from error
        if lock["source_commit"] != lock["mirror"]["source_oid"] or lock["upstream"]["base_commit"] != lock["mirror"]["base_oid"]:
            raise LockFirstError(f"{entry['patch']}: incompatible immutable lock")
        resolved.append({**entry, "lock_path": str(lock_path), "execution_index": str(matches[0])})
    if positions != sorted(positions):
        raise LockFirstError(f"{profile}: lock-first entries are not in grouped execution order")
    return LockFirstPlan(resolved, metadata)


_OID = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_SCHEMA_VERSION = 2
_EVIDENCE_FIELDS = {"module", "patch", "base", "source", "canonical_tree", "applied_commit", "applied_tree", "verdict"}


def write_batch_evidence(path: Path, results: list[dict[str, Any]], batch: dict[str, Any]) -> None:
    """Publish aggregate per-series evidence atomically, or leave no temp file."""
    if path.exists() or path.is_symlink():
        raise LockFirstError("lock-first evidence output already exists")
    if not isinstance(batch, dict):
        raise LockFirstError("lock-first evidence requires typed batch metadata")
    batch_id, expected_count, expected_series, module_order = (
        batch.get("batch_id"), batch.get("expected_count"), batch.get("series_order"), batch.get("module_order")
    )
    if not isinstance(batch_id, str) or not batch_id:
        raise LockFirstError("lock-first evidence batch_id is invalid")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count < 1:
        raise LockFirstError("lock-first evidence expected_count is invalid")
    if not isinstance(expected_series, list) or expected_count != len(expected_series):
        raise LockFirstError("lock-first evidence expected_count differs from ordered batch")
    if not isinstance(module_order, list) or not module_order or any(not isinstance(module, str) or not module for module in module_order):
        raise LockFirstError("lock-first evidence module order is invalid")
    if any(not isinstance(entry, dict) or set(entry) != {"module", "patch"} or not all(isinstance(entry.get(field), str) and entry[field] for field in ("module", "patch")) for entry in expected_series):
        raise LockFirstError("lock-first evidence expected series are invalid")
    expected_keys = [(entry["module"], entry["patch"]) for entry in expected_series]
    if len(set(expected_keys)) != len(expected_keys) or list(dict.fromkeys(module for module, _ in expected_keys)) != module_order:
        raise LockFirstError("lock-first evidence expected series are duplicate or have invalid module order")
    if not isinstance(results, list) or not results:
        raise LockFirstError("lock-first evidence requires a non-empty result batch")
    if any(not isinstance(entry, dict) for entry in results):
        raise LockFirstError("lock-first evidence entry is not an object")
    if [(entry.get("module"), entry.get("patch")) for entry in results] != expected_keys:
        raise LockFirstError("lock-first evidence series do not exactly match the grouped ordered batch")
    seen: set[tuple[str, str]] = set()
    for entry in results:
        key = (entry.get("module"), entry.get("patch"))
        if set(entry) != _EVIDENCE_FIELDS or key in seen or not all(isinstance(value, str) and value for value in key):
            raise LockFirstError("lock-first evidence entry has invalid fields or duplicate module+patch")
        seen.add(key)
        if entry["verdict"] != "VALID" or not all(
            isinstance(entry[key], str) and _OID.fullmatch(entry[key])
            for key in ("base", "source", "canonical_tree", "applied_commit", "applied_tree")
        ):
            raise LockFirstError("lock-first evidence entry has invalid OID or verdict")
    payload = {"evidence_schema_version": EVIDENCE_SCHEMA_VERSION, "verdict": "VALID", "batch_id": batch_id, "expected_count": expected_count,
               "module_order": module_order, "series_order": expected_series, "series": results}
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
            "module": lock_first_plan["module"],
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
