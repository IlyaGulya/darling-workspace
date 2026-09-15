"""CTest backend helpers for ``west test``."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from shlex import quote

from test_runtime import (
    load_ctest_runtime_profiles,
    partition_ctest_runtime_profiles,
)


def is_ctest_binding(test: dict) -> bool:
    """A registration reference, not a fixture runner with a build-time label."""
    return bool(test.get("ctest-label") or test.get("ctest-name")) and test.get("runner") in {None, "ctest"}


def ctest_reference_args(test: dict) -> list[str]:
    """Resolve existing labels and/or exact names without requiring name labels."""
    args = ["-L", test["ctest-label"]] if test.get("ctest-label") else []
    if test.get("ctest-name"):
        args += ["-R", ctest_test_name_regex([test["ctest-name"]])]
    return args


def ctest_registration_indices(catalogue: list[dict], selected: list[dict]) -> list[int]:
    """Map filtered JSON entries back to their indices in the configured suite."""
    def identity(test):
        properties = {item["name"]: item["value"] for item in test.get("properties", [])}
        return json.dumps([test["name"], test.get("command"), properties], sort_keys=True)

    positions = {}
    for index, registration in enumerate(catalogue, 1):
        positions.setdefault(identity(registration), []).append(index)
    indices = []
    for registration in selected:
        matches = positions.get(identity(registration), [])
        if len(matches) != 1:
            raise ValueError(f"CTest registration {registration['name']!r} is missing or ambiguous in its suite")
        indices.append(matches[0])
    return indices


def ctest_index_args(indices: list[int]) -> list[str]:
    if not indices:
        raise ValueError("an exact CTest selection needs at least one index")
    # Omitted range bounds mean ALL tests in CTest, not an empty range.
    values = [indices[0], indices[0], 1, *indices[1:]]
    return ["-I", ",".join(str(index) for index in values)]


def ctest_metadata_variants(
    test: dict, registrations: list[dict], *, default_env: str
) -> list[dict]:
    """Bind metadata to labelled variants or the configured host suite."""
    variants = []
    for registration in registrations:
        name = registration["name"]
        properties = {item["name"]: item["value"] for item in registration.get("properties", [])}
        labels = properties.get("LABELS", [])
        environments = {label[4:] for label in labels if label.startswith("env:")}
        if not environments:
            environments = {default_env}
        if len(environments) != 1 or not environments <= {"host", "darling", "macos"}:
            raise ValueError(f"CTest registration {name!r} has ambiguous environment labels")
        environment = next(iter(environments))
        # A scalar runs/env remains a restriction of this patch binding.
        if test.get("env") and test["env"] != environment:
            continue
        profiles = [label.removeprefix("runtime-profile:") for label in labels
                    if label.startswith("runtime-profile:")]
        if environment != "darling" and (
            test.get("runtime-profile")
            or profiles
            or set(test.get("requires", [])) & {"darling-prefix", "darling-eunion-prefix"}
            or (test.get("red-proof") or {}).get("mode") == "guest-runtime-deploy"
        ):
            raise ValueError(
                f"CTest registration {name!r}: Darling runtime ownership needs "
                "a Darling-scoped binding, separate from the native reference"
            )
        variant = dict(test)
        variant["env"] = environment
        variant["_ctest"] = {
            "name": name,
            "labels": labels,
            "profiles": profiles,
            "disabled": bool(properties.get("DISABLED", False)),
            "index": registration.get("_ctest_index"),
            "directory": properties.get("WORKING_DIRECTORY"),
        }
        if "diag" not in variant:
            diagnostics = {label[5:] for label in labels if label.startswith("diag:")}
            if len(diagnostics) == 1:
                variant["diag"] = diagnostics.pop()
        if environment == "darling":
            variant["requires"] = list(dict.fromkeys([*test.get("requires", []), "darling-prefix"]))
        variants.append(variant)
    return variants


def ctest_submodule_label_name(submodule: str) -> str:
    """Return the CTest submodule label suffix for a West project path/name."""
    name = Path(submodule).name
    if not name:
        raise ValueError(f"empty submodule selector: {submodule!r}")
    return name


def ctest_label_args(build_dir: Path, label: str) -> list[str]:
    return [
        "ctest",
        "--test-dir",
        str(build_dir),
        "--output-on-failure",
        "-L",
        label,
    ]


def ctest_label_display(build_dir: Path, label: str) -> str:
    return " ".join(quote(str(arg)) for arg in ctest_label_args(build_dir, label))


def ctest_selector_label_args(
    *,
    bead: str | None = None,
    env: str | None = None,
    diag: str | None = None,
    label: str | None = None,
    fuzz: bool = False,
    stress: bool = False,
    changed_submodules: list[str] | None = None,
    submodules: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    if bead:
        args += ["-L", f"^bead:{re.escape(bead)}$"]
    if env:
        args += ["-L", f"^env:{re.escape(env)}$"]
    if diag:
        args += ["-L", f"^diag:{re.escape(diag)}$"]
    if label:
        args += ["-L", label]
    if fuzz:
        args += ["-L", "^fuzz:true$"]
    if stress:
        args += ["-L", "^stress:true$"]
    submodule_names: list[str] = []
    for selector in [*(changed_submodules or []), *(submodules or [])]:
        name = ctest_submodule_label_name(selector)
        if name not in submodule_names:
            submodule_names.append(name)
    if submodule_names:
        alternation = "|".join(f"submod:{re.escape(name)}" for name in submodule_names)
        args += ["-L", f"^({alternation})$"]
    return args


def ctest_command(
    build_dir: Path,
    *,
    label_args: list[str] | None = None,
    list_only: bool = False,
    passthrough: list[str] | None = None,
) -> list[str]:
    args = ["ctest", "--test-dir", str(build_dir), "--output-on-failure"]
    args += list(label_args or [])
    if list_only:
        args.append("--show-only")
    args += list(passthrough or [])
    return args


def ctest_test_name_regex(names: list[str]) -> str:
    """Return an exact CTest regex for already-discovered test names."""

    if not names:
        raise ValueError("CTest runtime group needs at least one test name")
    # CTest's regex implementation accepts ordinary grouping but not Python's
    # non-capturing ``(?:...)`` syntax.
    return "^(" + "|".join(re.escape(name) for name in names) + ")$"


def ctest_runtime_group_passthrough(passthrough: list[str]) -> list[str]:
    """Drop CTest selectors after discovery has frozen an exact test group.

    Repeating ``-R`` is a union in CTest, so retaining a caller's selector
    alongside the group's exact regex can run tests from another runtime
    lifecycle. Labels, fixtures, and test ranges are likewise already reflected
    in JSON discovery. Keep only execution/reporting options for the replay.
    """

    selectors_with_value = {
        "-R",
        "--tests-regex",
        "-E",
        "--exclude-regex",
        "-I",
        "--tests-information",
        "-L",
        "--label-regex",
        "--fixture-exclude-any",
        "--fixture-exclude-setup",
        "--fixture-exclude-cleanup",
        "--fixture-required",
        "--fixture-setup",
        "--fixture-cleanup",
    }
    selectors_without_value = {"--rerun-failed", "--union"}
    result: list[str] = []
    index = 0
    while index < len(passthrough):
        argument = passthrough[index]
        if argument in selectors_without_value:
            index += 1
            continue
        if argument in selectors_with_value:
            if index + 1 >= len(passthrough):
                raise ValueError(f"CTest selector {argument} needs a value")
            index += 2
            continue
        result.append(argument)
        index += 1
    return result


def ctest_selection_command(
    build_dir: Path,
    *,
    label_args: list[str] | None = None,
    passthrough: list[str] | None = None,
) -> list[str]:
    """Return the machine-readable discovery command for a CTest selection.

    CTest owns filtering.  Consumers use this only to inspect properties of the
    exact tests CTest will run; it must never reimplement the selector logic.
    """

    return [
        "ctest",
        "--test-dir",
        str(build_dir),
        "--show-only=json-v1",
        *list(label_args or []),
        *list(passthrough or []),
    ]


def ctest_uses_prefix(*, env: str | None, list_only: bool) -> bool:
    """Whether a CTest selection owns a live Darling prefix lifecycle."""

    return env == "darling" and not list_only


class CtestSelectionMixin:

    def _display_ctest_label(self, label: str) -> str:
        build = self._testkit_dir() / "build"
        return ctest_label_display(build, label)

    def _ctest_catalogue(self, build: Path) -> list[dict]:
        discovery = self._run_bounded(
            ctest_selection_command(build), cwd=Path(self.topdir), env=None,
            timeout_seconds=30, capture_output=True,
        )
        if discovery.returncode:
            self._dump_command_tail("CTest catalogue discovery", discovery)
            self.die(f"could not discover CTest suite {build}")
        try:
            return json.loads(discovery.stdout)["tests"]
        except (KeyError, TypeError, ValueError) as error:
            self.die(f"invalid CTest catalogue in {build}: {error}")

    def _ensure_ctest_build(self, invocation=None) -> Path:
        if invocation and invocation.get("ctest_build"):
            build = Path(invocation["ctest_build"])
            built = getattr(self, "_compiled_ctest_builds", set())
            if build not in built:
                self._run_testkit_build_command("build", ["ninja", "-C", str(build)])
                self._compiled_ctest_builds = built | {build}
            return build
        build = getattr(self, "_ctest_build", None)
        if build is not None:
            return build
        build = self._configure_and_build(self._testkit_dir(), self._executor)
        self._ctest_build = build
        return build

    def _ctest_label_args(self, invocation) -> list[str]:
        build = self._ensure_ctest_build(invocation)
        if invocation.get("ctest_index") is not None:
            label_args = ctest_command(build, passthrough=ctest_index_args([invocation["ctest_index"]]))
        elif invocation.get("ctest_name"):
            label_args = ctest_command(build, passthrough=["-R", ctest_test_name_regex([invocation["ctest_name"]])])
        else:
            label_args = ctest_label_args(build, invocation["ctest_label"])
        discovery = self._run_bounded(
            ctest_selection_command(build, label_args=label_args[4:]),
            cwd=Path(self.topdir),
            env=None,
            timeout_seconds=30,
            capture_output=True,
        )
        if discovery.returncode:
            self._dump_command_tail("CTest label discovery", discovery)
            self.die(f"could not discover CTest label {invocation['ctest_label']!r}")
        try:
            selected = json.loads(discovery.stdout).get("tests", [])
        except json.JSONDecodeError as error:
            self.die(f"CTest label discovery returned invalid JSON: {error}")
        if not selected:
            self.die(
                f"CTest label {invocation['ctest_label']!r} selected no tests in {build}; "
                "refusing a false GREEN"
            )
        if invocation.get("ctest_index") is not None:
            identities = [
                (test["name"], next((item["value"] for item in test.get("properties", [])
                                    if item["name"] == "WORKING_DIRECTORY"), None))
                for test in selected
            ]
            if identities.count((invocation["ctest_name"], invocation["ctest_directory"])) != 1:
                self.die("CTest registration changed after discovery; refusing to run a different case")
        return label_args

    def _ctest_cmake_defines(
        self, invocation, *, source_override=None, source_root=None
    ) -> dict[str, str]:
        """Return CMake inputs needed by a source-bound CTest invocation."""

        defines: dict[str, str] = {}
        if source_override:
            defines[str(source_override)] = str(
                source_root
                if source_root is not None
                else self._project_path(invocation["source_module"])
            )
        if invocation.get("ctest_label") == "eunion-host":
            defines["DARLING_ENABLE_EUNION_HOST_SUITE"] = "ON"
        return defines

    @contextmanager
    def _ctest_source_override_context(self, invocation):
        override = invocation.get("ctest_source_override")
        if not override or invocation.get("ctest_build"):
            yield invocation
            return
        with tempfile.TemporaryDirectory(prefix="west-ctest-source-") as temp:
            configured = dict(invocation)
            configured["ctest_build"] = self._configure_and_build(
                self._testkit_dir(),
                self._executor,
                darling_launcher=self._resolve_darling_launcher(self._prefix),
                prefix=self._prefix,
                bundle_root=str(getattr(self, "_bundle_root", "")),
                build_dir=Path(temp) / "build",
                cmake_defines=self._ctest_cmake_defines(
                    invocation, source_override=override
                ),
            )
            yield configured

    def _ctest_runtime_profile_definitions(self) -> dict[str, dict]:
        path = self._testkit_dir() / "runtime-profiles.yml"
        try:
            return load_ctest_runtime_profiles(path)
        except (OSError, ValueError) as error:
            self.die(f"invalid CTest runtime profile definitions at {path}: {error}")

    def _selected_ctest_runtime_groups(
        self,
        build: Path,
        label_args: list[str],
        passthrough: list[str],
        additional_profiles: list[str],
        *,
        env: str | None = None,
        diag: str | None = None,
    ) -> list[dict]:
        """Return lifecycle groups for exactly the CTest-selected cases."""

        discovery = self._run_bounded(
            ctest_selection_command(
                build, label_args=label_args, passthrough=passthrough
            ),
            cwd=Path(self.topdir),
            env=None,
            timeout_seconds=30,
            capture_output=True,
        )
        if discovery.returncode:
            self._dump_command_tail("CTest runtime profile discovery", discovery)
            self.die("could not discover CTest runtime profiles")
        try:
            payload = json.loads(discovery.stdout)
        except json.JSONDecodeError as error:
            self.die(f"CTest runtime profile discovery returned invalid JSON: {error}")
        if not payload.get("tests"):
            self.die("CTest selectors matched no registrations; refusing an empty selection")
        try:
            catalogue = self._ctest_catalogue(build) if label_args or passthrough else payload["tests"]
            indices = ctest_registration_indices(catalogue, payload["tests"])
        except ValueError as error:
            self.die(f"invalid scoped CTest selection: {error}")
        for registration, index in zip(payload["tests"], indices):
            registration["_ctest_index"] = index
        try:
            variants = ctest_metadata_variants(
                {}, payload["tests"], default_env="macos" if sys.platform == "darwin" else "host"
            )
        except ValueError as error:
            self.die(f"invalid CTest environment selection: {error}")
        selections = [
            {
                "name": variant["_ctest"]["name"],
                "index": variant["_ctest"]["index"],
                "darling": variant["env"] == "darling",
                "profiles": variant["_ctest"]["profiles"],
            }
            for variant in variants
            if (not env or variant["env"] == env)
            and (not diag or self._resolved_diag(variant) == diag)
        ]
        if not selections:
            self.die(f"CTest selectors matched no applicable registrations (env={env or 'any'}, diag={diag or 'any'})")
        try:
            return partition_ctest_runtime_profiles(
                self._ctest_runtime_profile_definitions(),
                selections,
                additional_profiles,
            )
        except ValueError as error:
            self.die(f"invalid CTest runtime profile selection: {error}")

    @contextmanager
    def _ctest_runtime_profile_context(self, profiles: list[str]):
        """Build and temporarily deploy the runtime declared by selected CTest cases."""

        if not profiles:
            prefix_text = getattr(self, "_prefix", None)
            runtime_env = os.environ.copy()
            if prefix_text:
                runtime_env.update(self._darling_prefix_env(prefix_text))
                launcher = self._resolve_darling_launcher(prefix_text)
                if launcher:
                    runtime_env["DARLING"] = launcher
                    runtime_env["DARLING_LAUNCHER"] = launcher
            yield runtime_env
            return
        with self._runtime_profile_deployment_context(
            profiles, label_prefix="CTest", retain_deployment=False
        ) as deployment:
            yield deployment.env

    @contextmanager
    def _metadata_ctest_selection(self, selected, *, env, diag, label, additional_profiles):
        """Resolve references in the active source profile before prefix acquisition."""
        from test_selection import select_metadata_tests
        resolved = []
        builds = {}
        catalogues = {}
        unavailable = []
        with ExitStack() as stack:
            for patch, test in selected:
                if not is_ctest_binding(test):
                    resolved.append((patch, test))
                    continue
                invocation = self._test_invocation(patch, test)
                override = invocation.get("ctest_source_override")
                defines = self._ctest_cmake_defines(invocation, source_override=override)
                scope = (str(self._testkit_dir()), tuple(sorted(defines.items())))
                if scope not in builds:
                    scratch = stack.enter_context(tempfile.TemporaryDirectory(prefix="west-ctest-selection-"))
                    builds[scope] = self._configure_and_build(
                        self._testkit_dir(), self._executor,
                        darling_launcher=self._resolve_darling_launcher(self._prefix),
                        prefix=self._prefix,
                        bundle_root=str(getattr(self, "_bundle_root", "")),
                        build_dir=Path(scratch) / "build",
                        cmake_defines=defines,
                        compile_tests=False,
                    )
                    catalogues[scope] = self._ctest_catalogue(builds[scope])
                build = builds[scope]
                discovery = self._run_bounded(
                    ctest_selection_command(build, label_args=ctest_reference_args(test)),
                    cwd=Path(self.topdir), env=None, timeout_seconds=30, capture_output=True,
                )
                if discovery.returncode:
                    self._dump_command_tail("CTest reference discovery", discovery)
                    self.die(f"{patch['path']}: could not resolve CTest reference {ctest_reference_args(test)}")
                try:
                    registrations = json.loads(discovery.stdout)["tests"]
                    if not registrations:
                        raise ValueError(f"missing CTest reference {ctest_reference_args(test)}")
                    indices = ctest_registration_indices(catalogues[scope], registrations)
                    for registration, index in zip(registrations, indices):
                        registration["_ctest_index"] = index
                    variants = ctest_metadata_variants(
                        test, registrations, default_env="macos" if sys.platform == "darwin" else "host"
                    )
                    chosen = select_metadata_tests(
                        {"patches": [{**patch, "tests": variants}]},
                        patch_path=None, bead=None, env=env, diag=diag, label=label,
                        red_only=False, resolved_diag=self._resolved_diag,
                    ).selected
                    for _, variant in chosen:
                        registration = variant["_ctest"]
                        profiles = list(dict.fromkeys([
                            *([variant["runtime-profile"]] if variant.get("runtime-profile") else []),
                            *registration["profiles"],
                        ]))
                        groups = partition_ctest_runtime_profiles(
                            self._ctest_runtime_profile_definitions(),
                            [{"name": registration["name"], "darling": variant["env"] == "darling",
                              "profiles": profiles}],
                            additional_profiles,
                        )
                        registration["profiles"] = groups[0]["profiles"]
                        registration["build"] = str(build)
                        resolved.append((patch, variant))
                    if not chosen:
                        available = sorted({variant["env"] for variant in variants})
                        unavailable.append(
                            f"{patch['path']}:{test.get('name') or ctest_reference_args(test)}: "
                            f"available environments: {', '.join(available) or 'none'}"
                        )
                except (KeyError, TypeError, ValueError) as error:
                    self.die(f"{patch['path']}: invalid CTest reference: {error}")
            if not resolved:
                detail = "; ".join(unavailable) or "metadata selectors matched no runnable bindings"
                self.die(f"no tests selected (env={env or 'any'}): {detail}")
            # The outer prefix lease shuts down the existing runtime before
            # entering a provider. Its launcher mode must already be known.
            launcher_env = {}
            definitions = None
            for _, test in resolved:
                profiles = list(test.get("_ctest", {}).get("profiles", []))
                if test.get("runtime-profile"):
                    profiles.append(test["runtime-profile"])
                for name in dict.fromkeys(profiles):
                    if definitions is None:
                        definitions = self._ctest_runtime_profile_definitions()
                    if name not in definitions:
                        self.die(f"unknown runtime profile: {name}")
                    for key, value in definitions[name].get("launcher-env", {}).items():
                        value = str(value)
                        if key in launcher_env and launcher_env[key] != value:
                            self.die(f"selected runtime profiles conflict on launcher environment {key}")
                        launcher_env[key] = value
            previous_prefix_env = getattr(self, "_prefix_env", {})
            self._prefix_env = {**previous_prefix_env, **launcher_env}
            try:
                yield resolved
            finally:
                self._prefix_env = previous_prefix_env
