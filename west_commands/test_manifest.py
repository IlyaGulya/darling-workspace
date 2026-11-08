"""Source-independent workload declarations consumed by the structured test runners.

The catalog contains fully expanded runner inputs, not source or installation
recipes. A module names the current workspace source used by a host fixture;
guest workloads always consume the caller's one selected installed runtime.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml


class ManifestError(ValueError):
    pass


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SOURCE_AUTHORITY = frozenset({
    "patches", "patch", "patch-path", "source-branch", "source-base",
    "source-commit", "source-revision", "source-ref", "source-patches",
    "source-modules", "base-profile", "test-profiles", "artifact-profiles",
    "resource-profiles", "fixture-profiles", "source-mode", "runtime-artifacts",
    "build-targets", "deploy", "use", "extends", "artifacts", "resources",
    "fixtures", "materialize-profile", "ctest-source-override",
    "profile", "runtime-mode", "runtime-root", "bootstrap", "launcher-env",
    "guest-toolchain", "source-worktree",
})
_OWNER_ENVIRONMENT = frozenset({
    "DARLING_ROOTLESS", "DARLING_NOOVERLAYFS", "DARLING_EUNION",
    "DARLING_RUNTIME_ROOT", "DARLING_SYSTEM_ROOT", "DARLING_LAUNCHER",
    "DARLING", "DPREFIX", "DARLING_PREFIX",
    "WEST_TEST_PREFIX_LEASE", "WEST_TEST_PREFIX_LEASE_FD",
})


def load_workloads(path: Path) -> dict[str, Any]:
    """Load a flat catalog, failing before selection on source authority."""

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ManifestError(f"{path}: invalid workload YAML: {error}") from error
    return normalize_workloads(data)


def normalize_workloads(data: Any) -> dict[str, Any]:
    """Validate runner declarations without resolving profiles or source refs."""

    if not isinstance(data, dict):
        raise ManifestError("workload catalog must be a mapping")
    _reject_source_authority(data, "catalog")
    if data.get("version") != 1:
        raise ManifestError("workload catalog version must be 1")
    unknown = data.keys() - {"version", "workloads", "aliases"}
    if unknown:
        raise ManifestError(f"unknown workload catalog fields: {', '.join(sorted(unknown))}")
    entries = data.get("workloads")
    if not isinstance(entries, list):
        raise ManifestError("workloads must be a list")
    normalized = deepcopy(data)
    identifiers: set[str] = set()
    for index, test in enumerate(normalized["workloads"], 1):
        location = f"workloads[{index}]"
        if not isinstance(test, dict):
            raise ManifestError(f"{location}: workload must be a mapping")
        identifier = test.get("id")
        _identifier(identifier, f"{location}.id")
        if identifier in identifiers:
            raise ManifestError(f"duplicate workload id: {identifier}")
        identifiers.add(identifier)
        for field in ("module",):
            if not isinstance(test.get(field), str) or not test[field].strip():
                raise ManifestError(f"{location}: {field} must be a non-empty string")
        test.setdefault("name", identifier)
        if not isinstance(test["name"], str) or not test["name"].strip():
            raise ManifestError(f"{location}: name must be a non-empty string")
        binding = test.get("ctest-label") or test.get("ctest-name")
        if not binding:
            test.setdefault("env", "host")
            test.setdefault("diag", "bare")
        if test.get("env") not in {None, "host", "darling", "macos"}:
            raise ManifestError(f"{location}: env must be host, darling, or macos")
        if test.get("runner") == "source-profile-script":
            raise ManifestError(f"{location}: source-profile-script is retired; use source-script-fixture")
        for field in ("ctest-label", "ctest-name"):
            if field in test and (not isinstance(test[field], str) or not test[field].strip()):
                raise ManifestError(f"{location}: {field} must be a non-empty string")
        if test.get("ctest-name") and test.get("runner") not in {None, "ctest"}:
            raise ManifestError(f"{location}: ctest-name requires a CTest registration binding")
        if binding and test.get("runner") in {None, "ctest"} and test.get("command"):
            raise ManifestError(f"{location}: a CTest registration binding cannot override command")
        if test.get("runner") in {"guest-c-fixture", "guest-command-fixture", "guest-macho-fixture", "guest-argv-fixture"} and test.get("env") != "darling":
            raise ManifestError(f"{location}: {test['runner']} only supports env:darling")
        if test.get("runner") == "source-script-fixture":
            if not isinstance(test.get("source-script"), str) or not test["source-script"].strip():
                raise ManifestError(f"{location}: source-script-fixture requires source-script")
            if not isinstance(test.get("cases"), list) or not test["cases"]:
                raise ManifestError(f"{location}: source-script-fixture requires non-empty cases")
        controller = test.get("prefix-controller", False)
        if not isinstance(controller, bool):
            raise ManifestError(f"{location}: prefix-controller must be a boolean")
        if controller and (
            test.get("runner") not in {"guest-runtime-script", "script", "python", "source-script-fixture"}
            or test.get("env") != "darling"
            or bool(test.get("command"))
            or not isinstance(test.get("requires"), list)
            or not all(isinstance(resource, str) for resource in test.get("requires", ()))
            or not set(test.get("requires", ())) & {"darling-prefix", "darling-eunion-prefix"}
        ):
            raise ManifestError(f"{location}: prefix-controller requires a host controller runner, env:darling and a declared prefix resource")
        features = test.get("requires-build-features", {})
        if not isinstance(features, dict):
            raise ManifestError(f"{location}: requires-build-features must be a mapping")
        for key, value in features.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ManifestError(f"{location}: build feature names must be CMake variable names")
            if not isinstance(value, (str, bool, int)) or isinstance(value, str) and not value.strip():
                raise ManifestError(f"{location}: build feature values must be non-empty scalars")
        installed_paths = test.get("requires-installed-paths", [])
        if not isinstance(installed_paths, list):
            raise ManifestError(f"{location}: requires-installed-paths must be a list")
        for path in installed_paths:
            if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts or path == ".":
                raise ManifestError(f"{location}: installed prerequisite paths must stay relative to the runtime root")
        proof = test.get("red-proof")
        if proof is not None and (not isinstance(proof, dict) or proof.get("mode") != "self"):
            raise ManifestError(f"{location}: only self-contained model proofs are supported")
        _default_coverage_tier(test)
    aliases = normalized.setdefault("aliases", {})
    if not isinstance(aliases, dict):
        raise ManifestError("aliases must be a mapping")
    for alias, members in aliases.items():
        _identifier(alias, "workload alias")
        if alias in identifiers:
            raise ManifestError(f"workload alias conflicts with id: {alias}")
        if not isinstance(members, list) or not members:
            raise ManifestError(f"alias {alias!r} must contain workload ids")
        for member in members:
            if not isinstance(member, str) or member not in identifiers:
                raise ManifestError(f"alias {alias!r}: unknown workload id {member!r}")
        if len(members) != len(set(members)):
            raise ManifestError(f"alias {alias!r} contains duplicate workload ids")
    return normalized


def _identifier(value: Any, location: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ManifestError(f"{location}: expected a stable workload identifier")


def _reject_source_authority(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ManifestError(f"{location}: mapping keys must be strings")
            if key in _OWNER_ENVIRONMENT and location.endswith((".env-vars", ".guest-env-vars")):
                raise ManifestError(f"{location}.{key}: installed runtime and prefix selection belong to the caller")
            if key in _SOURCE_AUTHORITY or key.endswith(("-profile", "-profiles")) or key.startswith("current-minus-"):
                raise ManifestError(f"{location}.{key}: source/runtime selection authority is retired")
            _reject_source_authority(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value, 1):
            _reject_source_authority(child, f"{location}[{index}]")


def _default_coverage_tier(test: dict[str, Any]) -> None:
    if test.get("coverage-tier"):
        return
    runner = test.get("runner")
    if runner in {"source-contract-script", "source-script-fixture"} or test.get("kind") == "source-contract":
        test["kind"] = "source-contract"
        test["coverage-tier"] = "source"
    elif test.get("env") in {"darling", "macos"} or test.get("kind") == "guest":
        test["coverage-tier"] = "runtime"
    elif runner in {"c-fixture", "object-symbol-fixture"} or test.get("kind") == "build":
        test["coverage-tier"] = "compile"
    else:
        test["coverage-tier"] = "host"
