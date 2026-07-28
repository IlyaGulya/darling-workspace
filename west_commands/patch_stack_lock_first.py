"""Typed, opt-in canonical materialization for an ordered legacy batch.

This module deliberately contains no profile or patch-name literals.  The
allowlist is data in ``locks/patch-stack/lock-first-series-v2.yml`` and is
validated before the normal patch lifecycle mutates a worktree.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
import json
import re
from pathlib import Path
from typing import Any

import patch_stack_materialize
import patch_stack_profile_composition
import yaml


class LockFirstError(RuntimeError):
    pass


class LockFirstPlan(list[dict[str, str]]):
    """Ordered plan carrying the typed batch identity used for evidence."""

    def __init__(
        self,
        entries: list[dict[str, str]],
        metadata: dict[str, Any],
        composition: dict[str, Any] | None = None,
    ):
        super().__init__(entries)
        series = [{"module": entry["module"], "patch": entry["patch"]} for entry in entries]
        self.batch = {
            "batch_id": metadata["batch_id"],
            "expected_count": metadata["expected_count"],
            "series_order": series,
            "module_order": list(dict.fromkeys(entry["module"] for entry in entries)),
        }
        self.composition = composition
        if composition is not None:
            modules: list[dict[str, Any]] = []
            for module in self.batch["module_order"]:
                boundaries = [
                    {"patch": entry["patch"], "tree": composition["boundaries"][(module, entry["patch"])]}
                    for entry in entries if entry["module"] == module
                ]
                modules.append({
                    "module": module,
                    "starting": composition["starts"][module],
                    "series": boundaries,
                    "final_tree": composition["finals"][module],
                    "integration_final_tree": composition["integration_finals"][module],
                })
            self.batch["profile_composition"] = {
                "schema_version": composition["schema_version"],
                "path": composition["path"],
                "prerequisites": composition["prerequisites"],
                "frozen_manifest": composition["frozen_manifest"],
                "modules": modules,
            }


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "locks" / "patch-stack" / "lock-first-series-v2.yml"
MAPPING_REGISTRY = ROOT / "locks" / "patch-stack" / "lock-first-profiles-v1.yml"


def _cherry_pick(repo: Path, commit: str, *, git_options: tuple[str, ...] = ()) -> None:
    """Replay an immutable commit through Git's native mbox machinery.

    The input remains the declared immutable commit; the temporary mbox is
    generated locally with ``format-patch``.  This intentionally shares the
    exact message parsing and whitespace behavior of native ``git am``
    without reading a historical patch archive.
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
            ["git", *git_options, "am", "--3way", "--committer-date-is-author-date", mbox], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise LockFirstError(f"git am for immutable {commit} failed ({result.returncode}): {result.stderr.strip()}")
    finally:
        if mbox is not None:
            Path(mbox).unlink(missing_ok=True)


_MAPPING_V2_FIELDS = {"schema_version", "profile", "batch_id", "expected_count", "series"}
_MAPPING_V3_FIELDS = {*_MAPPING_V2_FIELDS, "composition"}
_SERIES_FIELDS = {"profile", "module", "patch", "lock"}
_REGISTRY_FIELDS = {"schema_version", "profiles"}
_REGISTRY_ENTRY_FIELDS = {"profile", "mapping"}


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
    if not isinstance(data, dict):
        raise LockFirstError("lock-first mapping must be an object")
    version = data.get("schema_version")
    if version == 2 and set(data) == _MAPPING_V2_FIELDS:
        data = {**data, "composition": None}
    elif version == 3 and set(data) == _MAPPING_V3_FIELDS:
        if not isinstance(data.get("composition"), str) or not data["composition"]:
            raise LockFirstError("lock-first mapping composition is invalid")
    else:
        raise LockFirstError("lock-first mapping must use exact schema_version 2 or 3")
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


def mapping_for_profile(profile: str, registry_path: Path = MAPPING_REGISTRY) -> Path:
    """Return the one typed mapping declared for a production profile.

    Profile selection is data-driven: no runtime caller chooses a batch file
    by profile-specific code. The registry and every path component are
    containment-checked before YAML from a mapping is read.
    """
    if not isinstance(profile, str) or not profile:
        raise LockFirstError("lock-first profile is invalid")
    try:
        registry = yaml.safe_load(registry_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise LockFirstError(f"invalid lock-first profile registry: {error}") from error
    if (not isinstance(registry, dict) or set(registry) != _REGISTRY_FIELDS
            or registry.get("schema_version") != 1
            or not isinstance(registry.get("profiles"), list)):
        raise LockFirstError("lock-first profile registry must use exact schema_version 1")
    root = registry_path.parent.absolute()
    if root.is_symlink():
        raise LockFirstError("lock-first profile registry root may not be a symlink")
    matches: list[Path] = []
    seen_profiles: set[str] = set()
    seen_mappings: set[Path] = set()
    for index, entry in enumerate(registry["profiles"]):
        if not isinstance(entry, dict) or set(entry) != _REGISTRY_ENTRY_FIELDS:
            raise LockFirstError(f"lock-first profile registry entry {index} is invalid")
        configured, relative_name = entry["profile"], entry["mapping"]
        if not isinstance(configured, str) or not configured or not isinstance(relative_name, str) or not relative_name:
            raise LockFirstError(f"lock-first profile registry entry {index} has an empty scalar")
        if configured in seen_profiles:
            raise LockFirstError("lock-first profile registry contains duplicate profile")
        seen_profiles.add(configured)
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise LockFirstError("lock-first profile mapping must be a contained filename")
        candidate = root / relative
        if candidate.is_symlink():
            raise LockFirstError("lock-first profile mapping may not be a symlink")
        resolved = candidate.resolve()
        if root not in resolved.parents or not resolved.is_file():
            raise LockFirstError("lock-first profile mapping escapes locks/patch-stack")
        if resolved in seen_mappings:
            raise LockFirstError("lock-first profile registry resolves duplicate mapping")
        seen_mappings.add(resolved)
        if configured == profile:
            matches.append(resolved)
    if not matches:
        raise LockFirstError(f"{profile}: no typed lock-first mapping is configured")
    if len(matches) != 1:
        raise LockFirstError(f"{profile}: typed lock-first mapping is ambiguous")
    load_mapping(matches[0], profile)
    return matches[0]


def plan(
    profile: str,
    patches: list[dict[str, Any]],
    mapping_path: Path | None = None,
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> LockFirstPlan:
    """Return an ordered, uniquely matched batch before any mutation."""
    if mapping_path is None:
        mapping_path = mapping_for_profile(profile)
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
    composition = None
    composition_name = metadata.get("composition")
    if composition_name is not None:
        relative = Path(composition_name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise LockFirstError("lock-first composition must be a contained filename")
        candidate = locks_root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise LockFirstError("lock-first composition may not be a symlink and must exist")
        try:
            composition = patch_stack_profile_composition.bind(
                candidate.resolve(), mapping_path=mapping_path.resolve(), mapping=metadata, entries=resolved,
            )
        except patch_stack_profile_composition.ProfileCompositionError as error:
            raise LockFirstError(f"{profile}: invalid profile composition: {error}") from error
    return LockFirstPlan(resolved, metadata, composition)


_OID = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_SCHEMA_VERSION = 2
_EVIDENCE_FIELDS = {"module", "patch", "base", "source", "canonical_tree", "applied_commit", "applied_tree", "verdict"}


def _stable_patch_id(repo: Path, start: str, end: str) -> str:
    """Return Git's stable identity for the complete ordered patch range."""
    diff = patch_stack_materialize._run(repo, "diff", start, end)
    if diff.returncode:
        raise LockFirstError(f"git diff for patch identity failed ({diff.returncode}): {diff.stderr.strip()}")
    result = subprocess.run(
        ["git", "patch-id", "--stable"], input=diff.stdout, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) < 1 or not _OID.fullmatch(fields[0]):
        raise LockFirstError(f"git patch-id failed ({result.returncode}): {result.stderr.strip()}")
    return fields[0]


def _require_exact_replay(repo: Path, proof: dict[str, Any], before: str, after: str) -> None:
    """Prove the applied history is an exact native replay of one immutable stack."""
    applied = patch_stack_materialize._git(repo, "rev-list", "--reverse", f"{before}..{after}").splitlines()
    if len(applied) != len(proof["ordered_commits"]):
        raise LockFirstError("profile replay produced a different commit count")
    for index, commit in enumerate(applied):
        parents = patch_stack_materialize._git(repo, "show", "-s", "--format=%P", commit).split()
        expected_parent = before if index == 0 else applied[index - 1]
        if parents != [expected_parent]:
            raise LockFirstError("profile replay produced a merge or nonlinear history")
    comparison = patch_stack_materialize._run(
        repo, "range-diff", f"{proof['base_oid']}..{proof['source_oid']}", f"{before}..{after}",
    )
    if comparison.returncode:
        raise LockFirstError(f"git range-diff failed ({comparison.returncode}): {comparison.stderr.strip()}")
    rows = [line for line in comparison.stdout.splitlines() if re.match(r"^\d+:\s+", line)]
    if len(rows) != len(proof["ordered_commits"]) or any(" = " not in line for line in rows):
        raise LockFirstError("immutable patch identity differs after profile replay")
    if _stable_patch_id(repo, proof["base_oid"], proof["source_oid"]) != _stable_patch_id(repo, before, after):
        raise LockFirstError("stable patch identity differs after profile replay")


def _composition_boundary(
    composition: dict[str, Any] | None,
    entry: dict[str, str],
) -> str:
    if composition is None:
        raise LockFirstError(
            f"{entry['patch']}: profile base differs from immutable series base and no composition lock is declared"
        )
    boundary = composition["boundaries"].get((entry["module"], entry["patch"]))
    if not isinstance(boundary, str) or not _OID.fullmatch(boundary):
        raise LockFirstError(f"{entry['patch']}: profile composition has no boundary tree")
    return boundary


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
    composition = batch.get("profile_composition")
    if composition is not None:
        if not isinstance(composition, dict) or set(composition) != {"schema_version", "path", "prerequisites", "frozen_manifest", "modules"}:
            raise LockFirstError("lock-first evidence profile composition is invalid")
        if composition["schema_version"] != 3 or not isinstance(composition["path"], str) or not composition["path"]:
            raise LockFirstError("lock-first evidence profile composition identity is invalid")
        if (not isinstance(composition["frozen_manifest"], dict)
                or set(composition["frozen_manifest"]) != {"path", "sha256"}
                or not isinstance(composition["frozen_manifest"]["path"], str)
                or not composition["frozen_manifest"]["path"]
                or not isinstance(composition["frozen_manifest"]["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", composition["frozen_manifest"]["sha256"])):
            raise LockFirstError("lock-first evidence profile composition frozen manifest is invalid")
        if (not isinstance(composition["prerequisites"], list)
                or any(not isinstance(value, dict) or set(value) != {"profile", "composition", "sha256", "frozen_manifest", "module_trees"}
                       or not isinstance(value["profile"], str) or not value["profile"]
                       or not isinstance(value["composition"], str) or not value["composition"]
                       or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                       or not isinstance(value["frozen_manifest"], dict) or set(value["frozen_manifest"]) != {"path", "sha256"}
                       or not isinstance(value["module_trees"], dict) or not value["module_trees"]
                       for value in composition["prerequisites"])):
            raise LockFirstError("lock-first evidence profile composition prerequisites are invalid")
        if not isinstance(composition["modules"], list) or [item.get("module") if isinstance(item, dict) else None for item in composition["modules"]] != module_order:
            raise LockFirstError("lock-first evidence profile composition module order is invalid")
        for module in composition["modules"]:
            if set(module) != {"module", "starting", "series", "final_tree", "integration_final_tree"}:
                raise LockFirstError("lock-first evidence profile composition module fields are invalid")
            if (not isinstance(module["starting"], dict) or set(module["starting"]) != {"tree"}
                    or not isinstance(module["starting"]["tree"], str) or not _OID.fullmatch(module["starting"]["tree"])
                    or not isinstance(module["final_tree"], str) or not _OID.fullmatch(module["final_tree"])
                    or not isinstance(module["integration_final_tree"], str) or not _OID.fullmatch(module["integration_final_tree"])):
                raise LockFirstError("lock-first evidence profile composition tree is invalid")
            expected_module = [entry["patch"] for entry in expected_series if entry["module"] == module["module"]]
            observed_module = module["series"]
            if (not isinstance(observed_module, list) or [item.get("patch") if isinstance(item, dict) else None for item in observed_module] != expected_module
                    or any(not isinstance(item, dict) or set(item) != {"patch", "tree"} or not isinstance(item["tree"], str) or not _OID.fullmatch(item["tree"]) for item in observed_module)
                    or not observed_module or module["final_tree"] != observed_module[-1]["tree"]):
                raise LockFirstError("lock-first evidence profile composition boundaries are invalid")
    payload = {"evidence_schema_version": EVIDENCE_SCHEMA_VERSION, "verdict": "VALID", "batch_id": batch_id, "expected_count": expected_count,
               "module_order": module_order, "series_order": expected_series, "series": results}
    if composition is not None:
        payload["profile_composition"] = composition
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


def materialize_batch_into(
    repo: Path,
    entries: list[dict[str, str]],
    *,
    git_options: tuple[str, ...] = (),
    reset_to_first_base: bool = False,
    composition: dict[str, Any] | None = None,
    skip_patches: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate and replay one module's immutable series in one transaction.

    A fresh disposable ODB receives the union of the module's declared base
    and source refs in one fetch. All locks are proven before the integration
    worktree is changed; commits are then replayed in exact typed order using
    native ``format-patch`` and ``git am``. ``reset_to_first_base`` is only
    for lifecycle-owned disposable worktrees whose manifest revision is not
    the profile's immutable integration base. The caller owns profile
    rollback. ``skip_patches`` is reserved for current-minus RED proofs: all
    immutable locks are still fetched and validated, but selected series are
    omitted and later series are proven by exact range-diff/patch identity
    rather than by a canonical boundary tree that necessarily includes the
    omitted change.
    """
    if not entries or len({entry["module"] for entry in entries}) != 1:
        raise LockFirstError("lock-first batch must contain one non-empty module")
    locks: list[tuple[dict[str, str], dict[str, Any]]] = []
    mirrors: set[str] = set()
    for entry in entries:
        try:
            lock = patch_stack_materialize.load_lock(Path(entry["lock_path"]))
        except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
            raise LockFirstError(f"{entry['patch']}: invalid immutable lock: {error}") from error
        locks.append((entry, lock))
        mirrors.add(lock["mirror"]["url"])
    if len(mirrors) != 1:
        raise LockFirstError("lock-first module entries use different immutable mirrors")
    transaction = uuid.uuid4().hex
    root = Path(tempfile.gettempdir()) / f"west-patch-lock-first-{transaction}"
    fetched_prefix = f"refs/west/patch-stack-lock-first/{transaction}"
    fetched_refs: list[str] = []
    results: list[dict[str, Any]] = []
    skipped = skip_patches or set()
    configured_patches = {entry["patch"] for entry in entries}
    if skipped - configured_patches:
        raise LockFirstError(
            "current-minus skip is not present in the typed module batch: "
            + ", ".join(sorted(skipped - configured_patches))
        )
    stats = {"immutable_fetch_transactions": 0, "temporary_contexts": 1, "validated_locks": 0, "replayed_commits": 0}
    try:
        root.mkdir()
        canonical = root / "canonical"
        patch_stack_materialize._git(root, "init", "-q", str(canonical))
        patch_stack_materialize._git(canonical, "remote", "add", "immutable", next(iter(mirrors)))
        canonical_specs: list[str] = []
        production_specs: list[str] = []
        refs: list[tuple[str, str]] = []
        for index, (_, lock) in enumerate(locks):
            for kind in ("base", "source"):
                remote_ref = lock["mirror"][f"{kind}_ref"]
                canonical_ref = f"refs/west/lock-first-input/{transaction}/{index}/{kind}"
                production_ref = f"{fetched_prefix}/{index}/{kind}"
                canonical_specs.append(f"{remote_ref}:{canonical_ref}")
                production_specs.append(f"{remote_ref}:{production_ref}")
                refs.append((canonical_ref, production_ref))
                fetched_refs.append(production_ref)
        patch_stack_materialize._git(canonical, "fetch", "--no-tags", "immutable", *canonical_specs)
        stats["immutable_fetch_transactions"] += 1
        validated: list[tuple[dict[str, str], dict[str, Any], dict[str, Any]]] = []
        for index, (entry, lock) in enumerate(locks):
            proof = patch_stack_materialize.validate_fetched_lock(canonical, lock, refs[index * 2][0], refs[index * 2 + 1][0])
            validated.append((entry, lock, proof))
            stats["validated_locks"] += 1
        # The immutable remote is contacted once for this module.  The second
        # transfer is from this transaction's disposable ODB, never an
        # alternate/shared object store or persistent cache.
        patch_stack_materialize._git(
            repo, "fetch", "--no-tags", "--no-recurse-submodules", str(canonical),
            *(f"{canonical_ref}:{production_ref}" for canonical_ref, production_ref in refs),
        )
        for index, (_, _, proof) in enumerate(validated):
            if patch_stack_materialize._oid(repo, refs[index * 2 + 1][1]) != proof["source_oid"]:
                raise LockFirstError("production immutable source fetch differs from validated source")
        if reset_to_first_base:
            first_base = validated[0][2]["base_oid"]
            patch_stack_materialize._git(repo, "reset", "--hard", first_base)
        omitted_before = False
        for entry, lock, proof in validated:
            if entry["patch"] in skipped:
                omitted_before = True
                continue
            before = patch_stack_materialize._oid(repo, "HEAD")
            before_tree = patch_stack_materialize._git(repo, "rev-parse", "HEAD^{tree}")
            declared_base_tree = patch_stack_materialize._git(repo, "show", "-s", "--format=%T", proof["base_oid"])
            for commit in proof["ordered_commits"]:
                _cherry_pick(repo, commit, git_options=git_options)
                stats["replayed_commits"] += 1
            after = patch_stack_materialize._oid(repo, "HEAD")
            applied_tree = patch_stack_materialize._git(repo, "rev-parse", "HEAD^{tree}")
            if not omitted_before and before_tree == declared_base_tree:
                expected_tree = proof["resulting_tree"]
            elif not omitted_before:
                _require_exact_replay(repo, proof, before, after)
                expected_tree = _composition_boundary(composition, entry)
            else:
                _require_exact_replay(repo, proof, before, after)
                expected_tree = applied_tree
            if applied_tree != expected_tree:
                raise LockFirstError(
                    f"{entry['patch']}: immutable replay tree {applied_tree} differs from "
                    f"expected profile boundary tree {expected_tree}"
                )
            results.append({"module": entry["module"], "patch": entry["patch"],
                            "base": lock["upstream"]["base_commit"], "source": proof["source_oid"],
                            "canonical_tree": proof["resulting_tree"],
                            "applied_commit": patch_stack_materialize._oid(repo, "HEAD"),
                            "applied_tree": applied_tree, "verdict": "VALID"})
        return results, stats
    except patch_stack_materialize.MaterializeError as error:
        raise LockFirstError(str(error)) from error
    finally:
        failures: list[str] = []
        # Native git am owns rebase-apply state.  The profile caller also
        # aborts touched repositories, but this primitive is used directly by
        # deterministic contracts and RuntimeSourceMaterializer, so it must
        # leave no interrupted apply state when its own transaction fails.
        rebase_path = patch_stack_materialize._run(repo, "rev-parse", "--git-path", "rebase-apply")
        if rebase_path.returncode == 0:
            candidate = Path(rebase_path.stdout.strip())
            if not candidate.is_absolute():
                candidate = repo / candidate
            if candidate.exists():
                aborted = patch_stack_materialize._run(repo, "am", "--abort")
                if aborted.returncode:
                    failures.append(f"git am --abort failed ({aborted.returncode}): {aborted.stderr.strip()}")
        elif rebase_path.returncode != 0:
            failures.append(f"git rev-parse --git-path rebase-apply failed ({rebase_path.returncode}): {rebase_path.stderr.strip()}")
        for ref in fetched_refs:
            try:
                patch_stack_materialize._delete_ref(repo, ref)
            except patch_stack_materialize.MaterializeError as error:
                failures.append(str(error))
        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError as error:
            failures.append(str(error))
        if failures:
            raise LockFirstError("lock-first batch cleanup failed: " + "; ".join(failures))


def materialize_into(
    repo: Path, lock_first_plan: dict[str, str]
) -> dict[str, Any]:
    """Compatibility shim for focused single-series tests.

    Production invokes :func:`materialize_batch_into`; this wrapper accepts
    only a typed immutable-lock entry and never an archive path.
    """
    results, _ = materialize_batch_into(repo, [lock_first_plan])
    return results[0]
