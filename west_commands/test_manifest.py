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
    for patch in normalized.get("patches", []) or []:
        tests = patch.get("tests")
        if tests is None:
            continue
        if not isinstance(tests, list):
            continue
        patch["tests"] = [
            normalize_test(test, test_profiles, artifact_profiles, index=index)
            if isinstance(test, dict)
            else test
            for index, test in enumerate(tests, start=1)
        ]
    return normalized


def normalize_test(
    test: dict[str, Any],
    test_profiles: dict[str, Any],
    artifact_profiles: dict[str, Any],
    *,
    index: int = 0,
) -> dict[str, Any]:
    refs = test.get("use", test.get("extends"))
    profile_names = _as_list(refs)
    merged: dict[str, Any] = {}
    stack: list[str] = []
    for name in profile_names:
        merged = _deep_merge(
            merged,
            _resolve_test_profile(str(name), test_profiles, artifact_profiles, stack),
        )
    override = {key: value for key, value in test.items() if key not in {"use", "extends"}}
    merged = _deep_merge(merged, override)
    _expand_artifacts(merged, artifact_profiles, index=index)
    return merged


def _resolve_test_profile(
    name: str,
    test_profiles: dict[str, Any],
    artifact_profiles: dict[str, Any],
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
            _resolve_test_profile(str(parent), test_profiles, artifact_profiles, stack),
        )
    body = {key: value for key, value in profile.items() if key not in {"use", "extends"}}
    merged = _deep_merge(merged, body)
    _expand_artifacts(merged, artifact_profiles)
    stack.pop()
    return merged


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
