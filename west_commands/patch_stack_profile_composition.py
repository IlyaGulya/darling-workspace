"""Typed profile-composition locks for canonical patch-stack replay.

An immutable series lock proves the intent and standalone result of one patch
series.  This module proves the distinct statement made by a profile: the
ordered result of replaying those immutable series on the profile's actual
module bases.  It deliberately stores trees, never generated integration
commit IDs, because committer identity is not canonical profile state.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml


class ProfileCompositionError(RuntimeError):
    pass


_OID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROOT_FIELDS = {"schema_version", "profile", "prerequisites", "frozen_manifest", "mapping", "modules"}
_MAPPING_FIELDS = {"path", "sha256", "batch_id", "expected_count"}
_FROZEN_MANIFEST_FIELDS = {"path", "sha256"}
_MODULE_FIELDS = {"module", "starting", "series", "final_tree", "integration_final_tree"}
_STARTING_FIELDS = {"tree"}
_SERIES_FIELDS = {"patch", "lock", "expected_applied_tree"}
_PREREQUISITE_FIELDS = {"profile", "composition", "sha256", "frozen_manifest", "module_trees"}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ProfileCompositionError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _oid(value: object, name: str) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise ProfileCompositionError(f"profile composition {name} must be a lowercase 40-hex OID")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProfileCompositionError(f"profile composition {name} must be a lowercase SHA-256")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileCompositionError(f"profile composition {name} must be a non-empty string")
    return value


def _contained_regular_file(root: Path, relative: str, name: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ProfileCompositionError(f"profile composition {name} escapes workspace root")
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ProfileCompositionError(f"profile composition {name} may not traverse a symlink")
    if not current.is_file():
        raise ProfileCompositionError(f"profile composition {name} is not a regular file")
    return current


def _workspace_root(mapping_path: Path, relative: str) -> Path:
    """Find the nearest workspace root containing the declared frozen file."""
    for candidate in (mapping_path.parent, *mapping_path.parents):
        try:
            _contained_regular_file(candidate, relative, "frozen_manifest.path")
        except ProfileCompositionError:
            continue
        return candidate
    raise ProfileCompositionError("profile composition frozen manifest is absent from mapping workspace")


def _prerequisite(root: Path, item: object) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _PREREQUISITE_FIELDS:
        raise ProfileCompositionError("profile composition prerequisite has invalid fields")
    profile = _nonempty(item.get("profile"), "prerequisite.profile")
    name = _nonempty(item.get("composition"), "prerequisite.composition")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise ProfileCompositionError("profile composition prerequisite escapes locks directory")
    _sha256(item.get("sha256"), "prerequisite.sha256")
    frozen = item.get("frozen_manifest")
    if not isinstance(frozen, dict) or set(frozen) != _FROZEN_MANIFEST_FIELDS:
        raise ProfileCompositionError("profile composition prerequisite frozen manifest is invalid")
    _nonempty(frozen.get("path"), "prerequisite.frozen_manifest.path")
    _sha256(frozen.get("sha256"), "prerequisite.frozen_manifest.sha256")
    trees = item.get("module_trees")
    if not isinstance(trees, dict) or not trees:
        raise ProfileCompositionError("profile composition prerequisite module trees are invalid")
    normalized: dict[str, str] = {}
    for module, tree in trees.items():
        normalized[_nonempty(module, "prerequisite.module_trees module")] = _oid(tree, "prerequisite.module_trees tree")
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise ProfileCompositionError("profile composition prerequisite lock is not a regular file")
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != item["sha256"]:
        raise ProfileCompositionError("profile composition prerequisite lock SHA-256 differs")
    return {
        "profile": profile,
        "composition": name,
        "sha256": item["sha256"],
        "frozen_manifest": dict(frozen),
        "module_trees": normalized,
    }


def load(path: Path) -> dict[str, Any]:
    """Load one exact schema-v3 profile composition lock without binding it."""
    try:
        payload = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError, ProfileCompositionError) as error:
        raise ProfileCompositionError(f"invalid profile composition lock: {error}") from error
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS or payload.get("schema_version") != 3:
        raise ProfileCompositionError("profile composition must use exact schema_version 3 fields")
    _nonempty(payload.get("profile"), "profile")
    if not isinstance(payload.get("prerequisites"), list):
        raise ProfileCompositionError("profile composition prerequisites are invalid")
    prerequisites = [_prerequisite(path.parent, item) for item in payload["prerequisites"]]
    if len({item["profile"] for item in prerequisites}) != len(prerequisites):
        raise ProfileCompositionError("profile composition prerequisites are duplicate")
    frozen_manifest = payload.get("frozen_manifest")
    if not isinstance(frozen_manifest, dict) or set(frozen_manifest) != _FROZEN_MANIFEST_FIELDS:
        raise ProfileCompositionError("profile composition frozen_manifest is invalid")
    _nonempty(frozen_manifest.get("path"), "frozen_manifest.path")
    _sha256(frozen_manifest.get("sha256"), "frozen_manifest.sha256")
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) != _MAPPING_FIELDS:
        raise ProfileCompositionError("profile composition mapping is invalid")
    _nonempty(mapping.get("path"), "mapping.path")
    _sha256(mapping.get("sha256"), "mapping.sha256")
    _nonempty(mapping.get("batch_id"), "mapping.batch_id")
    if not isinstance(mapping.get("expected_count"), int) or isinstance(mapping["expected_count"], bool) or mapping["expected_count"] < 1:
        raise ProfileCompositionError("profile composition mapping.expected_count is invalid")
    modules = payload.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ProfileCompositionError("profile composition modules are invalid")
    seen_modules: set[str] = set()
    for module in modules:
        if not isinstance(module, dict) or set(module) != _MODULE_FIELDS:
            raise ProfileCompositionError("profile composition module has invalid fields")
        name = _nonempty(module.get("module"), "module")
        if name in seen_modules:
            raise ProfileCompositionError("profile composition has duplicate module")
        seen_modules.add(name)
        starting = module.get("starting")
        if not isinstance(starting, dict) or set(starting) != _STARTING_FIELDS:
            raise ProfileCompositionError("profile composition module starting point is invalid")
        _oid(starting.get("tree"), "starting.tree")
        series = module.get("series")
        if not isinstance(series, list) or not series:
            raise ProfileCompositionError("profile composition module series are invalid")
        seen_series: set[tuple[str, str]] = set()
        for item in series:
            if not isinstance(item, dict) or set(item) != _SERIES_FIELDS:
                raise ProfileCompositionError("profile composition series has invalid fields")
            key = (_nonempty(item.get("patch"), "series.patch"), _nonempty(item.get("lock"), "series.lock"))
            if key in seen_series:
                raise ProfileCompositionError("profile composition has duplicate series")
            seen_series.add(key)
            _oid(item.get("expected_applied_tree"), "series.expected_applied_tree")
        _oid(module.get("final_tree"), "module.final_tree")
        if module["final_tree"] != series[-1]["expected_applied_tree"]:
            raise ProfileCompositionError("profile composition final tree differs from final boundary")
        _oid(module.get("integration_final_tree"), "module.integration_final_tree")
    payload["prerequisites"] = prerequisites
    return payload


def bind(
    path: Path,
    *,
    mapping_path: Path,
    mapping: dict[str, Any],
    entries: list[dict[str, str]],
) -> dict[str, Any]:
    """Bind a composition lock to one exact typed mapping and grouped plan."""
    payload = load(path)
    if payload["profile"] != mapping["profile"]:
        raise ProfileCompositionError("profile composition profile differs from mapping")
    declared = payload["mapping"]
    if declared["path"] != mapping_path.name:
        raise ProfileCompositionError("profile composition mapping path differs")
    if declared["sha256"] != hashlib.sha256(mapping_path.read_bytes()).hexdigest():
        raise ProfileCompositionError("profile composition mapping SHA-256 differs")
    if declared["batch_id"] != mapping["batch_id"] or declared["expected_count"] != mapping["expected_count"]:
        raise ProfileCompositionError("profile composition mapping identity differs")
    frozen = payload["frozen_manifest"]
    workspace_root = _workspace_root(mapping_path, frozen["path"])
    manifest_path = _contained_regular_file(workspace_root, frozen["path"], "frozen_manifest.path")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != frozen["sha256"]:
        raise ProfileCompositionError("profile composition frozen manifest SHA-256 differs")
    for prerequisite in payload["prerequisites"]:
        prerequisite_path = path.parent / prerequisite["composition"]
        prerequisite_lock = load(prerequisite_path)
        if prerequisite_lock["profile"] != prerequisite["profile"]:
            raise ProfileCompositionError("profile composition prerequisite profile differs")
        if prerequisite_lock["frozen_manifest"] != prerequisite["frozen_manifest"]:
            raise ProfileCompositionError("profile composition prerequisite frozen manifest differs")
        available = {
            item["module"]: (item["final_tree"] if item["module"] == "darling" else item["integration_final_tree"])
            for item in prerequisite_lock["modules"]
        }
        if any(available.get(module) != tree for module, tree in prerequisite["module_trees"].items()):
            raise ProfileCompositionError("profile composition prerequisite module tree differs")
    grouped: list[tuple[str, list[dict[str, str]]]] = []
    for entry in entries:
        if not grouped or grouped[-1][0] != entry["module"]:
            grouped.append((entry["module"], []))
        grouped[-1][1].append(entry)
    if [module["module"] for module in payload["modules"]] != [name for name, _ in grouped]:
        raise ProfileCompositionError("profile composition module order differs from grouped mapping")
    boundaries: dict[tuple[str, str], str] = {}
    starts: dict[str, dict[str, str]] = {}
    finals: dict[str, str] = {}
    integration_finals: dict[str, str] = {}
    for declared_module, (module, configured) in zip(payload["modules"], grouped, strict=True):
        expected = [(entry["patch"], Path(entry["lock_path"]).name) for entry in configured]
        observed = [(item["patch"], item["lock"]) for item in declared_module["series"]]
        if observed != expected:
            raise ProfileCompositionError("profile composition series order differs from typed mapping")
        starts[module] = dict(declared_module["starting"])
        finals[module] = declared_module["final_tree"]
        # A parent tree contains gitlink commit IDs, which are generated
        # lifecycle evidence and may vary with deterministic committer
        # identity.  Its canonical integration boundary is its own content
        # tree; nested module trees are verified independently below.
        integration_finals[module] = (
            declared_module["final_tree"] if module == "darling"
            else declared_module["integration_final_tree"]
        )
        for item in declared_module["series"]:
            boundaries[(module, item["patch"])] = item["expected_applied_tree"]
    return {
        "schema_version": payload["schema_version"],
        "path": path.name,
        "profile": payload["profile"],
        "prerequisites": [dict(item) for item in payload["prerequisites"]],
        "frozen_manifest": dict(frozen),
        "boundaries": boundaries,
        "starts": starts,
        "finals": finals,
        "integration_finals": integration_finals,
    }


def verify_integration(
    module: str, repo: Path, expected_tree: str, all_expected: dict[str, str], repos: dict[str, Path], *, ref: str = "HEAD",
) -> None:
    """Verify an integration boundary without treating generated gitlinks as IDs."""
    actual = subprocess.run(["git", "rev-parse", f"{ref}^{{tree}}"], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if actual.returncode:
        raise ProfileCompositionError(f"cannot read {module} integration tree: {actual.stderr.strip()}")
    actual_tree = actual.stdout.strip()
    if module != "darling":
        if actual_tree != expected_tree:
            raise ProfileCompositionError(f"{module} integration tree {actual_tree} differs from typed profile final tree {expected_tree}")
        return
    diff = subprocess.run(["git", "diff-tree", "--raw", "-r", expected_tree, actual_tree], cwd=repo,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if diff.returncode:
        raise ProfileCompositionError(f"cannot compare darling integration content: {diff.stderr.strip()}")
    children = {name for name in all_expected if name.startswith("darling/")}
    relative_children = {str(Path(name).relative_to("darling")): name for name in children}
    for line in filter(None, diff.stdout.splitlines()):
        fields = line.split("\t", 1)
        if len(fields) != 2:
            raise ProfileCompositionError("darling integration diff is malformed")
        meta, path = fields
        modes = meta.split()[:2]
        if modes:
            modes[0] = modes[0].lstrip(":")
        if path not in relative_children or modes != ["160000", "160000"]:
            raise ProfileCompositionError("darling integration changed non-gitlink content")
    for child in children:
        target = repos.get(child)
        if target is None:
            raise ProfileCompositionError(f"darling integration child {child} is unavailable")
        result = subprocess.run(["git", "rev-parse", f"{ref}^{{tree}}"], cwd=target, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or result.stdout.strip() != all_expected[child]:
            raise ProfileCompositionError(f"darling integration child {child} differs from typed final tree")
