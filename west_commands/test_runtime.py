"""Runtime RED proof planning helpers for ``west test``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_ctest_runtime_profiles(path: Path) -> dict[str, dict[str, Any]]:
    """Load the explicit runtime deployments used by CTest guest entries."""

    data = yaml.safe_load(path.read_text()) or {}
    profiles = data.get("runtime-profiles")
    if not isinstance(profiles, dict):
        raise ValueError("runtime-profiles must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name or not isinstance(profile, dict):
            raise ValueError("each runtime profile needs a non-empty name and mapping")
        source_profile = profile.get("source-profile")
        source_module = profile.get("source-module")
        source_modules = profile.get("source-modules")
        artifacts = profile.get("runtime-artifacts")
        if not isinstance(source_profile, str) or not source_profile:
            raise ValueError(f"runtime profile {name!r} needs source-profile")
        if not isinstance(source_module, str) or not source_module:
            raise ValueError(f"runtime profile {name!r} needs source-module")
        if not isinstance(source_modules, list) or not all(
            isinstance(module, str) and module for module in source_modules
        ):
            raise ValueError(f"runtime profile {name!r} needs source-modules")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError(f"runtime profile {name!r} needs runtime-artifacts")
        deploy_paths = {
            deploy_path
            for artifact in artifacts
            if isinstance(artifact, dict)
            for deploy_path in artifact.get("deploy", [])
            if isinstance(deploy_path, str)
        }
        system_kernel_modules = {
            "darling",
            "darling/src/external/darlingserver",
            "darling/src/external/xnu",
        }
        missing_system_kernel_modules = system_kernel_modules.difference(source_modules)
        if "usr/lib/system/libsystem_kernel.dylib" in deploy_paths and missing_system_kernel_modules:
            raise ValueError(
                f"runtime profile {name!r} deploying system_kernel must materialize "
                f"{', '.join(sorted(missing_system_kernel_modules))}"
            )
        normalized[name] = {
            "source-profile": source_profile,
            "source-module": source_module,
            "source-modules": source_modules,
            "runtime-artifacts": artifacts,
        }
    return normalized


def runtime_build_targets(proof: dict[str, Any]) -> list[str]:
    """Return unique Ninja targets from runtime-artifacts in first-seen order."""

    targets: list[str] = []
    for artifact in proof.get("runtime-artifacts", []):
        for target in artifact.get("build-targets", []):
            if target not in targets:
                targets.append(target)
    return targets


def runtime_deploy_targets(prefix: Path, deploy_path: str) -> list[Path]:
    """Return prefix file targets for one runtime artifact deploy path.

    Darling system paths under ``usr`` exist in both the guest-visible prefix
    root and the base root under ``libexec/darling``. Runtime proof deploys must
    swap both copies so the next guest launch cannot accidentally run stale
    code from the other view.
    """

    rel = Path(deploy_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"guest-runtime-deploy deploy path must be relative: {deploy_path}")
    if rel.parts and rel.parts[0] == "usr":
        return [prefix / "libexec/darling" / rel, prefix / rel]
    return [prefix / rel]


def describe_runtime_deploy_plan(proof: dict[str, Any]) -> str:
    def list_text(value: Any, missing: str) -> str:
        if not isinstance(value, list):
            return missing
        if not value:
            return missing
        return ",".join(str(item) for item in value)

    artifacts = []
    for artifact in proof.get("runtime-artifacts", []):
        if not isinstance(artifact, dict):
            artifacts.append("<invalid-artifact>")
            continue
        module = artifact.get("module")
        targets = artifact.get("build-targets")
        deploy = artifact.get("deploy")
        module_text = module if isinstance(module, str) and module else "<missing-module>"
        target_text = list_text(targets, "<missing-build-targets>")
        deploy_text = list_text(deploy, "<missing-deploy>")
        artifacts.append(f"{module_text}[build:{target_text}; deploy:{deploy_text}]")
    bad_profile = proof.get("bad-profile")
    suffix = f" [{bad_profile}]" if bad_profile else ""
    source_modules = proof.get("source-modules")
    source_text = ""
    if isinstance(source_modules, list) and source_modules:
        source_text = " sources:" + ",".join(str(item) for item in source_modules)
    return "guest-runtime-deploy" + suffix + source_text + ": " + "; ".join(artifacts)
