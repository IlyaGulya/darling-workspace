"""Runtime RED proof planning helpers for ``west test``."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from shlex import quote
from typing import Any

import yaml

from test_runtime_identity import runtime_identity
import test_runtime_cache


ROOTLESS_BOOTSTRAP_RESOURCE = "rootless-bootstrap"
ROOTLESS_BOOTSTRAP_TARGET = "rootless_bootstrap"
ROOTLESS_BOOTSTRAP_MANIFEST = "darling-rootless-bootstrap.json"
RUNTIME_MODE_MARKER_NAME = ".darling-runtime-mode-v1"
RUNTIME_MODE_NAMES = {
    "privileged-overlay",
    "privileged-copy",
    "privileged-eunion",
    "rootless-eunion",
}
ROOTLESS_TOOLCHAIN_RESOURCE = "rootless-toolchain"
ROOTLESS_TOOLCHAIN_TARGET = "rootless_toolchain"
ROOTLESS_TOOLCHAIN_MANIFEST = "darling-rootless-toolchain.json"
GUEST_TOOLCHAIN_RESOURCE = "darling-command-line-tools"
COMPILER_LAUNCHERS = frozenset({"ccache"})
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
        "darling/src/external/bash",
    }
).union(ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES)
ROOTLESS_NO_MOUNT_RUNTIME_RESOURCES = frozenset(
    {ROOTLESS_BOOTSTRAP_RESOURCE}
)
ROOTLESS_TOOLCHAIN_RUNTIME_RESOURCES = frozenset(
    {ROOTLESS_BOOTSTRAP_RESOURCE, ROOTLESS_TOOLCHAIN_RESOURCE}
)
_RUNTIME_RESOURCES = ROOTLESS_TOOLCHAIN_RUNTIME_RESOURCES
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


@dataclass(frozen=True)
class RuntimeComponentFile:
    """A built file and its source-declared filesystem placement."""

    source: Path
    placement: str = "runtime"


def load_runtime_component_manifest(
    build_root: Path, resource: str = ROOTLESS_BOOTSTRAP_RESOURCE
) -> dict[str, RuntimeComponentFile]:
    """Load one CMake-owned runtime component and validate its boundary.

    CMake owns the component's target-to-guest-path mapping. West only consumes
    its generated product metadata and refuses paths that escape the disposable
    build tree. ``entrypoints`` name built executables; ``resources`` name
    source-owned runtime files such as launchd plists. The runtime closure code
    separately selects only Mach-O entrypoints as dylib-dependency roots.
    Schema 2 adds ``placement: lower`` for installed defaults. Such resources
    never replace upper-prefix configuration. Schema 1 cannot declare placement,
    so an older consumer rejects a schema-2 producer rather than ignoring it.
    """

    manifest_names = {
        ROOTLESS_BOOTSTRAP_RESOURCE: ROOTLESS_BOOTSTRAP_MANIFEST,
        ROOTLESS_TOOLCHAIN_RESOURCE: ROOTLESS_TOOLCHAIN_MANIFEST,
    }
    try:
        manifest_name = manifest_names[resource]
    except KeyError as exc:
        raise ValueError(f"unknown runtime component resource {resource!r}") from exc
    manifest_path = build_root / manifest_name
    try:
        data = json.loads(manifest_path.read_text())
    except OSError as exc:
        raise ValueError(
            f"cannot read runtime component manifest {manifest_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid runtime component manifest {manifest_path}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict) or data.get("schema") not in (1, 2):
        raise ValueError(f"runtime component {resource!r} manifest must have schema 1 or 2")
    entries = data.get("entrypoints")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"runtime component {resource!r} needs non-empty entrypoints")
    resources = data.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError(f"runtime component {resource!r} resources must be a list")

    resolved_root = build_root.resolve()
    deployments: dict[str, RuntimeComponentFile] = {}

    def load_entries(items: list[Any], kind: str, *, executable: bool) -> None:
        for index, entry in enumerate(items):
            label = "entry" if kind == "entrypoints" else "resource"
            if not isinstance(entry, dict):
                raise ValueError(
                    f"runtime component {resource!r} {label} {index} must be a mapping"
                )
            target = entry.get("target")
            guest_path = entry.get("guest_path")
            host_path = entry.get("host_path")
            placement = entry.get("placement", "runtime")
            if (
                ("placement" in entry and data["schema"] != 2)
                or placement not in ("runtime", "lower")
                or (executable and placement != "runtime")
            ):
                raise ValueError(
                    f"runtime component {resource!r} {label} {index} has invalid placement {placement!r}"
                )
            if not all(
                isinstance(value, str) and value for value in (target, guest_path, host_path)
            ):
                raise ValueError(
                    f"runtime component {resource!r} {label} {index} needs target, guest_path, and host_path"
                )
            guest = Path(guest_path)
            if not guest.is_absolute() or ".." in guest.parts:
                raise ValueError(
                    f"runtime component {resource!r} {label} {target!r} has invalid guest path {guest_path!r}"
                )
            relative_guest_path = guest_path.removeprefix("/")
            if not relative_guest_path:
                raise ValueError(
                    f"runtime component {resource!r} {label} {target!r} cannot deploy at /"
                )
            source = Path(host_path)
            if not source.is_absolute():
                raise ValueError(
                    f"runtime component {resource!r} {label} {target!r} has non-absolute host path {host_path!r}"
                )
            resolved_source = source.resolve()
            if not resolved_source.is_relative_to(resolved_root):
                raise ValueError(
                    f"runtime component {resource!r} {label} {target!r} escapes build root: {host_path}"
                )
            if not resolved_source.is_file():
                requirement = "built executable" if executable else "regular resource file"
                raise ValueError(
                    f"runtime component {resource!r} {label} {target!r} is not a {requirement}: {host_path}"
                )
            if executable and not resolved_source.stat().st_mode & 0o111:
                raise ValueError(
                    f"runtime component {resource!r} entry {target!r} is not a built executable: {host_path}"
                )
            if relative_guest_path in deployments:
                raise ValueError(
                    f"runtime component {resource!r} has duplicate guest path {guest_path!r}"
                )
            deployments[relative_guest_path] = RuntimeComponentFile(
                resolved_source, placement
            )

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
        runtime_mode = profile.get("runtime-mode")
        guest_toolchain = profile.get("guest-toolchain")
        compiler_launcher = profile.get("compiler-launcher")
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
        if runtime_mode is not None and (
            not isinstance(runtime_mode, str)
            or runtime_mode not in RUNTIME_MODE_NAMES
        ):
            raise ValueError(
                f"runtime profile {name!r} has invalid runtime-mode "
                f"{runtime_mode!r}"
            )
        if bootstrap == "rootless-no-mount" and runtime_mode != "rootless-eunion":
            raise ValueError(
                f"runtime profile {name!r} rootless-no-mount must declare "
                "runtime-mode: rootless-eunion"
            )
        if guest_toolchain is not None and guest_toolchain != GUEST_TOOLCHAIN_RESOURCE:
            raise ValueError(
                f"runtime profile {name!r} has unknown guest-toolchain "
                f"{guest_toolchain!r}"
            )
        if compiler_launcher is not None:
            if (
                not isinstance(compiler_launcher, str)
                or compiler_launcher not in COMPILER_LAUNCHERS
            ):
                raise ValueError(
                    f"runtime profile {name!r} has unsupported compiler-launcher "
                    f"{compiler_launcher!r}; allowed values: "
                    + ", ".join(sorted(COMPILER_LAUNCHERS))
                )
            if purpose not in {
                "prefix-baseline",
                "guest-toolchain-provisioning",
            }:
                raise ValueError(
                    f"runtime profile {name!r} may use compiler-launcher only for "
                    "bootstrap-capable profiles"
                )
        if purpose not in {"runtime", "prefix-baseline", "guest-toolchain-provisioning"}:
            raise ValueError(
                f"runtime profile {name!r} has unknown purpose {purpose!r}"
            )
        if purpose == "prefix-baseline" and bootstrap != "rootless-no-mount":
            raise ValueError(
                f"runtime profile {name!r} prefix-baseline must use rootless-no-mount"
            )
        if purpose == "guest-toolchain-provisioning":
            if bootstrap != "rootless-no-mount":
                raise ValueError(
                    f"runtime profile {name!r} guest-toolchain-provisioning must use rootless-no-mount"
                )
            if guest_toolchain != GUEST_TOOLCHAIN_RESOURCE:
                raise ValueError(
                    f"runtime profile {name!r} guest-toolchain-provisioning needs "
                    f"guest-toolchain: {GUEST_TOOLCHAIN_RESOURCE}"
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
            if purpose == "guest-toolchain-provisioning":
                toolchain_artifacts = [
                    artifact
                    for artifact in artifacts
                    if isinstance(artifact, dict)
                    and runtime_artifact_has_resource(artifact, ROOTLESS_TOOLCHAIN_RESOURCE)
                ]
                if len(toolchain_artifacts) != 1:
                    raise ValueError(
                        f"runtime profile {name!r} guest-toolchain-provisioning needs exactly one "
                        f"{ROOTLESS_TOOLCHAIN_RESOURCE!r} resource"
                    )
                toolchain_artifact = toolchain_artifacts[0]
                if toolchain_artifact.get("build-targets") != [ROOTLESS_TOOLCHAIN_TARGET]:
                    raise ValueError(
                        f"runtime profile {name!r} {ROOTLESS_TOOLCHAIN_RESOURCE} resource must build "
                        f"only {ROOTLESS_TOOLCHAIN_TARGET!r}"
                    )
                if runtime_artifact_deploy_paths(toolchain_artifact):
                    raise ValueError(
                        f"runtime profile {name!r} {ROOTLESS_TOOLCHAIN_RESOURCE} resource must not "
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
        if runtime_mode is not None:
            normalized[name]["runtime-mode"] = runtime_mode
        if guest_toolchain is not None:
            normalized[name]["guest-toolchain"] = guest_toolchain
        if compiler_launcher is not None:
            normalized[name]["compiler-launcher"] = compiler_launcher
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
    guest_toolchain: str | None = None
    compiler_launcher: str | None = None
    bootstrap: str | None = None
    runtime_mode: str | None = None
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
        candidate_runtime_mode = definition.get("runtime-mode")
        if candidate_runtime_mode is not None:
            if runtime_mode is not None and runtime_mode != candidate_runtime_mode:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on runtime mode "
                    f"{candidate_runtime_mode}"
                )
            runtime_mode = candidate_runtime_mode
        candidate_toolchain = definition.get("guest-toolchain")
        if candidate_toolchain is not None:
            if guest_toolchain is not None and guest_toolchain != candidate_toolchain:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on guest toolchain "
                    f"{candidate_toolchain}"
                )
            guest_toolchain = candidate_toolchain
        candidate_launcher = definition.get("compiler-launcher")
        if candidate_launcher is not None:
            if compiler_launcher is not None and compiler_launcher != candidate_launcher:
                raise ValueError(
                    f"runtime profile {name!r} conflicts on compiler launcher "
                    f"{candidate_launcher}"
                )
            compiler_launcher = candidate_launcher
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
    if runtime_mode is not None:
        result["runtime-mode"] = runtime_mode
    if guest_toolchain is not None:
        result["guest-toolchain"] = guest_toolchain
    if compiler_launcher is not None:
        result["compiler-launcher"] = compiler_launcher
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
        bootstrap_only_profiles = [
            profile
            for profile in profiles
            if definitions[profile].get("purpose")
            in {"prefix-baseline", "guest-toolchain-provisioning"}
        ]
        if bootstrap_only_profiles:
            raise ValueError(
                f"CTest test {name!r} cannot select bootstrap-only runtime profile(s): "
                + ", ".join(bootstrap_only_profiles)
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
        if selection.get("index") is not None:
            group.setdefault("indices", []).append(selection["index"])
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


def runtime_deploy_targets(
    prefix: Path,
    deploy_path: str,
    *,
    rootless_no_mount: bool = False,
    placement: str = "runtime",
) -> list[Path]:
    """Return prefix file targets for one runtime artifact deploy path.

    Darling system paths under ``usr`` exist in both the guest-visible prefix
    root and the base root under ``libexec/darling``. Runtime proof deploys must
    swap both copies so the next guest launch cannot accidentally run stale
    code from the other view. A rootless no-mount prefix has the same two-view
    requirement for ``System`` paths because it cannot rely on the overlay to
    expose the lower template to the guest loader.
    Source-declared lower defaults bypass upper copies so configured or empty
    upper files remain authoritative across deployment and restoration.
    """

    rel = Path(deploy_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"guest-runtime-deploy deploy path must be relative: {deploy_path}")
    if placement == "lower":
        lower_root = prefix.resolve() / "libexec/darling"
        if not (lower_root / rel).resolve().is_relative_to(lower_root):
            raise ValueError(
                f"guest-runtime-deploy lower resource escapes installed root: {deploy_path}"
            )
        return [prefix / "libexec/darling" / rel]
    if placement != "runtime":
        raise ValueError(f"guest-runtime-deploy invalid placement: {placement!r}")
    if rel.parts and rel.parts[0] == "usr":
        return [prefix / "libexec/darling" / rel, prefix / rel]
    if rel.parts and rel.parts[0] == "System":
        targets = [prefix / "libexec/darling" / rel]
        if rootless_no_mount:
            targets.append(prefix / rel)
        return targets
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


# A source-profile preflight resolves immutable base refs from the mirror before
# it can decide applicability. When that fetch cannot reach the repository the
# verifier exits non-zero with git's own transport message, and reporting it as a
# profile defect sends the reader to rebase a profile that is fine. These markers
# are git's; the first is the message observed when the mirror was momentarily
# unreachable during a runtime preflight.
TRANSIENT_PREFLIGHT_MARKERS = (
    "Could not read from remote repository",
    "Could not resolve host",
    "Connection timed out",
    "Connection refused",
    "Operation timed out",
    "The remote end hung up unexpectedly",
    "unable to access",
    "early EOF",
)


def transient_preflight_failure(output: str) -> bool:
    """Return whether a failed preflight is a transport failure, not a defect."""

    return any(marker in output for marker in TRANSIENT_PREFLIGHT_MARKERS)


def preflight_retry_allowed(attempt: int, output: str, *, max_attempts: int = 2) -> bool:
    """Return whether a failed applicability preflight should be retried.

    Only a transport failure is retried, and only once: the verifier is
    read-only, so a retry cannot duplicate a measurement, and a persistent
    failure still has to be reported rather than hidden behind attempts.
    """

    return attempt + 1 < max_attempts and transient_preflight_failure(output)


def applicability_preflight_advice(profile: str, output: str) -> str:
    """Return the closing advice for a failed applicability preflight."""

    if transient_preflight_failure(output):
        return (
            "The verifier could not fetch this profile's immutable base refs: that is "
            "a transport failure between the workspace and the mirror, not a defect in "
            "the profile. Check access to the mirror and retry; nothing was deployed or "
            "measured, so there is no test result to interpret."
        )
    return (
        f"Repair or rebase that profile with `west patch verify --profile {profile}` "
        "before retrying; this is not a runtime test failure."
    )


class RuntimePlanMixin:

    def _display_invocation(self, invocation) -> str:
        if invocation.get("darling_cmake_target_fixture"):
            return invocation["display"]
        if invocation.get("guest_argv_fixture"):
            return invocation["display"]
        if invocation.get("guest_macho_fixture"):
            return invocation["display"]
        if invocation.get("guest_command_fixture"):
            return invocation["display"]
        if invocation.get("diag", "bare") == "bare":
            return invocation["display"]
        if invocation.get("guest_c_fixture"):
            executor = getattr(self, "_executor", None) or "<darling-debug-runner>"
            args = [
                executor,
                "run",
                "--name",
                f"west-test-{invocation['name']}",
                "--bundle-root",
                str(getattr(self, "_bundle_root", "~/work/darling-debug")),
                "--timeout-seconds",
                str(invocation.get("debug_timeout_seconds", invocation.get("timeout_seconds", 600))),
                "--",
                "<guest-c-fixture>",
                invocation["display"],
            ]
            return " ".join(quote(str(arg)) for arg in args)
        args = self._debug_runner_args(invocation, display_only=True)
        return " ".join(quote(str(arg)) for arg in args)

    def _runtime_diagnostic_output(self, invocation) -> str:
        """Read trace files owned by the invocation and its runtime provider."""

        parts = []
        seen_paths = set()
        for path in (
            *invocation.get("_host_trace_paths", []),
            *invocation.get("_runtime_diagnostic_trace_paths", []),
        ):
            path = Path(path)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
        return "".join(parts)

    def _bound_runtime_reuse_store(
        self, plan: test_runtime_cache.RuntimeReusePlan
    ) -> None:
        """Enforce the reuse store bound once per invocation.

        Sizing a materialised forest costs a walk over several gigabytes, so the
        bound is applied on the first runtime profile of a run and then left
        alone rather than re-measured for every profile.
        """

        if getattr(self, "_runtime_reuse_bounded", False):
            return
        self._runtime_reuse_bounded = True
        pruned = test_runtime_cache.prune(
            plan.store,
            test_runtime_cache.max_bytes(os.environ),
            protect=[plan.source_entry],
        )
        if pruned["evicted"]:
            self.inf(
                f"  runtime reuse evicted {len(pruned['evicted'])} entry(s), "
                f"{pruned['evicted_bytes']} bytes"
            )

    def _runtime_reuse_plan(
        self,
        *,
        profile_name: str,
        definition: dict,
        proof: dict,
        patch: dict,
        prefix_text: str,
        omit_patch: bool,
    ) -> test_runtime_cache.RuntimeReusePlan | None:
        """Return the identity-keyed reuse plan for one runtime profile.

        The plan is derived from the same identity the retained-runtime path
        already refuses to reuse across, plus the configure command the build
        will run, so a repeat acceptance run reuses instead of rebuilding while
        any source, metadata, define or toolchain change forces a rebuild.
        """
        from test_runtime_build import RuntimeBuildService

        store = test_runtime_cache.cache_root(
            Path(self.manifest.repo_abspath), os.environ
        )
        if store is None:
            return None
        identity = runtime_identity(
            topdir=Path(self.topdir),
            manifest_repo=Path(self.manifest.repo_abspath),
            profile_name=profile_name,
            definition=definition,
            launcher=Path(prefix_text) / "bin" / "darling",
        )
        source_key = test_runtime_cache.source_key(
            test_runtime_cache.source_identity(
                identity,
                omit_patch=omit_patch,
                patch_path=str(patch.get("path", "")),
                bad_profile=proof.get("bad-profile"),
                bad_revision=(
                    self._bad_revision(patch, proof) if omit_patch else None
                ),
            )
        )
        build_key = RuntimeBuildService(self).runtime_build_identity(
            proof,
            Path(prefix_text),
            store,
            source_key,
            configure_args=self._runtime_red_configure_args,
        )
        return test_runtime_cache.RuntimeReusePlan(
            store=store, source_key=source_key, build_key=build_key
        )

    @contextmanager
    def _guest_runtime_source_forest(
        self,
        patch,
        proof,
        *,
        omit_patch: bool,
        root: Path | None = None,
        evidence_session=None,
        reuse_key: str | None = None,
    ):
        """Create a temporary Darling source forest for a runtime build.

        The top-level Darling tree and every nested gitlink are detached local
        worktrees. When omit_patch is true, the target patch and explicit
        current-minus skips are removed to build the RED runtime. Otherwise the
        full active profile is materialized to build the GREEN runtime. This
        keeps live checkouts and the caller's prefix stable while giving CMake
        one coherent source root.
        """
        with self._runtime_source_materializer().guest_runtime_source_forest(
            patch,
            proof,
            omit_patch=omit_patch,
            root=root,
            evidence_session=evidence_session,
            reuse_key=reuse_key,
        ) as source_root:
            yield source_root

    def _cmake_cache_value(self, build_dir: Path, key: str) -> str | None:
        from test_runtime_build import RuntimeBuildService
        return RuntimeBuildService.cmake_cache_value(build_dir, key)

    def _runtime_red_configure_args(
        self, proof, prefix: Path, scratch_root: Path | None = None
    ) -> list[str]:
        from test_runtime_build import RuntimeBuildService
        return RuntimeBuildService(self).configure_args(proof, prefix, scratch_root)

    def _runtime_red_build_artifacts(
        self,
        source_root: Path,
        proof,
        prefix: Path,
        scratch_root: Path,
        *,
        label: str = "RED",
        allow_failure: bool = False,
        cache: tuple[Path, str] | None = None,
        on_reuse: Callable[[bool], None] | None = None,
    ) -> Path:
        from test_runtime_build import RuntimeBuildService
        return RuntimeBuildService(self).build_artifacts(
            source_root,
            proof,
            prefix,
            scratch_root,
            label=label,
            allow_failure=allow_failure,
            configure_args=self._runtime_red_configure_args,
            dump_command_tail=self._dump_command_tail,
            runner=self._run_bounded,
            timeout_seconds=getattr(self, "_runtime_build_timeout_seconds", None),
            cache=cache,
            on_reuse=on_reuse,
        )

    def _runtime_red_find_build_output(
        self, build_root: Path, deploy_path: str
    ) -> Path:
        from test_runtime_build import RuntimeBuildService
        return RuntimeBuildService(self).find_build_output(build_root, deploy_path)

    def _runtime_macho_inspect(self, path: Path, flag: str) -> str:
        from test_runtime_deploy import RuntimeDeploymentService
        return RuntimeDeploymentService(self).macho_inspect(path, flag)

    def _runtime_macho_dependencies(self, path: Path) -> list[str]:
        from test_runtime_deploy import RuntimeDeploymentService
        return RuntimeDeploymentService(self).macho_dependencies(path)

    def _runtime_macho_dylib_providers(
        self, build_root: Path, explicit: dict[str, Path]
    ) -> dict[str, Path]:
        from test_runtime_deploy import RuntimeDeploymentService
        return RuntimeDeploymentService(self).macho_dylib_providers(build_root, explicit)

    def _runtime_red_deploy_targets(self, prefix: Path, deploy_path: str) -> list[Path]:
        try:
            return runtime_deploy_targets(prefix, deploy_path)
        except ValueError:
            self.die(f"guest-runtime-deploy deploy path must be relative: {deploy_path}")

    @contextmanager
    def _runtime_red_deployed_artifacts(
        self,
        proof,
        build_root: Path,
        prefix: Path,
        *,
        label: str = "RED",
        restore_deployment: bool = True,
        lifecycle_env: dict[str, str] | None = None,
    ):
        from test_runtime_deploy import RuntimeDeploymentService
        with RuntimeDeploymentService(self).deployed(
            proof,
            build_root,
            prefix,
            label=label,
            restore_deployment=restore_deployment,
            lifecycle_env=lifecycle_env,
        ):
            yield

    def _display_guest_runtime_deploy_plan(self, proof) -> str:
        return describe_runtime_deploy_plan(proof)
