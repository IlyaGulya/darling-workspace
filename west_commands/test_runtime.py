"""Runtime RED proof planning helpers for ``west test``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOTLESS_NO_MOUNT_DEPLOY_PATHS = frozenset(
    {
        "bin/darling",
        "bin/darlingserver",
        "usr/libexec/darling/mldr",
        "usr/libexec/darling/launchd",
        "usr/libexec/darling/vchroot",
        "usr/lib/dyld",
        "usr/lib/system/libsystem_kernel.dylib",
    }
)


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
        bootstrap = profile.get("bootstrap")
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
        if bootstrap is not None and bootstrap != "rootless-no-mount":
            raise ValueError(
                f"runtime profile {name!r} has unknown bootstrap {bootstrap!r}"
            )
        cmake_defines = profile.get("cmake-defines", {})
        if not isinstance(cmake_defines, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(value, (str, int, float, bool, type(None)))
            for key, value in cmake_defines.items()
        ):
            raise ValueError(
                f"runtime profile {name!r} cmake-defines must map non-empty names "
                "to scalar values"
            )
        launcher_env = profile.get("launcher-env", {})
        if not isinstance(launcher_env, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(value, (str, int, float, bool, type(None)))
            for key, value in launcher_env.items()
        ):
            raise ValueError(
                f"runtime profile {name!r} launcher-env must map non-empty names "
                "to scalar values"
            )
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
        if bootstrap == "rootless-no-mount":
            missing_bootstrap_paths = ROOTLESS_NO_MOUNT_DEPLOY_PATHS.difference(
                deploy_paths
            )
            if missing_bootstrap_paths:
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount is missing "
                    "bootstrap deploy path(s): "
                    + ", ".join(sorted(missing_bootstrap_paths))
                )
        normalized[name] = {
            "source-profile": source_profile,
            "source-module": source_module,
            "source-modules": source_modules,
            "runtime-artifacts": artifacts,
            "cmake-defines": cmake_defines,
            "launcher-env": launcher_env,
        }
        if bootstrap is not None:
            normalized[name]["bootstrap"] = bootstrap
    return normalized


def compose_ctest_runtime_profiles(
    definitions: dict[str, dict[str, Any]], names: list[str]
) -> dict[str, Any] | None:
    """Merge compatible CTest runtime profiles into one deployment plan.

    One guest CTest selection can need several independently owned artifacts,
    such as libsystem_kernel and darlingserver.  They are safe to deploy in one
    lifecycle only when they come from the same source patch profile and no two
    declarations disagree about a deployed path.
    """

    selected = list(dict.fromkeys(names))
    if not selected:
        return None
    unknown = [name for name in selected if name not in definitions]
    if unknown:
        raise ValueError(f"unknown runtime profile(s): {', '.join(unknown)}")
    source_profiles = {definitions[name]["source-profile"] for name in selected}
    if len(source_profiles) != 1:
        raise ValueError(
            "incompatible runtime source profiles: "
            + ", ".join(f"{name}={definitions[name]['source-profile']}" for name in selected)
        )
    source_modules: list[str] = []
    artifacts: list[dict[str, Any]] = []
    deployed: dict[str, dict[str, Any]] = {}
    cmake_defines: dict[str, Any] = {}
    launcher_env: dict[str, Any] = {}
    bootstrap: str | None = None
    for name in selected:
        definition = definitions[name]
        for module in definition["source-modules"]:
            if module not in source_modules:
                source_modules.append(module)
        for artifact in definition["runtime-artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError(f"runtime profile {name!r} has invalid artifact")
            deploy_paths = artifact.get("deploy", [])
            conflicts = [
                deploy_path
                for deploy_path in deploy_paths
                if deploy_path in deployed and deployed[deploy_path] != artifact
            ]
            if conflicts:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on deploy path(s): "
                    f"{', '.join(conflicts)}"
                )
            if artifact not in artifacts:
                artifacts.append(artifact)
            for deploy_path in deploy_paths:
                deployed[deploy_path] = artifact
        for key, value in definition.get("cmake-defines", {}).items():
            if key in cmake_defines and cmake_defines[key] != value:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on CMake definition {key}"
                )
            cmake_defines[key] = value
        for key, value in definition.get("launcher-env", {}).items():
            if key in launcher_env and launcher_env[key] != value:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on launcher environment {key}"
                )
            launcher_env[key] = value
        candidate_bootstrap = definition.get("bootstrap")
        if candidate_bootstrap is not None:
            if bootstrap is not None and bootstrap != candidate_bootstrap:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on bootstrap {candidate_bootstrap}"
                )
            bootstrap = candidate_bootstrap
    result = {
        "name": "+".join(selected),
        "source-profile": source_profiles.pop(),
        "source-module": definitions[selected[0]]["source-module"],
        "source-modules": source_modules,
        "runtime-artifacts": artifacts,
    }
    if cmake_defines:
        result["cmake-defines"] = cmake_defines
    if launcher_env:
        result["launcher-env"] = launcher_env
    if bootstrap is not None:
        result["bootstrap"] = bootstrap
    return result


def partition_ctest_runtime_profiles(
    definitions: dict[str, dict[str, Any]],
    selections: list[dict[str, Any]],
    additional_profiles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan isolated CTest lifecycles for the exact selected tests.

    A suite can contain guest cases whose artifacts are built from different
    patch profiles. Those source trees cannot be combined, but their cases can
    run predictably in separate deploy/restore lifecycles under one prefix
    lock. Keeping this planner pure makes its validation independently testable.
    """

    extra = list(dict.fromkeys(additional_profiles or []))
    unknown = [name for name in extra if name not in definitions]
    if unknown:
        raise ValueError(f"unknown runtime profile(s): {', '.join(unknown)}")

    groups: dict[str | None, dict[str, Any]] = {}
    for selection in selections:
        name = selection.get("name")
        profiles = list(dict.fromkeys(selection.get("profiles", [])))
        darling = bool(selection.get("darling"))
        if not isinstance(name, str) or not name:
            raise ValueError("CTest runtime selection needs a non-empty test name")
        unknown = [profile for profile in profiles if profile not in definitions]
        if unknown:
            raise ValueError(
                f"CTest test {name!r} declares unknown runtime profile(s): "
                + ", ".join(unknown)
            )
        if darling and not profiles:
            raise ValueError(
                f"Darling CTest test {name!r} needs an explicit runtime-profile label"
            )
        source_profile = None
        if profiles:
            sources = {definitions[profile]["source-profile"] for profile in profiles}
            if len(sources) != 1:
                raise ValueError(
                    f"CTest test {name!r} declares incompatible runtime source profiles: "
                    + ", ".join(
                        f"{profile}={definitions[profile]['source-profile']}"
                        for profile in profiles
                    )
                )
            source_profile = sources.pop()
        group = groups.setdefault(
            source_profile,
            {"source-profile": source_profile, "profiles": [], "tests": []},
        )
        group["tests"].append(name)
        for profile in profiles:
            if profile not in group["profiles"]:
                group["profiles"].append(profile)

    runtime_groups = [group for group in groups.values() if group["source-profile"]]
    if extra:
        if not runtime_groups:
            raise ValueError(
                "--with-runtime-profile needs at least one selected CTest runtime-profile"
            )
        extra_sources = {definitions[profile]["source-profile"] for profile in extra}
        if len(extra_sources) != 1:
            raise ValueError(
                "--with-runtime-profile declarations must share one source profile: "
                + ", ".join(
                    f"{profile}={definitions[profile]['source-profile']}" for profile in extra
                )
            )
        extra_source = extra_sources.pop()
        matching = [group for group in runtime_groups if group["source-profile"] == extra_source]
        if not matching:
            raise ValueError(
                "--with-runtime-profile source profile does not match any selected CTest runtime: "
                + extra_source
            )
        for group in matching:
            for profile in extra:
                if profile not in group["profiles"]:
                    group["profiles"].append(profile)

    planned: list[dict[str, Any]] = []
    for group in groups.values():
        profiles = group["profiles"]
        planned.append({**group, "definition": compose_ctest_runtime_profiles(definitions, profiles)})
    return planned


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
