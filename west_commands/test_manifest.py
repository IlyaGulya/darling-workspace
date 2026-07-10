"""Patch test manifest normalization helpers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    pass


def load_test_profile(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: profile must be a mapping")
    return normalize_test_profile(data)


def normalize_test_profile(data: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(data)
    test_profiles = _mapping(normalized.get("test-profiles"), "test-profiles")
    artifact_profiles = _mapping(normalized.get("artifact-profiles"), "artifact-profiles")
    resource_profiles = _mapping(normalized.get("resource-profiles"), "resource-profiles")
    fixture_profiles = _mapping(normalized.get("fixture-profiles"), "fixture-profiles")
    for patch in normalized.get("patches", []) or []:
        tests = patch.get("tests")
        if tests is None:
            continue
        if not isinstance(tests, list):
            continue
        patch["tests"] = [
            normalize_test(
                test,
                test_profiles,
                artifact_profiles,
                resource_profiles,
                fixture_profiles,
                index=index,
            )
            if isinstance(test, dict)
            else test
            for index, test in enumerate(tests, start=1)
        ]
    return normalized


def normalize_test(
    test: dict[str, Any],
    test_profiles: dict[str, Any],
    artifact_profiles: dict[str, Any],
    resource_profiles: dict[str, Any] | None = None,
    fixture_profiles: dict[str, Any] | None = None,
    *,
    index: int = 0,
) -> dict[str, Any]:
    resource_profiles = resource_profiles or {}
    fixture_profiles = fixture_profiles or {}
    refs = test.get("use", test.get("extends"))
    profile_names = _as_list(refs)
    merged: dict[str, Any] = {}
    stack: list[str] = []
    for name in profile_names:
        merged = _deep_merge(
            merged,
            _resolve_test_profile(
                str(name),
                test_profiles,
                artifact_profiles,
                resource_profiles,
                fixture_profiles,
                stack,
            ),
        )
    override = {key: value for key, value in test.items() if key not in {"use", "extends"}}
    merged = _deep_merge(merged, override)
    _expand_compact_axes(merged, index=index)
    _expand_artifacts(merged, artifact_profiles, index=index)
    _expand_resources(merged, resource_profiles, index=index)
    _expand_fixtures(merged, fixture_profiles, index=index)
    _default_red_failure_phase(merged)
    return merged


def _resolve_test_profile(
    name: str,
    test_profiles: dict[str, Any],
    artifact_profiles: dict[str, Any],
    resource_profiles: dict[str, Any],
    fixture_profiles: dict[str, Any],
    stack: list[str],
) -> dict[str, Any]:
    if name in stack:
        chain = " -> ".join([*stack, name])
        raise ManifestError(f"cyclic test profile inheritance: {chain}")
    if name not in test_profiles:
        raise ManifestError(f"unknown test profile {name!r}")
    profile = test_profiles[name]
    if not isinstance(profile, dict):
        raise ManifestError(f"test profile {name!r} must be a mapping")
    stack.append(name)
    refs = profile.get("use", profile.get("extends"))
    merged: dict[str, Any] = {}
    for parent in _as_list(refs):
        merged = _deep_merge(
            merged,
            _resolve_test_profile(
                str(parent),
                test_profiles,
                artifact_profiles,
                resource_profiles,
                fixture_profiles,
                stack,
            ),
        )
    body = {key: value for key, value in profile.items() if key not in {"use", "extends"}}
    merged = _deep_merge(merged, body)
    _expand_compact_axes(merged)
    _expand_artifacts(merged, artifact_profiles)
    _expand_resources(merged, resource_profiles)
    _expand_fixtures(merged, fixture_profiles)
    _default_red_failure_phase(merged)
    stack.pop()
    return merged


def _expand_compact_axes(test: dict[str, Any], *, index: int = 0) -> None:
    location = f"tests[{index}]" if index else "test profile"
    if "needs" in test:
        raise ManifestError(
            f"{location}: needs is not part of the compact test DSL; "
            "use artifacts/resources/fixtures"
        )
    if "ctest" in test:
        if "ctest-label" in test and test["ctest-label"] != test["ctest"]:
            raise ManifestError(f"{location}: ctest conflicts with ctest-label")
        test["ctest-label"] = test.pop("ctest")
    if "build-target" in test:
        if "target" in test and test["target"] != test["build-target"]:
            raise ManifestError(f"{location}: build-target conflicts with target")
        test["target"] = test.pop("build-target")
    runs = test.pop("runs", None)
    if runs is not None:
        runs_map = {"host": "host", "guest": "darling", "macos": "macos"}
        if runs not in runs_map:
            raise ManifestError(f"{location}: runs must be host, guest, or macos")
        env = runs_map[str(runs)]
        if "env" in test and test["env"] != env:
            raise ManifestError(f"{location}: runs conflicts with env")
        test["env"] = env
        if runs == "guest":
            _append_unique(test, "requires", ["darling-prefix"])
    proof = test.get("red-proof")
    if isinstance(proof, str):
        proof_map = {
            "source": {"mode": "source-base"},
            "runtime": {
                "mode": "guest-runtime-deploy",
                "bad-profile": "current-minus-patch",
            },
            "self": {"mode": "self"},
            "none": None,
        }
        if proof not in proof_map:
            raise ManifestError(
                f"{location}: red-proof must be source, runtime, self, none, or a mapping"
            )
        expanded = proof_map[proof]
        if expanded is None:
            test.pop("red-proof", None)
            test["red"] = False
        else:
            test["red-proof"] = expanded
            test.setdefault("red", True)
    elif proof is not None and not isinstance(proof, dict):
        raise ManifestError(
            f"{location}: red-proof must be source, runtime, self, none, or a mapping"
        )


def _default_red_failure_phase(test: dict[str, Any]) -> None:
    """Fill the deterministic failing stage implied by compact test axes.

    A RED proof must not accept an arbitrary non-zero exit.  The runner emits
    one of these stable stage markers only at the failing operation and checks
    the expanded value before accepting RED.  Explicit manifest values always
    win for unusual runners.
    """

    proof = test.get("red-proof")
    if not isinstance(proof, dict) or proof.get("expect-failure-phase"):
        return
    mode = proof.get("mode")
    if mode == "guest-runtime-deploy":
        proof["expect-failure-phase"] = "runtime"
        return
    if mode == "self":
        proof["expect-failure-phase"] = "self"
        return
    if mode != "source-base":
        return

    runner = test.get("runner")
    tier = test.get("coverage-tier")
    if runner == "object-symbol-fixture":
        proof["expect-failure-phase"] = "inspect"
    elif runner in {"source-contract-script", "source-profile-script", "source-script-fixture"}:
        proof["expect-failure-phase"] = "script"
    elif runner == "source-build-fixture":
        proof["expect-failure-phase"] = "build" if tier == "compile" else "run"
    elif runner in {"cmake-configure-fixture", "darling-cmake-target-fixture"}:
        proof["expect-failure-phase"] = "configure" if tier != "compile" else "build"
    elif runner == "c-fixture":
        proof["expect-failure-phase"] = "compile" if tier == "compile" else "run"
    else:
        proof["expect-failure-phase"] = "script"


def _expand_artifacts(
    test: dict[str, Any],
    artifact_profiles: dict[str, Any],
    *,
    index: int = 0,
) -> None:
    refs = test.pop("artifacts", None)
    if refs is None:
        return
    artifacts = []
    for ref in _as_list(refs):
        if isinstance(ref, dict):
            artifacts.append(deepcopy(ref))
            continue
        name = str(ref)
        if name not in artifact_profiles:
            location = f"tests[{index}]" if index else "test profile"
            raise ManifestError(f"{location}: unknown artifact profile {name!r}")
        artifact = artifact_profiles[name]
        if isinstance(artifact, list):
            artifacts.extend(deepcopy(artifact))
        elif isinstance(artifact, dict):
            artifacts.append(deepcopy(artifact))
        else:
            raise ManifestError(f"artifact profile {name!r} must be a mapping or list")
    if not artifacts:
        return
    proof = test.setdefault("red-proof", {})
    if not isinstance(proof, dict):
        raise ManifestError("red-proof must be a mapping when artifacts are used")
    existing = proof.get("runtime-artifacts", [])
    if existing and not isinstance(existing, list):
        raise ManifestError("red-proof.runtime-artifacts must be a list")
    proof["runtime-artifacts"] = [*deepcopy(existing), *artifacts]


def _expand_resources(
    test: dict[str, Any],
    resource_profiles: dict[str, Any],
    *,
    index: int = 0,
) -> None:
    refs = test.pop("resources", None)
    if refs is None:
        return
    for ref in _as_list(refs):
        profile = _resolve_named_profile(
            ref,
            resource_profiles,
            "resource",
            index=index,
        )
        _merge_typed_resource(test, profile, index=index)


def _expand_fixtures(
    test: dict[str, Any],
    fixture_profiles: dict[str, Any],
    *,
    index: int = 0,
) -> None:
    refs = test.pop("fixtures", None)
    if refs is None:
        return
    for ref in _as_list(refs):
        profile = _resolve_named_profile(
            ref,
            fixture_profiles,
            "fixture",
            index=index,
        )
        _merge_typed_fixture(test, profile, index=index)


def _resolve_named_profile(
    ref: Any,
    profiles: dict[str, Any],
    profile_type: str,
    *,
    index: int = 0,
) -> dict[str, Any]:
    if isinstance(ref, dict):
        if "use" not in ref:
            return deepcopy(ref)
        name = str(ref["use"])
        override = {key: value for key, value in ref.items() if key != "use"}
    else:
        name = str(ref)
        override = {}
    if name not in profiles:
        location = f"tests[{index}]" if index else "test profile"
        raise ManifestError(f"{location}: unknown {profile_type} profile {name!r}")
    profile = profiles[name]
    if not isinstance(profile, dict):
        raise ManifestError(f"{profile_type} profile {name!r} must be a mapping")
    return _deep_merge(profile, override)


def _merge_typed_resource(
    test: dict[str, Any],
    profile: dict[str, Any],
    *,
    index: int = 0,
) -> None:
    location = f"tests[{index}]" if index else "test profile"
    kind = profile.get("kind")
    body = {key: value for key, value in profile.items() if key != "kind"}
    if kind == "dcc-cache":
        _merge_single_mapping(test, "dcc-cache", body, location)
    elif kind == "host-trace-files":
        _extend_list_field(test, "host-trace-files", body.get("files", []), location)
        if body.get("oracle") is not None:
            test["host-trace-oracle"] = bool(body["oracle"])
    elif kind == "host-stat-deltas":
        _extend_list_field(test, "host-stat-deltas", body.get("fields", []), location)
    else:
        raise ManifestError(
            f"{location}: resource profile kind must be dcc-cache, "
            "host-trace-files, or host-stat-deltas"
        )


def _merge_typed_fixture(
    test: dict[str, Any],
    profile: dict[str, Any],
    *,
    index: int = 0,
) -> None:
    location = f"tests[{index}]" if index else "test profile"
    kind = profile.get("kind")
    body = {key: value for key, value in profile.items() if key != "kind"}
    if kind == "eunion-overlay":
        _append_unique(test, "requires", ["darling-eunion-prefix"])
        _extend_list_field(test, "eunion-template-files", body.get("template-files", []), location)
        _extend_list_field(test, "eunion-template-symlinks", body.get("template-symlinks", []), location)
        _extend_list_field(test, "eunion-upper-files", body.get("upper-files", []), location)
        _extend_list_field(test, "eunion-cleanup-dirs", body.get("cleanup-dirs", []), location)
        if body.get("verify-template-files-after") is not None:
            test["eunion-verify-template-files-after"] = bool(
                body["verify-template-files-after"]
            )
    else:
        raise ManifestError(f"{location}: fixture profile kind must be eunion-overlay")


def _merge_single_mapping(
    test: dict[str, Any],
    field: str,
    value: dict[str, Any],
    location: str,
) -> None:
    if not isinstance(value, dict):
        raise ManifestError(f"{location}: {field} resource body must be a mapping")
    if field in test and test[field]:
        if not isinstance(test[field], dict):
            raise ManifestError(f"{location}: {field} must be a mapping")
        test[field] = _deep_merge(test[field], value)
    else:
        test[field] = deepcopy(value)


def _extend_list_field(
    test: dict[str, Any],
    field: str,
    values: Any,
    location: str,
) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        raise ManifestError(f"{location}: {field} profile data must be a list")
    existing = test.get(field, [])
    if existing and not isinstance(existing, list):
        raise ManifestError(f"{location}: {field} must be a list")
    test[field] = [*deepcopy(existing), *deepcopy(values)]


def _append_unique(test: dict[str, Any], field: str, values: list[Any]) -> None:
    existing = list(_as_list(test.get(field)))
    for value in values:
        if value not in existing:
            existing.append(value)
    test[field] = existing


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be a mapping")
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
