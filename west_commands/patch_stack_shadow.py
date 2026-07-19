"""Opt-in, non-authoritative shadow comparison for one approved patch series."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

import patch_stack_materialize
from patch_stack_preflight import load_lock


class ShadowError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "locks" / "patch-stack" / "shadow-series-v1.yml"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ShadowError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _mapping(path: Path = MAPPING) -> list[dict[str, str]]:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("series"), list):
        raise ShadowError("invalid shadow-series metadata")
    result: list[dict[str, str]] = []
    for item in value["series"]:
        if not isinstance(item, dict) or set(item) != {"profile", "module", "patch", "lock"} or not all(isinstance(item[key], str) and item[key] for key in item):
            raise ShadowError("invalid shadow-series entry")
        result.append(item)
    return result


def plan(profile: str, patches: list[dict[str, Any]], mapping_path: Path = MAPPING) -> dict[str, str]:
    """Build the sole allowed shadow plan before production mutation."""
    matches = [item for item in _mapping(mapping_path) if item["profile"] == profile]
    if len(matches) != 1:
        raise ShadowError(f"{profile}: requires exactly one typed shadow entry")
    entry = matches[0]
    selected = [item for item in patches if item.get("module") == entry["module"] and item.get("path") == entry["patch"]]
    if len(selected) != 1:
        raise ShadowError(f"{profile}: allowlisted shadow patch must occur exactly once")
    locks_root = mapping_path.parent.resolve()
    relative = Path(entry["lock"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ShadowError("shadow lock must be a contained relative path")
    candidate = locks_root / relative
    lock_path = candidate.resolve()
    if locks_root not in lock_path.parents or candidate.is_symlink() or not lock_path.is_file():
        raise ShadowError("shadow lock escapes locks/patch-stack")
    return {**entry, "lock_path": str(lock_path)}


def _seed_clone(root: Path, name: str, remote: str, ref: str) -> Path:
    repo = root / name
    _git(root, "init", "-q", str(repo))
    _git(repo, "remote", "add", "immutable", remote)
    _git(repo, "fetch", "--no-tags", "immutable", f"{ref}:refs/heads/seed")
    _git(repo, "checkout", "-q", "--detach", "refs/heads/seed")
    return repo


def run_shadow(
    *,
    shadow_plan: dict[str, str],
    legacy_patch: Path,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Compare legacy mbox application to its immutable canonical lock.

    This deliberately has no access to the production integration worktree:
    both sides are fresh disposable object databases and their refs disappear
    with the transaction directory.
    """
    legacy_patch = legacy_patch.resolve()
    profile, patch = shadow_plan["profile"], shadow_plan["patch"]
    lock_path = Path(shadow_plan["lock_path"])
    lock_bytes = lock_path.read_bytes()
    lock = load_lock(lock_path)
    transaction = uuid.uuid4().hex
    evidence: dict[str, Any] = {
        "verdict": "ERROR", "transaction_id": transaction, "profile": profile,
        "patch": patch, "lock": str(lock_path), "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "base_oid": lock["upstream"]["base_commit"], "source_oid": lock["source_commit"],
        "ordered_commits": lock["ordered_commits"], "canonical_expected_tree": lock["expected_tree"],
        "legacy_resulting_tree": None, "canonical_resulting_tree": None, "cleanup": {},
    }
    if evidence_path is None:
        evidence_path = ROOT / ".west-test" / "patch-stack-shadow" / f"{transaction}.json"
    evidence["evidence_path"] = str(evidence_path)
    root = Path(tempfile.gettempdir()) / f"west-patch-shadow-{transaction}"
    try:
        root.mkdir()
        legacy = _seed_clone(root, "legacy", lock["mirror"]["url"], lock["mirror"]["base_ref"])
        legacy_base = _git(legacy, "rev-parse", "HEAD")
        evidence["fetched_legacy_base_oid"] = legacy_base
        if legacy_base != lock["upstream"]["base_commit"]:
            raise ShadowError("mirror base tag OID differs from lock")
        headers = re.findall(
            r"^From ([0-9a-f]{40}) Mon Sep 17 00:00:00 2001$",
            legacy_patch.read_text(),
            re.MULTILINE,
        )
        evidence["legacy_mbox_ordered_commits"] = headers
        evidence["legacy_mbox_commit_count"] = len(headers)
        if headers != lock["ordered_commits"]:
            raise ShadowError("legacy mbox From chain differs from lock")
        _git(legacy, "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "user.name=West Shadow", "-c", "user.email=west-shadow@example.invalid", "am", "--3way", "--committer-date-is-author-date", str(legacy_patch))
        evidence["legacy_resulting_tree"] = _git(legacy, "rev-parse", "HEAD^{tree}")
        canonical = _seed_clone(root, "canonical", lock["mirror"]["url"], lock["mirror"]["base_ref"])
        canonical_evidence = root / "canonical-evidence.json"
        result = patch_stack_materialize.materialize(canonical, lock_path, evidence_path=canonical_evidence)
        evidence["canonical_resulting_tree"] = result["resulting_tree"]
        evidence["canonical_cleanup"] = result["cleanup"]
        if result["verdict"] != "VALID" or result["fetched"] != {"base_oid": lock["upstream"]["base_commit"], "source_oid": lock["source_commit"]}:
            raise ShadowError("canonical materialization evidence is incomplete")
        if evidence["legacy_resulting_tree"] != lock["expected_tree"] or evidence["canonical_resulting_tree"] != lock["expected_tree"]:
            raise ShadowError("legacy/canonical tree mismatch")
        evidence["verdict"] = "VALID"
    except BaseException as error:
        evidence["error"] = str(error)
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, ShadowError):
            raise
        raise ShadowError(str(error)) from error
    finally:
        try:
            if root.exists():
                shutil.rmtree(root)
            evidence["cleanup"]["root"] = "removed"
        except Exception as error:
            evidence["cleanup"]["root"] = f"failed: {error}"
            evidence["verdict"] = "ERROR"
        if evidence_path is not None:
            temporary = evidence_path.with_name(evidence_path.name + f".{transaction}.tmp")
            try:
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence["evidence_write"] = "published"
                temporary.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
                temporary.replace(evidence_path)
            except Exception as error:
                evidence["evidence_write"] = f"failed: {error}"
                if evidence["verdict"] == "VALID":
                    evidence["verdict"] = "ERROR"
                try:
                    temporary.unlink(missing_ok=True)
                    evidence["cleanup"]["evidence_tmp"] = "removed"
                except Exception as cleanup_error:
                    evidence["cleanup"]["evidence_tmp"] = f"failed: {cleanup_error}"
    if evidence["verdict"] != "VALID":
        raise ShadowError(evidence.get("error", "shadow cleanup failed"))
    return evidence
