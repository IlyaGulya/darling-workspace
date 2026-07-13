"""Runtime RED proof planning helpers for ``west test``."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml


ROOTLESS_BOOTSTRAP_RESOURCE = "rootless-bootstrap"
ROOTLESS_BOOTSTRAP_TARGET = "rootless_bootstrap"
ROOTLESS_BOOTSTRAP_MANIFEST = "darling-rootless-bootstrap.json"
# Source owners whose patched revisions can provide Mach-O libraries in the
# bootstrap closure. A materialized runtime forest must not leave them as live
# symlinks, or it can build an unpatched provider while claiming profile parity.
ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES = frozenset(
    {
        "darling/src/external/corefoundation",
        "darling/src/external/libsystem",
    }
)
ROOTLESS_NO_MOUNT_SOURCE_MODULES = frozenset(
    {
        "darling",
        "darling/src/external/darlingserver",
        "darling/src/external/dyld",
        "darling/src/external/xnu",
    }
).union(ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES)
ROOTLESS_NO_MOUNT_RUNTIME_RESOURCES = frozenset(
    {ROOTLESS_BOOTSTRAP_RESOURCE}
)
_RUNTIME_RESOURCES = ROOTLESS_NO_MOUNT_RUNTIME_RESOURCES
_MACHO_MAGICS = frozenset(
    {
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)
_MACHO_FAT_MAGICS = frozenset(
    {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)
_CMAKE_DEFINE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_CMAKE_DEFINE_RESERVED = frozenset(
    {"CMAKE_BUILD_TYPE", "CMAKE_INSTALL_PREFIX", "DARLING_PATCH_PROFILE"}
)


def parse_runtime_cmake_define_overrides(values: list[str]) -> dict[str, str]:
    """Parse explicit feature overrides for a disposable runtime deployment."""

    overrides: dict[str, str] = {}
    for value in values:
        name, separator, definition = value.partition("=")
        if not separator or not _CMAKE_DEFINE_NAME.fullmatch(name):
            raise ValueError(
                "runtime CMake overrides must have the form NAME=VALUE; "
                f"got {value!r}"
            )
        if name in _RUNTIME_CMAKE_DEFINE_RESERVED:
            raise ValueError(
                f"runtime CMake override {name!r} is owned by the runtime framework"
            )
        if "\n" in definition or "\r" in definition:
            raise ValueError(
                f"runtime CMake override {name!r} must be one line"
            )
        previous = overrides.get(name)
        if previous is not None and previous != definition:
            raise ValueError(
                f"runtime CMake override {name!r} was specified with conflicting values"
            )
        overrides[name] = definition
    return overrides


def merge_runtime_cmake_define_overrides(
    declared: Mapping[str, Any], overrides: Mapping[str, str]
) -> dict[str, Any]:
    """Apply intentional diagnostic feature overrides to one provider plan."""

    return {**declared, **overrides}


def runtime_artifact_deploy_paths(artifact: dict[str, Any]) -> list[str]:
    """Expand one typed runtime artifact into concrete prefix deploy paths."""

    paths = list(artifact.get("deploy", []))
    resource = artifact.get("resource")
    if resource is None:
        return paths
    if resource not in _RUNTIME_RESOURCES:
        raise ValueError(f"unknown runtime artifact resource {resource!r}")
    return paths


def runtime_artifact_has_resource(artifact: dict[str, Any], resource: str) -> bool:
    """Return whether an artifact declares one named runtime resource."""

    return artifact.get("resource") == resource


def is_macho_binary(path: Path) -> bool:
    """Return whether *path* starts with a supported thin or fat Mach-O magic."""

    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def is_fat_macho_binary(path: Path) -> bool:
    """Return whether *path* is a universal Mach-O product."""

    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACHO_FAT_MAGICS
    except OSError:
        return False


def load_rootless_bootstrap_manifest(build_root: Path) -> dict[str, Path]:
    """Load CMake's rootless products and validate their deployment boundary.

    CMake owns the component's target-to-guest-path mapping. West only consumes
    its generated product metadata and refuses paths that escape the disposable
    build tree. ``entrypoints`` name built executables; ``resources`` name
    source-owned runtime files such as launchd plists. The runtime closure code
    separately selects only Mach-O entrypoints as dylib-dependency roots.
    """

    manifest_path = build_root / ROOTLESS_BOOTSTRAP_MANIFEST
    try:
        data = json.loads(manifest_path.read_text())
    except OSError as exc:
        raise ValueError(f"cannot read rootless bootstrap manifest {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid rootless bootstrap manifest {manifest_path}: {exc.msg}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("rootless bootstrap manifest must have schema 1")
    entries = data.get("entrypoints")
    if not isinstance(entries, list) or not entries:
        raise ValueError("rootless bootstrap manifest needs non-empty entrypoints")
    resources = data.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError("rootless bootstrap manifest resources must be a list")

    resolved_root = build_root.resolve()
    deployments: dict[str, Path] = {}

    def load_entries(items: list[Any], kind: str, *, executable: bool) -> None:
        for index, entry in enumerate(items):
            label = "entry" if kind == "entrypoints" else "resource"
            if not isinstance(entry, dict):
                raise ValueError(
                    f"rootless bootstrap manifest {label} {index} must be a mapping"
                )
            target = entry.get("target")
            guest_path = entry.get("guest_path")
            host_path = entry.get("host_path")
            if not all(
                isinstance(value, str) and value for value in (target, guest_path, host_path)
            ):
                raise ValueError(
                    f"rootless bootstrap manifest {label} {index} needs target, guest_path, and host_path"
                )
            guest = Path(guest_path)
            if not guest.is_absolute() or ".." in guest.parts:
                raise ValueError(
                    f"rootless bootstrap manifest {label} {target!r} has invalid guest path {guest_path!r}"
                )
            relative_guest_path = guest_path.removeprefix("/")
            if not relative_guest_path:
                raise ValueError(
                    f"rootless bootstrap manifest {label} {target!r} cannot deploy at /"
                )
            source = Path(host_path)
            if not source.is_absolute():
                raise ValueError(
                    f"rootless bootstrap manifest {label} {target!r} has non-absolute host path {host_path!r}"
                )
            resolved_source = source.resolve()
            if not resolved_source.is_relative_to(resolved_root):
                raise ValueError(
                    f"rootless bootstrap manifest {label} {target!r} escapes build root: {host_path}"
                )
            if not resolved_source.is_file():
                requirement = "built executable" if executable else "regular resource file"
                raise ValueError(
                    f"rootless bootstrap manifest {label} {target!r} is not a {requirement}: {host_path}"
                )
            if executable and not resolved_source.stat().st_mode & 0o111:
                raise ValueError(
                    f"rootless bootstrap manifest entry {target!r} is not a built executable: {host_path}"
                )
            if relative_guest_path in deployments:
                raise ValueError(
                    f"rootless bootstrap manifest has duplicate guest path {guest_path!r}"
                )
            deployments[relative_guest_path] = resolved_source

    load_entries(entries, "entrypoints", executable=True)
    load_entries(resources, "resources", executable=False)
    return deployments


def parse_macho_dylib_id(output: str) -> str | None:
    """Extract the install name from ``llvm-objdump --macho --dylib-id`` output."""

    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("/") and not candidate.endswith(":"):
            return candidate
    return None


def parse_macho_dylib_dependencies(output: str) -> list[str]:
    """Extract ordered install names from ``llvm-objdump --macho --dylibs-used``."""

    dependencies: list[str] = []
    for line in output.splitlines():
        candidate = line.strip()
        name, separator, _details = candidate.partition(" (")
        if separator and name:
            dependencies.append(name)
    return dependencies


def resolve_macho_runtime_closure(
    roots: Mapping[str, Path],
    providers: Mapping[str, Path],
    dependencies_for: Callable[[Path], list[str]],
) -> dict[str, Path]:
    """Resolve guest-Mach-O dependencies from explicit roots to built providers.

    Rootless startup has no host shared cache to hide an omitted dylib. The
    manifest therefore declares a closure resource, while this function derives
    its concrete members from the product binaries actually built for the run.
    """

    closure = dict(roots)
    pending = list(roots)
    while pending:
        required_by = pending.pop(0)
        source = closure[required_by]
        for dependency in dependencies_for(source):
            if dependency == required_by:
                continue
            if not dependency.startswith("/"):
                raise ValueError(
                    "rootless bootstrap closure cannot resolve non-absolute Mach-O "
                    f"dependency {dependency!r} required by {required_by}"
                )
            provider = providers.get(dependency)
            if provider is None:
                raise ValueError(
                    "rootless bootstrap closure has no built provider for Mach-O "
                    f"dependency {dependency} required by {required_by}"
                )
            if dependency not in closure:
                closure[dependency] = provider
                pending.append(dependency)
    return closure


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
        purpose = profile.get("purpose", "runtime")
        bootstrap_smoke_timeout = profile.get("bootstrap-smoke-timeout-seconds", 60)
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
        if purpose not in {"runtime", "prefix-baseline"}:
            raise ValueError(
                f"runtime profile {name!r} has unknown purpose {purpose!r}"
            )
        if purpose == "prefix-baseline" and bootstrap != "rootless-no-mount":
            raise ValueError(
                f"runtime profile {name!r} prefix-baseline must use rootless-no-mount"
            )
        if (
            type(bootstrap_smoke_timeout) is not int
            or bootstrap_smoke_timeout <= 0
        ):
            raise ValueError(
                f"runtime profile {name!r} bootstrap-smoke-timeout-seconds "
                "must be a positive integer"
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
            deploy_path for artifact in artifacts if isinstance(artifact, dict)
            for deploy_path in runtime_artifact_deploy_paths(artifact)
            if isinstance(deploy_path, str)
        }
        resources = {
            artifact.get("resource") for artifact in artifacts if isinstance(artifact, dict)
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
            missing_bootstrap_modules = ROOTLESS_NO_MOUNT_SOURCE_MODULES.difference(
                source_modules
            )
            if missing_bootstrap_modules:
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount must materialize "
                    "bootstrap source module(s): "
                    + ", ".join(sorted(missing_bootstrap_modules))
                )
            missing_resources = ROOTLESS_NO_MOUNT_RUNTIME_RESOURCES.difference(resources)
            if missing_resources:
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount is missing "
                    "runtime resource(s): " + ", ".join(sorted(missing_resources))
                )
            bootstrap_artifacts = [
                artifact
                for artifact in artifacts
                if isinstance(artifact, dict)
                and runtime_artifact_has_resource(artifact, ROOTLESS_BOOTSTRAP_RESOURCE)
            ]
            if len(bootstrap_artifacts) != 1:
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount needs exactly one "
                    f"{ROOTLESS_BOOTSTRAP_RESOURCE!r} resource"
                )
            bootstrap_artifact = bootstrap_artifacts[0]
            if bootstrap_artifact.get("build-targets") != [ROOTLESS_BOOTSTRAP_TARGET]:
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount resource must build "
                    f"only {ROOTLESS_BOOTSTRAP_TARGET!r}"
                )
            if runtime_artifact_deploy_paths(bootstrap_artifact):
                raise ValueError(
                    f"runtime profile {name!r} rootless-no-mount resource must not "
                    "declare deploy paths; CMake owns them"
                )
        normalized[name] = {
            "source-profile": source_profile,
            "source-module": source_module,
            "source-modules": source_modules,
            "runtime-artifacts": artifacts,
            "cmake-defines": cmake_defines,
            "launcher-env": launcher_env,
            "purpose": purpose,
            "bootstrap-smoke-timeout-seconds": bootstrap_smoke_timeout,
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
    bootstrap_smoke_timeout = 0
    for name in selected:
        definition = definitions[name]
        bootstrap_smoke_timeout = max(
            bootstrap_smoke_timeout,
            definition.get("bootstrap-smoke-timeout-seconds", 60),
        )
        for module in definition["source-modules"]:
            if module not in source_modules:
                source_modules.append(module)
        for artifact in definition["runtime-artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError(f"runtime profile {name!r} has invalid artifact")
            deploy_paths = runtime_artifact_deploy_paths(artifact)
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
        "bootstrap-smoke-timeout-seconds": bootstrap_smoke_timeout,
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
        baseline_profiles = [
            profile
            for profile in profiles
            if definitions[profile].get("purpose") == "prefix-baseline"
        ]
        if baseline_profiles:
            raise ValueError(
                f"CTest test {name!r} cannot select prefix-baseline runtime profile(s): "
                + ", ".join(baseline_profiles)
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
    if rel.parts and rel.parts[0] == "System":
        # CMake installs launchd resources in the lower Darling template. Do
        # not create an upper copy: that would change union precedence and
        # diverge from the official install tree.
        return [prefix / "libexec/darling" / rel]
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
