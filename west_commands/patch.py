"""Generic patch profiles for the Darling West workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

import yaml
from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_git import (
    git,
    git_for_patch_application,
    git_for_temporary_patch_application,
)
import test_manifest
from test_runtime import ROOTLESS_BOOTSTRAP_RESOURCE, ROOTLESS_BOOTSTRAP_TARGET


DEFAULT_EXPORT_MAX_LINES = 200_000
DEFAULT_EXPORT_MAX_GROWTH = 20
DEFAULT_EXPORT_MAX_BYTES = 1_000_000

_GENERATED_ARTIFACT_PATHS = (
    re.compile(
        r"(^|/)tests/[^/]*"
        r"(?:snapshot|stat-(?:before|after)|smaps|census|handoff)"
        r"[^/]*\.(?:json|jsonl|txt|md)$",
        re.IGNORECASE,
    ),
    re.compile(r"(^|/)tools/.*/(?:BUILD|DCC).*-OUT\.txt$", re.IGNORECASE),
    re.compile(r"(^|/)tools/.*/audit/[^/]+\.txt$", re.IGNORECASE),
)
_LEGACY_AUTOMATION_TRAILER = re.compile(
    r"^Co-Authored-By:\s*(?:Claude|Codex)\b.*$",
    re.IGNORECASE,
)


class ExportPlan(NamedTuple):
    patch: dict
    output: Path
    commit: str
    exported: bytes
    checksum: str
    patch_changed: bool


def format_patch_command(patch, commit: str) -> list[str]:
    revision = (
        f"{patch['source-base']}..{commit}"
        if patch.get("source-base")
        else commit
    )
    command = [
        "git",
        "format-patch",
        "--stdout",
        "--no-signature",
        "--no-numbered",
        "--subject-prefix=PATCH",
        "--full-index",
        "--binary",
        "--no-renames",
    ]
    if not patch.get("source-base"):
        command.append("-1")
    command.append(revision)
    return command


def generated_patch_artifacts(exported: bytes) -> list[str]:
    """Return generated evidence files carried by an exported patch.

    The patch format gives us a structured file boundary. Inspecting those
    paths catches committed snapshots and capture output without matching
    arbitrary source text or rejecting real test programs.
    """

    artifacts: list[str] = []
    current_path: str | None = None
    deleted = False

    def finish() -> None:
        if current_path is None or deleted:
            return
        if any(
            pattern.search(current_path) for pattern in _GENERATED_ARTIFACT_PATHS
        ):
            if current_path not in artifacts:
                artifacts.append(current_path)

    for line in exported.decode(errors="replace").splitlines():
        match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
        if match:
            finish()
            current_path = match.group(2)
            deleted = False
        elif current_path is not None and line.startswith("deleted file mode "):
            deleted = True
    finish()
    return artifacts


def describe_generated_patch_artifacts(artifacts: list[str]) -> str:
    return (
        "generated evidence artifact(s): "
        + ", ".join(artifacts)
        + "; remove snapshots/capture output from the source branch before export"
    )


def legacy_automation_trailers(exported: bytes) -> list[str]:
    """Return forbidden Claude/Codex trailers from patch-mail metadata."""

    return [
        line
        for line in exported.decode(errors="replace").splitlines()
        if _LEGACY_AUTOMATION_TRAILER.match(line)
    ]


def describe_legacy_automation_trailers(trailers: list[str]) -> str:
    return (
        f"legacy automation trailer(s): {len(trailers)} Claude/Codex "
        "Co-Authored-By line(s); rewrite the local commit messages before export"
    )


class DarlingPatch(WestCommand):
    def __init__(self):
        super().__init__("patch", "", "Apply tracked Darling patch profiles")
        # Set per-invocation in do_run from the profile's `base-profile` key.
        self._base_profile = None

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)
        for action in ("list", "verify", "export", "apply", "clean", "status", "check"):
            command = subparsers.add_parser(action)
            command.add_argument("--profile", default="homebrew")
            if action == "verify":
                command.add_argument(
                    "--applicability-only",
                    action="store_true",
                    help="verify patch files and applicability at manifest revisions without requiring local source branches",
                )
            if action == "export":
                command.add_argument(
                    "--patch",
                    help="export only one patch entry from the selected profile",
                )
                command.add_argument(
                    "--check",
                    action="store_true",
                    help="verify exported patch files and metadata without writing",
                )
                command.add_argument(
                    "--allow-large-output",
                    action="store_true",
                    help="allow writing unusually large exported patch output",
                )
            if action == "apply":
                command.add_argument("--roll-back", action="store_true")
            if action == "clean":
                command.add_argument("--force", action="store_true")
            if action == "status":
                command.add_argument(
                    "--strict",
                    action="store_true",
                    help="exit non-zero if any patch is MISSING or CONFLICT",
                )
            if action == "check":
                command.add_argument(
                    "--strict",
                    action="store_true",
                    help="exit non-zero if a non-doc patch has no tests/exception",
                )
                command.add_argument(
                    "--quality",
                    action="store_true",
                    help="also report low-noise test quality warnings",
                )
                command.add_argument(
                    "--strict-quality",
                    action="store_true",
                    help="exit non-zero for test quality warnings; implies --quality",
                )
        return parser

    def do_run(self, args, unknown):
        if unknown:
            self.die(f"unknown arguments: {' '.join(unknown)}")

        manifest_repo = Path(self.manifest.repo_abspath)
        profile_dir = manifest_repo / "patches" / args.profile
        profile_path = profile_dir / "patches.yml"
        if not profile_path.is_file():
            self.die(f"patch profile not found: {profile_path}")

        try:
            profile = test_manifest.load_test_profile(profile_path)
        except test_manifest.ManifestError as error:
            self.die(str(error))
        patches = profile.get("patches", [])
        # Optional stacking: a profile may declare `base-profile: <name>` to be
        # applied ON TOP of another profile's integration branch instead of the
        # raw manifest revision (e.g. a `perf` profile that stacks on `homebrew`
        # because a bootable build needs the homebrew fixes underneath). When
        # unset, the base is the manifest revision (the original behaviour).
        self._base_profile = profile.get("base-profile")
        if self._base_profile == args.profile:
            self.die(f"{args.profile}: base-profile cannot be itself")
        if args.action == "list":
            self._list(patches)
        elif args.action == "verify":
            self._verify(
                profile_dir,
                patches,
                require_source_branches=not args.applicability_only,
            )
        elif args.action == "export":
            self._export(
                profile_path,
                profile_dir,
                profile,
                patches,
                args.patch,
                args.check,
                args.allow_large_output,
            )
        elif args.action == "status":
            self._status(profile_dir, patches, args.strict)
        elif args.action == "check":
            self._check(
                profile_dir,
                patches,
                args.strict,
                quality=args.quality or args.strict_quality,
                strict_quality=args.strict_quality,
            )
        elif args.action == "apply":
            self._apply(
                args.profile,
                profile_dir,
                patches,
                profile["integration-date"],
                args.roll_back,
            )
        else:
            self._clean(args.profile, patches, args.force)

    def _projects(self):
        result = {}
        for project in self.manifest.projects:
            result[project.name] = project
            result[project.path] = project
        return result

    def _group(self, patches):
        grouped = OrderedDict()
        for patch in patches:
            grouped.setdefault(patch["module"], []).append(patch)
        return grouped

    def _repo(self, module: str) -> Path:
        project = self._projects().get(module)
        if project is None:
            self.die(f"unknown West project: {module}")
        return Path(project.abspath)

    def _project_path(self, ref: str) -> Path | None:
        project = self._projects().get(ref)
        if project is not None:
            return Path(project.abspath)
        path = Path(self.topdir) / ref
        if path.exists():
            return path
        return None

    def _manifest_revision(self, module: str) -> str:
        project = self._projects().get(module)
        if project is None:
            self.die(f"unknown West project: {module}")
        revision = project.revision
        repo = Path(project.abspath)
        if not revision or subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=repo,
            check=False,
        ).returncode != 0:
            self.die(
                f"{module}: manifest revision {revision or '<empty>'} "
                f"is not available; run west update {project.name}"
            )
        return revision

    def _base_revision(self, module: str) -> str:
        """The commit a profile's patches are applied ON TOP of, for `module`.

        Without a base-profile this is the frozen manifest revision (original
        behaviour). With `base-profile: <name>`, it is the tip of that base
        profile's `integration/<name>` branch in the module IF that branch
        exists there (i.e. the base profile actually patches this module);
        otherwise it falls back to the manifest revision, because the base
        profile does not touch this module and there is nothing to stack on.
        """
        if not self._base_profile:
            return self._manifest_revision(module)
        repo = self._repo(module)
        base_branch = f"integration/{self._base_profile}"
        if self._branch_exists(repo, base_branch):
            return git(repo, "rev-parse", base_branch, capture=True)
        # Base profile does not patch this module -> stack directly on manifest.
        return self._manifest_revision(module)

    def _require_base_applied(self, modules):
        """Fail early if a stacked profile is asked to apply but its base
        profile's integration branch is missing from every module that base
        would patch. This catches "apply perf before applying homebrew"."""
        if not self._base_profile:
            return
        base_branch = f"integration/{self._base_profile}"
        base_dir = Path(self.manifest.repo_abspath) / "patches" / self._base_profile
        base_yml = base_dir / "patches.yml"
        if not base_yml.is_file():
            self.die(
                f"base-profile {self._base_profile!r} not found at {base_yml}"
            )
        base_modules = {
            p["module"] for p in yaml.safe_load(base_yml.read_text()).get("patches", [])
        }
        missing = [
            m
            for m in base_modules
            if not self._branch_exists(self._repo(m), base_branch)
        ]
        if missing:
            self.die(
                f"base profile {self._base_profile!r} is not applied "
                f"(missing {base_branch} in: {', '.join(sorted(missing))}). "
                f"Run `west patch apply --profile {self._base_profile}` first."
            )

    def _ensure_clean(self, repo: Path, parent: bool = False):
        command = ["status", "--porcelain"]
        if parent:
            command.extend(["--ignore-submodules=all", "--untracked-files=no"])
        if git(repo, *command, capture=True):
            raise RuntimeError(f"worktree is dirty: {repo}")

    def _list(self, patches):
        for patch in patches:
            source = patch.get("source-branch", "-")
            bead = patch.get("bead", "-")
            self.inf(f"{patch['module']}: {source} [{bead}]")
            self.inf(f"  {patch['path']}")
            for test in patch.get("tests", []) or []:
                red = " red" if test.get("red") else ""
                tier = self._coverage_tier(test)
                target = (
                    test.get("ctest-label")
                    or test.get("command")
                    or test.get("target")
                    or test.get("script")
                    or test.get("name", "-")
                )
                self.inf(f"    test:{red} [{tier}] {test.get('name', '-')} -> {target}")
            if patch.get("test-exception"):
                exc = patch["test-exception"]
                reason = exc.get("reason", exc) if isinstance(exc, dict) else exc
                self.inf(f"    test-exception: {reason}")

    def _validate_test_metadata(self, patch) -> list[str]:
        errors: list[str] = []
        tests = patch.get("tests")
        exception = patch.get("test-exception")
        if tests is not None and not isinstance(tests, list):
            errors.append("tests must be a list")
            return errors
        for index, test in enumerate(tests or [], start=1):
            if not isinstance(test, dict):
                errors.append(f"tests[{index}] must be a mapping")
                continue
            if not test.get("name"):
                errors.append(f"tests[{index}] missing name")
            if not (
                test.get("command")
                or test.get("ctest-label")
                or test.get("script")
                or test.get("source-script")
                or test.get("source-file")
                or test.get("target")
                or test.get("guest-command")
                or test.get("guest-argv")
                or test.get("runner") == "cmake-configure-fixture"
                or test.get("runner") == "darling-cmake-target-fixture"
            ):
                errors.append(
                    f"tests[{index}] needs script, source-script, source-file, target, ctest-label, guest command, or command override"
                )
            runner = test.get("runner")
            if runner and runner not in {
                "script",
                "guest-runtime-script",
                "self-contract-script",
                "source-contract-script",
                "source-profile-script",
                "python",
                "cmake-configure-fixture",
                "c-fixture",
                "darling-cmake-target-fixture",
                "guest-c-fixture",
                "guest-command-fixture",
                "guest-argv-fixture",
                "object-symbol-fixture",
                "source-script-fixture",
                "source-build-fixture",
                "west-build",
                "ctest",
            }:
                errors.append(f"tests[{index}] invalid runner {runner!r}")
            if test.get("target") and runner not in {None, "west-build", "darling-cmake-target-fixture"}:
                errors.append(f"tests[{index}] target requires runner: west-build or darling-cmake-target-fixture")
            if test.get("script") and runner not in {
                None,
                "script",
                "guest-runtime-script",
                "self-contract-script",
                "source-contract-script",
                "source-profile-script",
                "python",
                "c-fixture",
                "guest-c-fixture",
                "object-symbol-fixture",
                "source-build-fixture",
            }:
                errors.append(
                    f"tests[{index}] script requires runner: script, guest-runtime-script, self-contract-script, source-contract-script, source-profile-script, python, c-fixture, guest-c-fixture, object-symbol-fixture, or source-build-fixture"
                )
            if test.get("script"):
                repo_ref = test.get("repo", patch["module"])
                repo_path = self._project_path(repo_ref)
                if repo_path is None:
                    errors.append(f"tests[{index}] unknown test repo {repo_ref!r}")
            if test.get("guest-command") and runner != "guest-command-fixture":
                errors.append(f"tests[{index}] guest-command requires runner: guest-command-fixture")
            if test.get("guest-argv") and runner != "guest-argv-fixture":
                errors.append(f"tests[{index}] guest-argv requires runner: guest-argv-fixture")
            if runner == "object-symbol-fixture":
                repo_ref = test.get("repo", patch["module"])
                repo_path = self._project_path(repo_ref)
                if repo_path is None:
                    errors.append(f"tests[{index}] unknown test repo {repo_ref!r}")
            if runner == "c-fixture":
                if not test.get("script"):
                    errors.append(f"tests[{index}] c-fixture requires script")
                for key in ("include-dirs", "stub-headers", "compile-flags", "source-files"):
                    if test.get(key) is not None and not isinstance(test.get(key), list):
                        errors.append(f"tests[{index}] {key} must be a list")
                if test.get("generated-headers") is not None:
                    headers = test.get("generated-headers")
                    if not isinstance(headers, dict) or not all(
                        isinstance(path, str) and isinstance(content, str)
                        for path, content in headers.items()
                    ):
                        errors.append(f"tests[{index}] generated-headers must be a string mapping")
                if test.get("source-root-module") is not None and not isinstance(
                    test.get("source-root-module"), str
                ):
                    errors.append(f"tests[{index}] source-root-module must be a string")
            if runner == "object-symbol-fixture":
                if not test.get("source-file"):
                    errors.append(f"tests[{index}] object-symbol-fixture requires source-file")
                for key in ("include-dirs", "fixture-include-dirs", "compile-flags"):
                    if test.get(key) is not None and not isinstance(test.get(key), list):
                        errors.append(f"tests[{index}] {key} must be a list")
                checks = test.get("symbol-checks")
                if not isinstance(checks, list) or not checks:
                    errors.append(f"tests[{index}] object-symbol-fixture requires symbol-checks")
                elif not all(isinstance(check, dict) for check in checks):
                    errors.append(f"tests[{index}] symbol-checks must be a list of mappings")
                else:
                    for check_index, check in enumerate(checks):
                        for key in (
                            "compile-flags",
                            "present-undefined-symbols",
                            "absent-undefined-symbols",
                            "present-defined-symbols",
                            "absent-defined-symbols",
                        ):
                            if check.get(key) is not None and not isinstance(check.get(key), list):
                                errors.append(
                                    f"tests[{index}].symbol-checks[{check_index}] {key} must be a list"
                                )
            if runner == "guest-c-fixture":
                if not test.get("script"):
                    errors.append(f"tests[{index}] guest-c-fixture requires script")
                if not test.get("ok-marker") and not test.get("host-trace-oracle"):
                    errors.append(f"tests[{index}] guest-c-fixture requires ok-marker")
                if test.get("host-trace-oracle") is not None:
                    if not isinstance(test.get("host-trace-oracle"), bool):
                        errors.append(f"tests[{index}] host-trace-oracle must be boolean")
                    elif test.get("host-trace-oracle") and not test.get("host-trace-files"):
                        errors.append(f"tests[{index}] host-trace-oracle requires host-trace-files")
                for key in ("compile-flags", "link-flags", "run-args"):
                    if test.get(key) is not None and not isinstance(test.get(key), list):
                        errors.append(f"tests[{index}] {key} must be a list")
            if runner in {"guest-command-fixture", "guest-argv-fixture"}:
                if runner == "guest-command-fixture":
                    if not isinstance(test.get("guest-command"), str) or not test.get("guest-command"):
                        errors.append(f"tests[{index}] guest-command-fixture requires guest-command")
                else:
                    guest_argv = test.get("guest-argv")
                    if not isinstance(guest_argv, list) or not guest_argv:
                        errors.append(f"tests[{index}] guest-argv-fixture requires a non-empty guest-argv list")
                    elif not all(isinstance(arg, str) and arg for arg in guest_argv):
                        errors.append(f"tests[{index}] guest-argv must contain non-empty strings")
                    elif not guest_argv[0].startswith("/"):
                        errors.append(f"tests[{index}] guest-argv executable must be an absolute guest path")
                if test.get("expect") is not None:
                    expect = test.get("expect")
                    if not isinstance(expect, dict):
                        errors.append(f"tests[{index}] expect must be a mapping")
                    else:
                        rc_mode = expect.get("returncode", 0)
                        if rc_mode not in {"any", "nonzero", "timeout"} and not isinstance(rc_mode, int):
                            errors.append(
                                f"tests[{index}].expect.returncode must be an integer, any, nonzero, or timeout"
                            )
                        for key in ("output-contains", "output-lacks"):
                            values = expect.get(key, [])
                            if values is not None and (
                                not isinstance(values, list)
                                or not all(isinstance(item, str) and item for item in values)
                            ):
                                errors.append(f"tests[{index}].expect.{key} must be a list of strings")
            if runner == "source-build-fixture":
                if not test.get("script"):
                    errors.append(f"tests[{index}] source-build-fixture requires script")
                for key in ("build-commands", "run-commands"):
                    if test.get(key) is not None and (
                        not isinstance(test.get(key), list)
                        or not all(isinstance(command, str) for command in test.get(key))
                    ):
                        errors.append(f"tests[{index}] {key} must be a list of strings")
                if not test.get("run-commands"):
                    errors.append(f"tests[{index}] source-build-fixture requires run-commands")
            if runner == "source-script-fixture":
                if not test.get("source-script"):
                    errors.append(f"tests[{index}] source-script-fixture requires source-script")
                cases = test.get("cases")
                if not isinstance(cases, list) or not cases:
                    errors.append(f"tests[{index}] source-script-fixture requires cases")
                elif not all(isinstance(case, dict) for case in cases):
                    errors.append(f"tests[{index}] source-script-fixture cases must be mappings")
                else:
                    for case_index, case in enumerate(cases):
                        if case.get("args") is not None and not isinstance(case.get("args"), list):
                            errors.append(f"tests[{index}].cases[{case_index}] args must be a list")
            if runner == "source-contract-script":
                proof = test.get("red-proof") if isinstance(test.get("red-proof"), dict) else {}
                if test.get("red") and proof.get("mode") != "source-base":
                    errors.append(
                        f"tests[{index}] red source-contract-script requires red-proof mode: source-base"
                    )
                if not proof.get("source-env") and not test.get("source-env"):
                    errors.append(
                        f"tests[{index}] source-contract-script requires source-env"
                    )
            if runner == "source-profile-script":
                proof = test.get("red-proof") if isinstance(test.get("red-proof"), dict) else {}
                if not test.get("red") or proof.get("mode") != "source-base":
                    errors.append(
                        f"tests[{index}] source-profile-script requires red: true and red-proof mode: source-base"
                    )
                if not proof.get("source-env") and not test.get("source-env"):
                    errors.append(
                        f"tests[{index}] source-profile-script requires source-env"
                    )
                source_module = proof.get("source-module", patch["module"])
                repo = test.get("repo", source_module)
                if repo != source_module:
                    errors.append(
                        f"tests[{index}] source-profile-script repo must match red-proof source-module"
                    )
            if runner == "self-contract-script":
                proof = test.get("red-proof") if isinstance(test.get("red-proof"), dict) else {}
                if not test.get("red") or proof.get("mode") != "self":
                    errors.append(
                        f"tests[{index}] self-contract-script requires red: true and red-proof mode: self"
                    )
            if runner == "guest-runtime-script":
                runs = test.get("runs") or test.get("env")
                if runs not in {"guest", "darling"}:
                    errors.append(f"tests[{index}] guest-runtime-script requires runs: guest")
            if runner == "cmake-configure-fixture":
                for key in ("configure-args", "marker-files"):
                    if test.get(key) is not None and not isinstance(test.get(key), list):
                        errors.append(f"tests[{index}] {key} must be a list")
                if test.get("fake-tools") is not None and not isinstance(test.get("fake-tools"), dict):
                    errors.append(f"tests[{index}] fake-tools must be a mapping")
                if test.get("expect") is not None and not isinstance(test.get("expect"), dict):
                    errors.append(f"tests[{index}] expect must be a mapping")
            if runner == "darling-cmake-target-fixture":
                if not test.get("target"):
                    errors.append(f"tests[{index}] darling-cmake-target-fixture requires target")
                for key in (
                    "fixture-files",
                    "cmake-args",
                    "build-args",
                    "fallback-executable-sources",
                    "fallback-include-dirs",
                    "fallback-link-libraries",
                    "required-compile-options",
                ):
                    if test.get(key) is not None and not isinstance(test.get(key), list):
                        errors.append(f"tests[{index}] {key} must be a list")
                checks = test.get("required-compile-options") or []
                if not all(isinstance(check, dict) for check in checks):
                    errors.append(f"tests[{index}] required-compile-options must be a list of mappings")
                else:
                    for check_index, check in enumerate(checks):
                        if not check.get("source"):
                            errors.append(
                                f"tests[{index}].required-compile-options[{check_index}] requires source"
                            )
                        if check.get("options") is not None and not isinstance(check.get("options"), list):
                            errors.append(
                                f"tests[{index}].required-compile-options[{check_index}] options must be a list"
                            )
            if test.get("args") is not None and not isinstance(test.get("args"), list):
                errors.append(f"tests[{index}] args must be a list")
            if test.get("env-vars") is not None and not isinstance(test.get("env-vars"), dict):
                errors.append(f"tests[{index}] env-vars must be a mapping")
            if test.get("guest-env-vars") is not None:
                guest_env_vars = test.get("guest-env-vars")
                if runner not in {"guest-c-fixture", "guest-command-fixture"}:
                    errors.append(f"tests[{index}] guest-env-vars requires runner: guest-c-fixture or guest-command-fixture")
                elif not isinstance(guest_env_vars, dict) or not all(
                    isinstance(k, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                    and isinstance(v, (str, int, float, bool))
                    for k, v in guest_env_vars.items()
                ):
                    errors.append(f"tests[{index}] guest-env-vars must be a mapping of shell variable names to scalar values")
            if test.get("dcc-cache") is not None:
                dcc_cache = test.get("dcc-cache")
                if runner not in {"guest-c-fixture", "guest-command-fixture"}:
                    errors.append(f"tests[{index}] dcc-cache requires runner: guest-c-fixture or guest-command-fixture")
                elif not isinstance(dcc_cache, dict):
                    errors.append(f"tests[{index}] dcc-cache must be a mapping")
                else:
                    for key in ("source-module", "source-ref", "tools-dir", "builder", "closure-list", "env", "enable-env", "install-root"):
                        if dcc_cache.get(key) is not None and (
                            not isinstance(dcc_cache.get(key), str) or not dcc_cache.get(key)
                        ):
                            errors.append(f"tests[{index}].dcc-cache.{key} must be a non-empty string")
                    install_root = dcc_cache.get("install-root")
                    if install_root is not None and install_root not in {"guest-visible", "base", "prefix"}:
                        errors.append(
                            f"tests[{index}].dcc-cache.install-root must be guest-visible, base, or prefix"
                        )
                    for key in ("env", "enable-env"):
                        value = dcc_cache.get(key)
                        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                            errors.append(f"tests[{index}].dcc-cache.{key} must be a shell variable name")
                    for key in ("soft", "stale"):
                        if dcc_cache.get(key) is not None and not isinstance(dcc_cache.get(key), bool):
                            errors.append(f"tests[{index}].dcc-cache.{key} must be boolean")
            if test.get("timeout-seconds") is not None:
                timeout = test.get("timeout-seconds")
                if not isinstance(timeout, int) or timeout <= 0:
                    errors.append(f"tests[{index}] timeout-seconds must be a positive integer")
            runtime_profile = test.get("runtime-profile")
            if runtime_profile is not None:
                if not isinstance(runtime_profile, str) or not runtime_profile:
                    errors.append(f"tests[{index}] runtime-profile must be a non-empty string")
                elif test.get("env") != "darling":
                    errors.append(f"tests[{index}] runtime-profile requires env: darling")
            if test.get("blocked") is not None and not isinstance(test.get("blocked"), bool):
                errors.append(f"tests[{index}] blocked must be boolean")
            if test.get("requires-env") is not None:
                required = test.get("requires-env")
                if not isinstance(required, list) or not all(
                    isinstance(name, str) and name for name in required
                ):
                    errors.append(f"tests[{index}] requires-env must be a list of names")
            if test.get("requires") is not None:
                required = test.get("requires")
                if not isinstance(required, list) or not all(
                    isinstance(name, str) and name for name in required
                ):
                    errors.append(f"tests[{index}] requires must be a list of names")
                elif any(name not in {"darling-prefix", "darling-eunion-prefix"} for name in required):
                    errors.append(f"tests[{index}] has unsupported requires resource")
            required_resources = test.get("requires") if isinstance(test.get("requires"), list) else []
            if test.get("host-trace-files") is not None:
                traces = test.get("host-trace-files")
                if runner not in {"guest-c-fixture", "guest-argv-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] host-trace-files requires runner: guest-c-fixture, guest-argv-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(traces, list) or not traces:
                    errors.append(f"tests[{index}] host-trace-files must be a non-empty list")
                elif not all(isinstance(trace, dict) for trace in traces):
                    errors.append(f"tests[{index}] host-trace-files entries must be mappings")
                else:
                    for trace_index, trace in enumerate(traces):
                        env_name = trace.get("env")
                        rel_path = trace.get("prefix-relative-path")
                        contains = trace.get("contains", [])
                        if not isinstance(env_name, str) or not env_name:
                            errors.append(
                                f"tests[{index}].host-trace-files[{trace_index}] needs env"
                            )
                        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                            errors.append(
                                f"tests[{index}].host-trace-files[{trace_index}] env must be a shell variable name"
                            )
                        if not isinstance(rel_path, str) or not rel_path:
                            errors.append(
                                f"tests[{index}].host-trace-files[{trace_index}] needs prefix-relative-path"
                            )
                        elif rel_path.startswith("/") or ".." in Path(rel_path).parts:
                            errors.append(
                                f"tests[{index}].host-trace-files[{trace_index}] path must be prefix-relative"
                            )
                        if not isinstance(contains, list) or not all(
                            isinstance(item, str) and item for item in contains
                        ):
                            errors.append(
                                f"tests[{index}].host-trace-files[{trace_index}] contains must be a list of strings"
                            )
            if test.get("host-temp-files") is not None:
                temps = test.get("host-temp-files")
                if runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] host-temp-files requires runner: guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(temps, list) or not temps:
                    errors.append(f"tests[{index}] host-temp-files must be a non-empty list")
                elif not all(isinstance(temp_file, dict) for temp_file in temps):
                    errors.append(f"tests[{index}] host-temp-files entries must be mappings")
                else:
                    for temp_index, temp_file in enumerate(temps):
                        env_name = temp_file.get("env")
                        rel_path = temp_file.get("prefix-relative-path")
                        contents = temp_file.get("contents", "")
                        guest_path = temp_file.get("guest-path", False)
                        if not isinstance(env_name, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name):
                            errors.append(
                                f"tests[{index}].host-temp-files[{temp_index}] env must be a shell variable name"
                            )
                        if not isinstance(rel_path, str) or not rel_path:
                            errors.append(
                                f"tests[{index}].host-temp-files[{temp_index}] needs prefix-relative-path"
                            )
                        elif rel_path.startswith("/") or ".." in Path(rel_path).parts:
                            errors.append(
                                f"tests[{index}].host-temp-files[{temp_index}] path must be prefix-relative"
                            )
                        if contents is not None and not isinstance(contents, str):
                            errors.append(
                                f"tests[{index}].host-temp-files[{temp_index}] contents must be a string"
                            )
                        if not isinstance(guest_path, bool):
                            errors.append(
                                f"tests[{index}].host-temp-files[{temp_index}] guest-path must be a boolean"
                            )
            if test.get("host-stat-deltas") is not None:
                deltas = test.get("host-stat-deltas")
                if runner != "guest-c-fixture":
                    errors.append(f"tests[{index}] host-stat-deltas requires runner: guest-c-fixture")
                elif not isinstance(deltas, list) or not deltas:
                    errors.append(f"tests[{index}] host-stat-deltas must be a non-empty list")
                elif not all(isinstance(delta, dict) for delta in deltas):
                    errors.append(f"tests[{index}] host-stat-deltas entries must be mappings")
                else:
                    for delta_index, delta in enumerate(deltas):
                        path = delta.get("path")
                        minimum = delta.get("min-delta", 1)
                        if not isinstance(path, str) or not path:
                            errors.append(
                                f"tests[{index}].host-stat-deltas[{delta_index}] needs path"
                            )
                        elif not all(
                            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
                            for part in path.split(".")
                        ):
                            errors.append(
                                f"tests[{index}].host-stat-deltas[{delta_index}] path must be a dotted JSON field path"
                            )
                        if not isinstance(minimum, int) or minimum <= 0:
                            errors.append(
                                f"tests[{index}].host-stat-deltas[{delta_index}] min-delta must be a positive integer"
                            )
            if test.get("eunion-template-files") is not None:
                files = test.get("eunion-template-files")
                if "darling-eunion-prefix" not in required_resources:
                    errors.append(f"tests[{index}] eunion-template-files requires darling-eunion-prefix")
                elif runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] eunion-template-files requires runner: guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(files, list) or not files:
                    errors.append(f"tests[{index}] eunion-template-files must be a non-empty list")
                elif not all(isinstance(item, dict) for item in files):
                    errors.append(f"tests[{index}] eunion-template-files entries must be mappings")
                else:
                    for file_index, item in enumerate(files):
                        guest_path = item.get("guest-path")
                        contents = item.get("contents", "")
                        if (
                            not isinstance(guest_path, str)
                            or not guest_path.startswith("/")
                            or ".." in Path(guest_path).parts
                        ):
                            errors.append(
                                f"tests[{index}].eunion-template-files[{file_index}] guest-path must be absolute without '..'"
                            )
                        if contents is not None and not isinstance(contents, str):
                            errors.append(
                                f"tests[{index}].eunion-template-files[{file_index}] contents must be a string"
                            )
            if test.get("eunion-upper-files") is not None:
                files = test.get("eunion-upper-files")
                if "darling-eunion-prefix" not in required_resources:
                    errors.append(f"tests[{index}] eunion-upper-files requires darling-eunion-prefix")
                elif runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] eunion-upper-files requires runner: guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(files, list) or not files:
                    errors.append(f"tests[{index}] eunion-upper-files must be a non-empty list")
                elif not all(isinstance(item, dict) for item in files):
                    errors.append(f"tests[{index}] eunion-upper-files entries must be mappings")
                else:
                    for file_index, item in enumerate(files):
                        guest_path = item.get("guest-path")
                        contents = item.get("contents", "")
                        if (
                            not isinstance(guest_path, str)
                            or not guest_path.startswith("/")
                            or ".." in Path(guest_path).parts
                        ):
                            errors.append(
                                f"tests[{index}].eunion-upper-files[{file_index}] guest-path must be absolute without '..'"
                            )
                        if contents is not None and not isinstance(contents, str):
                            errors.append(
                                f"tests[{index}].eunion-upper-files[{file_index}] contents must be a string"
                            )
            if test.get("eunion-template-symlinks") is not None:
                symlinks = test.get("eunion-template-symlinks")
                if "darling-eunion-prefix" not in required_resources:
                    errors.append(
                        f"tests[{index}] eunion-template-symlinks requires darling-eunion-prefix"
                    )
                elif runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] eunion-template-symlinks requires runner: "
                        "guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(symlinks, list) or not symlinks:
                    errors.append(
                        f"tests[{index}] eunion-template-symlinks must be a non-empty list"
                    )
                elif not all(isinstance(item, dict) for item in symlinks):
                    errors.append(
                        f"tests[{index}] eunion-template-symlinks entries must be mappings"
                    )
                else:
                    for symlink_index, item in enumerate(symlinks):
                        guest_path = item.get("guest-path")
                        target = item.get("target")
                        allow_parent_target = item.get("allow-parent-target", False)
                        if (
                            not isinstance(guest_path, str)
                            or not guest_path.startswith("/")
                            or ".." in Path(guest_path).parts
                        ):
                            errors.append(
                                f"tests[{index}].eunion-template-symlinks[{symlink_index}] "
                                "guest-path must be absolute without '..'"
                            )
                        if not isinstance(target, str) or not target or target.startswith("/"):
                            errors.append(
                                f"tests[{index}].eunion-template-symlinks[{symlink_index}] "
                                "target must be a non-empty relative path"
                            )
                        elif not allow_parent_target and ".." in Path(target).parts:
                            errors.append(
                                f"tests[{index}].eunion-template-symlinks[{symlink_index}] "
                                "target contains '..' without allow-parent-target"
                            )
                        if not isinstance(allow_parent_target, bool):
                            errors.append(
                                f"tests[{index}].eunion-template-symlinks[{symlink_index}] "
                                "allow-parent-target must be boolean"
                            )
            if test.get("eunion-forbid-template-paths") is not None:
                paths = test.get("eunion-forbid-template-paths")
                if "darling-eunion-prefix" not in required_resources:
                    errors.append(
                        f"tests[{index}] eunion-forbid-template-paths requires darling-eunion-prefix"
                    )
                elif runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] eunion-forbid-template-paths requires runner: "
                        "guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(paths, list) or not paths:
                    errors.append(
                        f"tests[{index}] eunion-forbid-template-paths must be a non-empty list"
                    )
                else:
                    for path_index, guest_path in enumerate(paths):
                        if (
                            not isinstance(guest_path, str)
                            or not guest_path.startswith("/")
                            or ".." in Path(guest_path).parts
                        ):
                            errors.append(
                                f"tests[{index}].eunion-forbid-template-paths[{path_index}] "
                                "must be absolute without '..'"
                            )
            if test.get("eunion-require-upper-paths") is not None:
                paths = test.get("eunion-require-upper-paths")
                if "darling-eunion-prefix" not in required_resources:
                    errors.append(
                        f"tests[{index}] eunion-require-upper-paths requires darling-eunion-prefix"
                    )
                elif runner not in {"guest-c-fixture", "guest-runtime-script", "script"}:
                    errors.append(
                        f"tests[{index}] eunion-require-upper-paths requires runner: "
                        "guest-c-fixture, guest-runtime-script, or script"
                    )
                elif not isinstance(paths, list) or not paths:
                    errors.append(
                        f"tests[{index}] eunion-require-upper-paths must be a non-empty list"
                    )
                else:
                    for path_index, guest_path in enumerate(paths):
                        if (
                            not isinstance(guest_path, str)
                            or not guest_path.startswith("/")
                            or ".." in Path(guest_path).parts
                        ):
                            errors.append(
                                f"tests[{index}].eunion-require-upper-paths[{path_index}] "
                                "must be absolute without '..'"
                            )
            if test.get("requires-profile") is not None and not isinstance(
                test.get("requires-profile"), str
            ):
                errors.append(f"tests[{index}] requires-profile must be a string")
            if test.get("red") and test.get("red-proof") is None:
                errors.append(f"tests[{index}] red test needs red-proof")
            proof = test.get("red-proof")
            if isinstance(proof, dict) and proof.get("source-revision") is not None:
                source_revision = proof.get("source-revision")
                source_revision_is_guest_baseline = (
                    proof.get("mode") == "guest-runtime-deploy"
                    and proof.get("bad-profile") == "current-minus-patch"
                )
                if proof.get("mode") != "source-base" and not source_revision_is_guest_baseline:
                    errors.append(
                        f"tests[{index}] red-proof source-revision requires mode: source-base "
                        "or guest-runtime-deploy with bad-profile: current-minus-patch"
                    )
                elif not isinstance(source_revision, str) or not source_revision.strip():
                    errors.append(
                        f"tests[{index}] red-proof source-revision must be a non-empty revision"
                    )
            if test.get("red-proof") is not None:
                if not isinstance(proof, dict):
                    errors.append(f"tests[{index}] red-proof must be a mapping")
                elif proof.get("mode") not in {"self", "source-base", "guest-runtime-deploy"}:
                    errors.append(
                        f"tests[{index}] red-proof mode must be self, source-base, or guest-runtime-deploy"
                    )
                elif proof.get("mode") == "source-base" and not proof.get("source-env"):
                    errors.append(
                        f"tests[{index}] red-proof source-base needs source-env"
                    )
                elif runner == "guest-c-fixture" and proof.get("mode") == "source-base":
                    errors.append(
                        f"tests[{index}] guest-c-fixture cannot use source-base red-proof "
                        "without an isolated bad/fixed Darling deploy runner"
                    )
                elif proof.get("mode") == "self" and not proof.get("why-self"):
                    errors.append(
                        f"tests[{index}] red-proof self needs why-self"
                    )
                elif proof.get("mode") == "guest-runtime-deploy":
                    for key in ("expect-output-contains", "expect-output-lacks"):
                        expected_output = proof.get(key)
                        if expected_output is not None:
                            if isinstance(expected_output, str):
                                pass
                            elif not isinstance(expected_output, list) or not all(
                                isinstance(item, str) and item for item in expected_output
                            ):
                                errors.append(
                                    f"tests[{index}] red-proof {key} must be a string or list of strings"
                                )
                    if runner not in {
                        "guest-c-fixture",
                        "guest-command-fixture",
                        "guest-argv-fixture",
                        "guest-runtime-script",
                        "script",
                    }:
                            errors.append(
                                f"tests[{index}] red-proof guest-runtime-deploy requires runner: guest-c-fixture, guest-command-fixture, guest-argv-fixture, guest-runtime-script, or script"
                            )
                    elif runner == "script" and not (
                        {"darling-prefix", "darling-eunion-prefix"} & set(required_resources)
                    ):
                            errors.append(
                                f"tests[{index}] red-proof guest-runtime-deploy script runner requires darling-prefix"
                            )
                    bad_profile = proof.get("bad-profile")
                    if bad_profile is not None and bad_profile != "current-minus-patch":
                        errors.append(
                            f"tests[{index}] red-proof guest-runtime-deploy bad-profile must be current-minus-patch"
                        )
                    skip_patches = proof.get("current-minus-skip-patches")
                    if skip_patches is not None:
                        if bad_profile != "current-minus-patch":
                            errors.append(
                                f"tests[{index}] current-minus-skip-patches requires bad-profile: current-minus-patch"
                            )
                        elif not isinstance(skip_patches, list) or not all(
                            isinstance(path, str) and path for path in skip_patches
                        ):
                            errors.append(
                                f"tests[{index}] current-minus-skip-patches must be a list of patch paths"
                            )
                    source_patches = proof.get("source-patches")
                    if source_patches is not None:
                        if not isinstance(source_patches, list) or not all(
                            isinstance(path, str)
                            and path
                            and not Path(path).is_absolute()
                            and ".." not in Path(path).parts
                            for path in source_patches
                        ):
                            errors.append(
                                f"tests[{index}] source-patches must be a list of workspace-relative patch paths"
                            )
                    cmake_defines = proof.get("cmake-defines")
                    if cmake_defines is not None:
                        if not isinstance(cmake_defines, dict):
                            errors.append(
                                f"tests[{index}] red-proof cmake-defines must be a mapping"
                            )
                        else:
                            for key, value in cmake_defines.items():
                                if not isinstance(key, str) or not key:
                                    errors.append(
                                        f"tests[{index}] red-proof cmake-defines keys must be non-empty strings"
                                    )
                                    continue
                                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                                    errors.append(
                                        f"tests[{index}] red-proof cmake-defines key {key!r} is not a CMake variable name"
                                    )
                                if value is not None and not isinstance(value, (str, int, float, bool)):
                                    errors.append(
                                        f"tests[{index}] red-proof cmake-defines values must be strings, numbers, booleans, or null"
                                    )
                    red_runner = proof.get("red-runner")
                    if red_runner is not None:
                        if not isinstance(red_runner, dict):
                            errors.append(f"tests[{index}] red-proof red-runner must be a mapping")
                        else:
                            red_runner_kind = red_runner.get(
                                "runner",
                                "script" if red_runner.get("script") else None,
                            )
                            if red_runner.get("red-proof") is not None:
                                errors.append(
                                    f"tests[{index}] red-proof red-runner must not define red-proof"
                                )
                            if red_runner_kind not in {
                                "script",
                                "python",
                                "c-fixture",
                                "guest-command-fixture",
                            }:
                                errors.append(
                                    f"tests[{index}] red-proof red-runner uses unsupported runner {red_runner_kind!r}"
                                )
                            if red_runner_kind in {"script", "python", "c-fixture"} and not red_runner.get("script"):
                                errors.append(
                                    f"tests[{index}] red-proof red-runner requires script"
                                )
                            if red_runner.get("host-temp-files") is not None:
                                temps = red_runner.get("host-temp-files")
                                if red_runner_kind != "script":
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-temp-files requires runner: script"
                                    )
                                elif not isinstance(temps, list) or not temps:
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-temp-files must be a non-empty list"
                                    )
                                elif not all(isinstance(temp_file, dict) for temp_file in temps):
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-temp-files entries must be mappings"
                                    )
                                else:
                                    for temp_index, temp_file in enumerate(temps):
                                        env_name = temp_file.get("env")
                                        rel_path = temp_file.get("prefix-relative-path")
                                        contents = temp_file.get("contents", "")
                                        guest_path = temp_file.get("guest-path", False)
                                        if not isinstance(env_name, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name):
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-temp-files[{temp_index}] env must be a shell variable name"
                                            )
                                        if not isinstance(rel_path, str) or not rel_path:
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-temp-files[{temp_index}] needs prefix-relative-path"
                                            )
                                        elif rel_path.startswith("/") or ".." in Path(rel_path).parts:
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-temp-files[{temp_index}] path must be prefix-relative"
                                            )
                                        if contents is not None and not isinstance(contents, str):
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-temp-files[{temp_index}] contents must be a string"
                                            )
                                        if not isinstance(guest_path, bool):
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-temp-files[{temp_index}] guest-path must be a boolean"
                                            )
                            if red_runner.get("host-trace-files") is not None:
                                traces = red_runner.get("host-trace-files")
                                if red_runner_kind != "script":
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-trace-files requires runner: script"
                                    )
                                elif not isinstance(traces, list) or not traces:
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-trace-files must be a non-empty list"
                                    )
                                elif not all(isinstance(trace, dict) for trace in traces):
                                    errors.append(
                                        f"tests[{index}] red-proof red-runner host-trace-files entries must be mappings"
                                    )
                                else:
                                    for trace_index, trace in enumerate(traces):
                                        env_name = trace.get("env")
                                        rel_path = trace.get("prefix-relative-path")
                                        contains = trace.get("contains", [])
                                        if not isinstance(env_name, str) or not env_name:
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-trace-files[{trace_index}] needs env"
                                            )
                                        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-trace-files[{trace_index}] env must be a shell variable name"
                                            )
                                        if not isinstance(rel_path, str) or not rel_path:
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-trace-files[{trace_index}] needs prefix-relative-path"
                                            )
                                        elif rel_path.startswith("/") or ".." in Path(rel_path).parts:
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-trace-files[{trace_index}] path must be prefix-relative"
                                            )
                                        if not isinstance(contains, list) or not all(
                                            isinstance(item, str) and item for item in contains
                                        ):
                                            errors.append(
                                                f"tests[{index}].red-proof.red-runner.host-trace-files[{trace_index}] contains must be a list of strings"
                                            )
                    artifacts = proof.get("runtime-artifacts")
                    if not isinstance(artifacts, list) or not artifacts:
                        errors.append(
                            f"tests[{index}] red-proof guest-runtime-deploy needs runtime-artifacts"
                        )
                    elif not all(isinstance(artifact, dict) for artifact in artifacts):
                        errors.append(
                            f"tests[{index}] red-proof guest-runtime-deploy runtime-artifacts must be mappings"
                        )
                    else:
                        for artifact_index, artifact in enumerate(artifacts):
                            if not artifact.get("module") or not isinstance(artifact.get("module"), str):
                                errors.append(
                                    f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] needs module"
                                )
                            build_targets = artifact.get("build-targets")
                            if not isinstance(build_targets, list) or not build_targets or not all(
                                isinstance(target, str) and target for target in build_targets
                            ):
                                errors.append(
                                    f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] needs build-targets"
                                )
                            resource = artifact.get("resource")
                            if resource is None:
                                deploy = artifact.get("deploy")
                                if not isinstance(deploy, list) or not deploy or not all(
                                    isinstance(path, str) and path for path in deploy
                                ):
                                    errors.append(
                                        f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] needs deploy"
                                    )
                            elif resource != ROOTLESS_BOOTSTRAP_RESOURCE:
                                errors.append(
                                    f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] "
                                    f"has unknown resource {resource!r}"
                                )
                            else:
                                if artifact.get("module") != "darling":
                                    errors.append(
                                        f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] "
                                        "rootless-bootstrap must belong to darling"
                                    )
                                if build_targets != [ROOTLESS_BOOTSTRAP_TARGET]:
                                    errors.append(
                                        f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] "
                                        "rootless-bootstrap must build only rootless_bootstrap"
                                    )
                                if "deploy" in artifact:
                                    errors.append(
                                        f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] "
                                        "rootless-bootstrap must not declare deploy paths"
                                    )
                if isinstance(proof, dict) and proof.get("expect-failure-phase") is not None:
                    phases = proof.get("expect-failure-phase")
                    if isinstance(phases, str):
                        phases = [phases]
                    allowed_phases = {
                        "setup",
                        "compile",
                        "run",
                        "inspect",
                        "script",
                        "build",
                        "configure",
                        "ctest",
                        "runtime",
                        "self",
                    }
                    if not isinstance(phases, list) or not phases or not all(
                        isinstance(phase, str) and phase in allowed_phases
                        for phase in phases
                    ):
                        errors.append(
                            f"tests[{index}] red-proof expect-failure-phase must be one or more known phases"
                        )
                    elif proof.get("mode") == "guest-runtime-deploy" and "runtime" in phases:
                        errors.append(
                            f"tests[{index}] guest-runtime-deploy must name an exact failure phase, not 'runtime'"
                        )
            env = test.get("env")
            if env and env not in {"host", "darling", "macos"}:
                errors.append(f"tests[{index}] invalid env {env!r}")
            diag = test.get("diag")
            if diag and diag not in {"bare", "guarded", "forensic"}:
                errors.append(f"tests[{index}] invalid diag {diag!r}")
            kind = test.get("kind")
            if kind and kind not in {
                "unit",
                "contract",
                "source-contract",
                "guest",
                "package",
                "fuzz",
                "stress",
                "build",
                "gate",
            }:
                errors.append(f"tests[{index}] invalid kind {kind!r}")
            tier = test.get("coverage-tier")
            if tier and tier not in {"runtime", "compile", "host", "model", "source"}:
                errors.append(f"tests[{index}] invalid coverage-tier {tier!r}")
            if kind == "source-contract" and tier and tier != "source":
                errors.append(
                    f"tests[{index}] source-contract must use coverage-tier: source"
                )
            if tier == "source" and kind != "source-contract":
                errors.append(
                    f"tests[{index}] coverage-tier: source requires kind: source-contract"
                )
        if exception is not None:
            if not isinstance(exception, dict):
                errors.append("test-exception must be a mapping")
            elif not exception.get("reason"):
                errors.append("test-exception needs reason")
            elif not exception.get("scope"):
                errors.append("test-exception needs scope")
        return errors

    @staticmethod
    def _coverage_tier(test) -> str:
        """Return the coverage class materialized by manifest normalization."""
        explicit = test.get("coverage-tier")
        if explicit:
            return explicit
        # Keep a conservative result for direct unit callers that bypass the
        # public manifest loader; production metadata is always normalized.
        return "source"

    @staticmethod
    def _is_behavioral_test(test) -> bool:
        """Whether a test is strong enough to close patch coverage.

        Source contracts are useful drift guards, but they only prove that text
        or symbols are present. They do not prove the fixed behavior in a host,
        guest, build, package, fuzz, or stress scenario, so they are deliberately
        excluded from the coverage count.
        """
        return DarlingPatch._coverage_tier(test) != "source"

    @staticmethod
    def _quality_warnings(patch) -> list[str]:
        warnings = []
        for index, test in enumerate(patch.get("tests") or [], start=1):
            proof = test.get("red-proof")
            if not isinstance(proof, dict):
                continue
            if proof.get("mode") != "guest-runtime-deploy":
                continue

            artifacts = proof.get("runtime-artifacts") or []
            source_modules = set(proof.get("source-modules") or [])
            builds_system_kernel = any(
                isinstance(artifact, dict)
                and artifact.get("module") == "darling/src/external/xnu"
                and "system_kernel" in (artifact.get("build-targets") or [])
                for artifact in artifacts
            )
            if (
                builds_system_kernel
                and "darling/src/external/darlingserver" not in source_modules
            ):
                warnings.append(
                    f"tests[{index}] guest-runtime-deploy builds system_kernel; "
                    "add red-proof.source-modules: [darling/src/external/darlingserver] "
                    "so RPC-generated headers come from the same materialized profile"
                )

            test_name = str(test.get("name", "")).lower()
            patch_path = str(patch.get("path", "")).lower()
            dyld_is_subject = "dyld" in test_name or "dyld" in patch_path
            if not dyld_is_subject:
                for artifact_index, artifact in enumerate(artifacts):
                    if not isinstance(artifact, dict):
                        continue
                    if artifact.get("module") != "darling/src/external/dyld":
                        continue
                    warnings.append(
                        f"tests[{index}].red-proof.runtime-artifacts[{artifact_index}] "
                        "deploys dyld although the patch/test is not dyld-scoped; "
                        "remove the artifact or split the proof so unrelated dyld build "
                        "failures cannot satisfy RED"
                    )
        return warnings

    def _check(
        self,
        profile_dir: Path,
        patches,
        strict: bool,
        *,
        quality: bool = False,
        strict_quality: bool = False,
    ):
        missing = []
        invalid = []
        quality_warnings = []
        covered_by_tier = {
            "runtime": 0,
            "compile": 0,
            "host": 0,
            "model": 0,
        }
        excepted = 0
        for patch in patches:
            errors = self._validate_test_metadata(patch)
            if errors:
                invalid.append((patch, errors))
                continue
            if quality:
                warnings = self._quality_warnings(patch)
                patch_path = profile_dir / patch["path"]
                if patch_path.is_file():
                    patch_content = patch_path.read_bytes()
                    artifacts = generated_patch_artifacts(patch_content)
                    if artifacts:
                        warnings.append(
                            "patch contains "
                            + describe_generated_patch_artifacts(artifacts)
                        )
                    trailers = legacy_automation_trailers(patch_content)
                    if trailers:
                        warnings.append(
                            "patch contains "
                            + describe_legacy_automation_trailers(trailers)
                        )
                if warnings:
                    quality_warnings.append((patch, warnings))
            tests = [
                test
                for test in (patch.get("tests") or [])
                if not test.get("blocked")
            ]
            behavioral = [test for test in tests if self._is_behavioral_test(test)]
            exception = patch.get("test-exception")
            if behavioral:
                tiers = [self._coverage_tier(test) for test in behavioral]
                strongest = min(
                    tiers,
                    key=lambda tier: {
                        "runtime": 0,
                        "compile": 1,
                        "host": 2,
                        "model": 3,
                    }.get(tier, 99),
                )
                covered_by_tier[strongest] += 1
                suffix = ""
                if len(behavioral) != len(tests):
                    suffix = f", {len(tests) - len(behavioral)} source-contract(s)"
                tier_summary = ", ".join(
                    f"{tier}:{tiers.count(tier)}"
                    for tier in ("runtime", "compile", "host", "model")
                    if tier in tiers
                )
                self.inf(
                    f"{strongest.upper():<9} {patch['path']} "
                    f"({len(behavioral)} behavioral test(s); {tier_summary}{suffix})"
                )
            elif tests:
                if exception:
                    excepted += 1
                    reason = exception.get("reason", "-")
                    self.inf(
                        f"EXCEPTION {patch['path']} ({reason}; "
                        f"{len(tests)} source-contract(s))"
                    )
                else:
                    missing.append(patch)
                    self.inf(
                        f"SOURCE    {patch['path']} ({len(tests)} source-contract(s); "
                        f"missing behavioral test)  [{patch.get('bead', '-')}]"
                    )
            elif exception:
                excepted += 1
                reason = exception.get("reason", "-")
                self.inf(f"EXCEPTION {patch['path']} ({reason})")
            else:
                missing.append(patch)
                self.inf(f"MISSING   {patch['path']}  [{patch.get('bead', '-')}]")
        for patch, errors in invalid:
            for error in errors:
                self.err(f"INVALID   {patch['path']}: {error}")
        for patch, warnings in quality_warnings:
            for warning in warnings:
                self.inf(f"QUALITY   {patch['path']}: {warning}")
        self.inf(
            "test metadata: "
            f"{sum(covered_by_tier.values())} covered "
            f"(runtime {covered_by_tier['runtime']}, "
            f"compile {covered_by_tier['compile']}, "
            f"host {covered_by_tier['host']}, "
            f"model {covered_by_tier['model']}), "
            f"{excepted} exceptions, "
            f"{len(missing)} missing, {len(invalid)} invalid "
            f"(of {len(patches)})"
        )
        if missing:
            self.inf(
                "hint: add behavioral tests: [{name, runner, script|target|ctest-label, env, diag, kind, red}] "
                "or test-exception: {reason, note}"
            )
        if strict and (missing or invalid):
            self.die(
                f"{len(missing)} missing + {len(invalid)} invalid patch test metadata entries"
            )
        if strict_quality and quality_warnings:
            total = sum(len(warnings) for _, warnings in quality_warnings)
            self.die(f"{total} patch test quality warning(s)")

    def _prepare(
        self, module: str, repo: Path, branch: str, parent: bool = False
    ):
        self._ensure_clean(repo, parent=parent)
        # Start from the base revision: manifest-rev normally, or the base
        # profile's integration tip when this profile stacks (base-profile).
        revision = self._base_revision(module)
        git(repo, "switch", "--detach", revision)
        git(repo, "branch", "-f", branch, revision)
        git(repo, "switch", branch)

    def _ensure_generated_context(self, module: str, profile: str):
        repo = self._repo(module)
        branch = f"integration/{profile}"
        current = git(repo, "branch", "--show-current", capture=True)
        # The acceptable detached base is the base revision (manifest-rev, or
        # the base profile's integration tip when stacking).
        base_revision = self._base_revision(module)
        base_commit = git(repo, "rev-parse", base_revision, capture=True)
        head = git(repo, "rev-parse", "HEAD", capture=True)
        # Accept being: on our own integration branch; detached at the base
        # commit; or (when stacking) still on the base profile's integration
        # branch -- which is exactly where the module legitimately sits right
        # before a stacked profile is applied for the first time.
        base_branch = (
            f"integration/{self._base_profile}" if self._base_profile else None
        )
        allowed = (
            current == branch
            or (current == base_branch and base_branch is not None)
            or (not current and head == base_commit)
        )
        if not allowed:
            expected = f"{branch} or detached {base_revision}"
            if base_branch:
                expected = f"{branch}, {base_branch}, or detached {base_revision}"
            raise RuntimeError(
                f"{module}: expected {expected}, found {current or head}"
            )
        self._ensure_clean(repo, parent=module == "darling")

    def _verify_patch(self, profile_dir: Path, patch):
        path = profile_dir / patch["path"]
        content = path.read_bytes()
        trailers = legacy_automation_trailers(content)
        if trailers:
            raise RuntimeError(
                f"{path}: patch contains {describe_legacy_automation_trailers(trailers)}"
            )
        artifacts = generated_patch_artifacts(content)
        if artifacts:
            raise RuntimeError(
                f"{path}: patch contains {describe_generated_patch_artifacts(artifacts)}"
            )
        actual = hashlib.sha256(content).hexdigest()
        expected = patch["sha256sum"]
        if actual != expected:
            raise RuntimeError(
                f"checksum mismatch for {path}: {actual} != {expected}"
            )
        return path

    def _verify(
        self,
        profile_dir: Path,
        patches,
        *,
        require_source_branches: bool = True,
    ):
        manifest_repo = Path(self.manifest.repo_abspath)
        bead_ids = {
            json.loads(line)["id"]
            for line in (
                manifest_repo / ".beads" / "issues.jsonl"
            ).read_text().splitlines()
            if line.strip()
        }
        grouped = self._group(patches)

        for patch in patches:
            source_commit = patch.get("source-commit", "")
            if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
                self.die(f"{patch['path']}: source-commit must be a full SHA")

            publication_status = patch.get("publication-status")
            if publication_status not in {"ready", "provisional", "blocked"}:
                self.die(
                    f"{patch['path']}: invalid publication-status "
                    f"{publication_status!r}"
                )
            if (
                publication_status != "ready"
                and not patch.get("publication-blocker")
            ):
                self.die(
                    f"{patch['path']}: {publication_status} fixes require "
                    "publication-blocker"
                )

            repo = self._repo(patch["module"])
            if require_source_branches:
                source_branch = patch["source-branch"]
                if not self._branch_exists(repo, source_branch):
                    self.die(f"{patch['module']}: missing source branch {source_branch}")
                branch_head = git(repo, "rev-parse", source_branch, capture=True)
                if branch_head != source_commit:
                    self.die(
                        f"{patch['module']}: {source_branch} drifted "
                        f"({branch_head} != {source_commit})"
                    )

            path = self._verify_patch(profile_dir, patch)
            if require_source_branches:
                exported = subprocess.run(
                    format_patch_command(patch, source_commit),
                    cwd=repo,
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
                if hashlib.sha256(exported).hexdigest() != patch["sha256sum"]:
                    self.die(f"{patch['path']}: patch export drifted")

            bead = patch.get("bead")
            if bead and bead not in bead_ids:
                self.die(f"{patch['path']}: unknown Bead {bead}")
            pr_draft = patch.get("pr-draft")
            if pr_draft and not (manifest_repo / pr_draft).is_file():
                self.die(f"{patch['path']}: missing PR draft {pr_draft}")
            self.inf(f"verified {path.relative_to(manifest_repo)}")

        self._verify_applicability(profile_dir, grouped)
        self.inf(f"verified {len(patches)} patches")

    def _export(
        self,
        profile_path: Path,
        profile_dir: Path,
        profile,
        patches,
        patch_selector: str | None,
        check: bool,
        allow_large_output: bool,
    ):
        selected_paths = None
        if patch_selector:
            selected_paths = {patch_selector}
            if not any(patch["path"] == patch_selector for patch in patches):
                self.die(f"{patch_selector}: patch not found in profile")

        plans = self._plan_export(profile_dir, patches, selected_paths, allow_large_output)
        changed = False
        metadata_updates: dict[str, dict[str, str]] = {}
        for plan in plans:
            patch = plan.patch
            output = plan.output
            if check:
                if plan.patch_changed:
                    self.die(f"{patch['path']}: exported patch drift")
                self.inf(f"export-check OK {output.relative_to(profile_dir)}")
                continue

            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(plan.exported)
            if (
                patch.get("source-commit") != plan.commit
                or patch.get("sha256sum") != plan.checksum
            ):
                metadata_updates[patch["path"]] = {
                    "source-commit": plan.commit,
                    "sha256sum": plan.checksum,
                }
            changed = changed or plan.patch_changed
            self.inf(f"exported {output.relative_to(profile_dir)}")

        if not check:
            if metadata_updates:
                self._update_profile_metadata(profile_path, metadata_updates)
            self.inf(
                f"updated {profile_path.relative_to(Path(self.manifest.repo_abspath))}"
                if changed
                else f"refreshed {profile_path.relative_to(Path(self.manifest.repo_abspath))}"
            )

    def _plan_export(
        self,
        profile_dir: Path,
        patches,
        selected_paths: set[str] | None,
        allow_large_output: bool,
    ) -> list[ExportPlan]:
        plans: list[ExportPlan] = []
        previous_commit_by_module: dict[str, str] = {}
        selected_modules = None
        planned_paths: set[str] = set()
        if selected_paths is not None:
            selected_modules = {
                patch["module"]
                for patch in patches
                if patch["path"] in selected_paths
            }
        for patch in patches:
            selected = selected_paths is None or patch["path"] in selected_paths
            if (
                selected_paths is not None
                and not selected
                and patch["module"] not in selected_modules
            ):
                continue
            repo = self._repo(patch["module"])
            source_branch = patch["source-branch"]
            if not self._branch_exists(repo, source_branch):
                self.die(f"{patch['module']}: missing source branch {source_branch}")

            commit = git(repo, "rev-parse", source_branch, capture=True)
            if not selected:
                previous_commit_by_module[patch["module"]] = commit
                continue
            self._validate_export_revision(repo, patch, "source-commit", commit)
            self._validate_export_revision(repo, patch, "source-base", commit)
            self._validate_export_base_ancestor(repo, patch, commit)
            self._validate_export_stack_base(
                repo,
                patch,
                commit,
                previous_commit_by_module.get(patch["module"]),
            )
            previous_commit_by_module[patch["module"]] = commit

            result = subprocess.run(
                format_patch_command(patch, commit),
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                stderr = result.stderr.decode(errors="replace").strip()
                self.die(
                    f"{patch['path']}: git format-patch failed before export writes"
                    + (f": {stderr}" if stderr else "")
                )
            exported = result.stdout
            checksum = hashlib.sha256(exported).hexdigest()
            output = profile_dir / patch["path"]
            current_content = output.read_bytes() if output.is_file() else None
            patch_changed = (
                patch.get("source-commit") != commit
                or patch.get("sha256sum") != checksum
                or current_content != exported
            )
            self._check_export_artifacts(patch, exported)
            if not allow_large_output:
                self._check_export_size(patch, exported, current_content)
            plans.append(
                ExportPlan(
                    patch=patch,
                    output=output,
                    commit=commit,
                    exported=exported,
                    checksum=checksum,
                    patch_changed=patch_changed,
                )
            )
            planned_paths.add(patch["path"])
            if selected_paths is not None and planned_paths >= selected_paths:
                break
        return plans

    def _validate_export_revision(
        self,
        repo: Path,
        patch,
        field: str,
        branch_commit: str,
    ) -> None:
        revision = patch.get(field)
        if not revision:
            return
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            self.die(f"{patch['path']}: {field} must be a full SHA")
        if (
            subprocess.run(
                ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            return
        hint = ""
        if field == "source-base":
            parent = subprocess.run(
                ["git", "rev-parse", f"{branch_commit}^"],
                cwd=repo,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout.strip()
            if parent:
                hint = f"; source branch parent is {parent}"
        elif field == "source-commit":
            hint = f"; source branch currently points to {branch_commit}"
        self.die(
            f"{patch['path']}: {field} {revision} is not available{hint}; "
            "repair stale patch metadata before exporting"
        )

    def _validate_export_base_ancestor(
        self,
        repo: Path,
        patch,
        branch_commit: str,
    ) -> None:
        source_base = patch.get("source-base")
        if not source_base:
            return
        if (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_base, branch_commit],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            return
        parent = subprocess.run(
            ["git", "rev-parse", f"{branch_commit}^"],
            cwd=repo,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
        hint = f"; source branch parent is {parent}" if parent else ""
        self.die(
            f"{patch['path']}: source-base {source_base} is not an ancestor "
            f"of source branch head {branch_commit}{hint}; repair stale patch "
            "metadata before exporting"
        )

    def _validate_export_stack_base(
        self,
        repo: Path,
        patch,
        branch_commit: str,
        previous_commit: str | None,
    ) -> None:
        source_base = patch.get("source-base")
        if not source_base or not previous_commit:
            return
        previous_is_ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", previous_commit, branch_commit],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not previous_is_ancestor or source_base == previous_commit:
            return
        self.die(
            f"{patch['path']}: source-base {source_base} is stale for the "
            f"linear {patch['module']} stack; previous profile patch is "
            f"{previous_commit}. Rebase/export metadata must use the previous "
            "patch as source-base or move this patch to a non-linear position."
        )

    def _check_export_size(
        self,
        patch,
        exported: bytes,
        current_content: bytes | None,
    ) -> None:
        max_lines = int(
            os.environ.get("WEST_PATCH_EXPORT_MAX_LINES", DEFAULT_EXPORT_MAX_LINES)
        )
        max_growth = int(
            os.environ.get("WEST_PATCH_EXPORT_MAX_GROWTH", DEFAULT_EXPORT_MAX_GROWTH)
        )
        max_bytes = int(
            os.environ.get("WEST_PATCH_EXPORT_MAX_BYTES", DEFAULT_EXPORT_MAX_BYTES)
        )
        exported_lines = exported.count(b"\n")
        exported_bytes = len(exported)
        if exported_lines <= max_lines and exported_bytes <= max_bytes:
            return
        current_lines = current_content.count(b"\n") if current_content is not None else 0
        current_bytes = len(current_content) if current_content is not None else 0
        grew_too_much = (
            current_content is None
            or exported_lines > current_lines * max_growth
            or exported_bytes > current_bytes * max_growth
        )
        if current_content is None or exported == current_content or grew_too_much:
            baseline = (
                f"{current_lines} current lines/{current_bytes} bytes"
                if current_content is not None
                else "no current file"
            )
            self.die(
                f"{patch['path']}: exported patch has {exported_lines} lines/{exported_bytes} bytes "
                f"({baseline}); pass --allow-large-output to write it"
            )

    def _check_export_artifacts(self, patch, exported: bytes) -> None:
        trailers = legacy_automation_trailers(exported)
        if trailers:
            self.die(
                f"{patch['path']}: exported patch contains "
                f"{describe_legacy_automation_trailers(trailers)}"
            )
        artifacts = generated_patch_artifacts(exported)
        if artifacts:
            self.die(
                f"{patch['path']}: exported patch contains "
                f"{describe_generated_patch_artifacts(artifacts)}"
            )

    def _update_profile_metadata(
        self, profile_path: Path, updates: dict[str, dict[str, str]]
    ) -> None:
        """Patch source-commit/sha256sum fields without reserializing YAML.

        PyYAML rewrites human-authored block scalars and quoting across the
        whole profile. Export only needs to refresh two scalar fields in the
        touched patch entries, so keep the original text layout intact.
        """
        lines = profile_path.read_text().splitlines(keepends=True)
        updated = set()
        index = 0
        while index < len(lines):
            match = re.match(r"^(\s*)- path:\s+(.+?)\s*$", lines[index])
            if not match:
                index += 1
                continue
            indent, path = match.groups()
            end = index + 1
            while end < len(lines):
                next_match = re.match(r"^(\s*)- path:\s+(.+?)\s*$", lines[end])
                if next_match and len(next_match.group(1)) <= len(indent):
                    break
                end += 1
            if path not in updates:
                index = end
                continue

            fields = updates[path]
            field_indent = indent + "  "
            present = set()
            insert_at = end
            for line_no in range(index + 1, end):
                for field, value in fields.items():
                    if re.match(rf"^{re.escape(field_indent)}{field}:\s+", lines[line_no]):
                        newline = "\n" if lines[line_no].endswith("\n") else ""
                        lines[line_no] = f"{field_indent}{field}: {value}{newline}"
                        present.add(field)
                if re.match(rf"^{re.escape(field_indent)}github:\s*$", lines[line_no]):
                    insert_at = min(insert_at, line_no)
            missing = [field for field in ("source-commit", "sha256sum") if field not in present]
            if missing:
                new_lines = [f"{field_indent}{field}: {fields[field]}\n" for field in missing]
                lines[insert_at:insert_at] = new_lines
                end += len(new_lines)
            updated.add(path)
            index = end

        missing_updates = sorted(set(updates) - updated)
        if missing_updates:
            self.die(f"{profile_path}: failed to update entries: {', '.join(missing_updates)}")
        profile_path.write_text("".join(lines))

    def _verify_applicability(self, profile_dir: Path, grouped):
        with tempfile.TemporaryDirectory(prefix="west-patch-verify-") as temp:
            temp_root = Path(temp)
            for index, (module, module_patches) in enumerate(grouped.items()):
                repo = self._repo(module)
                worktree = temp_root / str(index)
                # Verify each module's patches apply on the SAME base they will
                # be applied on: manifest-rev, or the base profile's integration
                # tip when stacking.
                revision = self._base_revision(module)
                git(
                    repo,
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(worktree),
                    revision,
                )
                try:
                    for patch in module_patches:
                        git_for_temporary_patch_application(
                            worktree,
                            "am",
                            "--3way",
                            "--committer-date-is-author-date",
                            str(profile_dir / patch["path"]),
                        )
                finally:
                    self._abort_am(worktree)
                    git(
                        repo,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                        check=False,
                    )

    def _patch_state(self, repo: Path, patch_path: Path) -> str:
        """Classify a patch against the CURRENT working tree of `repo`.

        Unlike `verify` (which checks the patches against a throwaway worktree at
        the frozen manifest revision), this inspects the tree you actually build
        from -- so it catches the case where the build tree drifted away from
        patches.yml and a tracked fix is silently missing.

          APPLIED  - the patch reverse-applies cleanly: its content is present
                     verbatim against the current tree.
          MISSING  - the patch forward-applies cleanly: its content is ABSENT.
                     This is the authoritative "lost patch" signal.
          STACKED? - neither clean reverse nor clean forward. For a standalone
                     patch this means partially applied / context-shifted /
                     conflicting. But it is ALSO the normal result for a member
                     of an interdependent patch SERIES (e.g. eunion-*): once a
                     later patch in the series edits the same lines, an earlier
                     patch no longer reverse-applies verbatim even though it is
                     fully present. So STACKED? is NOT proof of drift -- only
                     MISSING is. Trust a clean `west patch apply`/`verify` as the
                     authoritative applicability check for stacked series.
        """
        reverse_ok = (
            subprocess.run(
                ["git", "apply", "--reverse", "--check", str(patch_path)],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if reverse_ok:
            return "APPLIED"
        forward_ok = (
            subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if forward_ok:
            return "MISSING"
        return "STACKED?"

    def _status(self, profile_dir: Path, patches, strict: bool):
        grouped = self._group(patches)
        counts = {"APPLIED": 0, "MISSING": 0, "STACKED?": 0, "NOFILE": 0}
        for module, module_patches in grouped.items():
            repo = self._repo(module)
            branch = git(repo, "branch", "--show-current", capture=True) or (
                "(detached " + git(repo, "rev-parse", "--short", "HEAD", capture=True) + ")"
            )
            self.inf(f"{module}  [{branch}]")
            for patch in module_patches:
                patch_path = profile_dir / patch["path"]
                if not patch_path.is_file():
                    state = "NOFILE"
                else:
                    state = self._patch_state(repo, patch_path)
                counts[state] = counts.get(state, 0) + 1
                bead = patch.get("bead", "-")
                self.inf(f"  {state:8} {patch['path']}  [{bead}]")
        total = sum(counts.values())
        self.inf(
            f"status: {counts['APPLIED']} applied, {counts['MISSING']} missing, "
            f"{counts['STACKED?']} stacked?, {counts['NOFILE']} no-file "
            f"(of {total})"
        )
        if counts["STACKED?"]:
            self.inf(
                "note: STACKED? is expected for interdependent series (eunion-*) "
                "and is NOT drift; only MISSING means a tracked patch is absent."
            )
        # MISSING is the authoritative drift signal; STACKED? is not (see
        # _patch_state). A NOFILE is a real error (patches.yml references a
        # patch file that does not exist).
        if strict and (counts["MISSING"] or counts["NOFILE"]):
            self.die(
                f"{counts['MISSING']} missing + {counts['NOFILE']} no-file "
                "patch(es) -- build tree drifted from patches.yml"
            )

    def _abort_am(self, repo: Path):
        am_state = git(
            repo, "rev-parse", "--git-path", "rebase-apply", capture=True
        )
        if (repo / am_state).exists():
            git(repo, "am", "--abort", check=False)

    def _reset_submodule_index(self, repo: Path):
        """Reset submodule gitlink entries in the index to match HEAD.

        Parking the submodules (detaching them onto their base revisions) moves
        each nested module's HEAD, which dirties the superproject's gitlink
        pointers in the index. `git am` runs its own dirty-index guard that --
        unlike our `_ensure_clean` (which passes --ignore-submodules=all) -- is
        NOT submodule-aware, so it refuses with "Dirty index: cannot apply".
        The darling-module patches only touch real files (e.g. mldr.c), never
        the gitlinks, so restoring the gitlink index entries to HEAD makes the
        index clean from `git am`'s perspective without dropping any real change.
        The submodules' own working trees / branches are untouched."""
        dirty = git(
            repo,
            "diff",
            "--cached",
            "--ignore-submodules=none",
            "--name-only",
            "--diff-filter=M",
            capture=True,
        )
        paths = [
            line
            for line in dirty.splitlines()
            if (repo / line / ".git").exists()
        ]
        for path in paths:
            git(repo, "reset", "--quiet", "HEAD", "--", path)

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        )

    def _apply(
        self,
        profile: str,
        profile_dir: Path,
        patches,
        integration_date: str,
        roll_back: bool,
    ):
        lock_source = Path(self.manifest.repo_abspath) / "west.lock.yml"
        if not lock_source.is_file():
            self.die(f"frozen manifest not found: {lock_source}")

        branch = f"integration/{profile}"
        grouped = self._group(patches)
        # A stacked profile requires its base profile to be applied first.
        self._require_base_applied(list(grouped))
        modules = list(grouped)
        if "darling" not in modules:
            modules.append("darling")
        for module in modules:
            try:
                self._ensure_generated_context(module, profile)
            except RuntimeError as error:
                self.die(str(error))

        touched = []
        try:
            for module, module_patches in grouped.items():
                repo = self._repo(module)
                self._prepare(
                    module, repo, branch, parent=module == "darling"
                )
                touched.append(repo)
                if module == "darling":
                    # `git am` on the superproject has a submodule-unaware
                    # dirty-index guard; parked submodule gitlinks trip it even
                    # though the patches only touch real files. See helper.
                    self._reset_submodule_index(repo)
                for patch in module_patches:
                    path = self._verify_patch(profile_dir, patch)
                    git_for_patch_application(
                        repo,
                        "am",
                        "--3way",
                        "--committer-date-is-author-date",
                        str(path),
                    )
                self.inf(f"{module}: applied {len(module_patches)} patches")
            lock = self._record_integration(profile, grouped, integration_date)
            self.inf(f"wrote {lock}")
        except Exception as error:
            for repo in touched:
                self._abort_am(repo)
            if roll_back:
                self._reset(profile, grouped, force=True)
            self.die(str(error))

    def _record_integration(
        self, profile: str, grouped, integration_date: str
    ):
        branch = f"integration/{profile}"
        darling = self._repo("darling")
        if "darling" not in grouped:
            self._prepare("darling", darling, branch, parent=True)

        nested = [module for module in grouped if module != "darling"]
        if nested:
            paths = [str(Path(module).relative_to("darling")) for module in nested]
            git(darling, "add", *paths)
            commit_env = os.environ.copy()
            commit_env["GIT_AUTHOR_DATE"] = integration_date
            commit_env["GIT_COMMITTER_DATE"] = integration_date
            git(
                darling,
                "commit",
                "-m",
                f"Integrate {profile} patch profile",
                env=commit_env,
            )

        # Seed the lock from the base profile's lock when stacking (so modules
        # the stacked profile does NOT patch keep the base profile's revisions,
        # not the raw manifest revision); otherwise from the frozen manifest.
        if self._base_profile:
            base_lock = (
                Path(self.manifest.repo_abspath)
                / "patches"
                / self._base_profile
                / "west.lock.yml"
            )
            lock_path = base_lock if base_lock.is_file() else (
                Path(self.manifest.repo_abspath) / "west.lock.yml"
            )
        else:
            lock_path = Path(self.manifest.repo_abspath) / "west.lock.yml"
        lock_data = yaml.safe_load(lock_path.read_text())
        revisions = {"darling": git(darling, "rev-parse", "HEAD", capture=True)}
        for module in grouped:
            revisions[module] = git(
                self._repo(module), "rev-parse", "HEAD", capture=True
            )
        for project in lock_data["manifest"]["projects"]:
            path = project.get("path", project["name"])
            if path in revisions:
                project["revision"] = revisions[path]

        output = (
            Path(self.manifest.repo_abspath)
            / "patches"
            / profile
            / "west.lock.yml"
        )
        output.write_text(yaml.safe_dump(lock_data, sort_keys=False, width=1000))
        return output

    def _reset(self, profile: str, grouped, force: bool):
        branch = f"integration/{profile}"
        modules = list(grouped)
        if "darling" not in modules:
            modules.append("darling")

        for module in modules:
            if not force:
                try:
                    self._ensure_generated_context(module, profile)
                except RuntimeError as error:
                    self.die(f"refusing to clean: {error}")

        for module in reversed(modules):
            repo = self._repo(module)
            self._abort_am(repo)
            # Reset to the base this profile was applied on: manifest-rev, or
            # the base profile's integration tip when stacking. Resetting a
            # stacked profile therefore leaves the base profile intact.
            revision = self._base_revision(module)
            switch_args = ["switch", "--detach", revision]
            if force:
                switch_args.insert(1, "--discard-changes")
            git(repo, *switch_args)
            if self._branch_exists(repo, branch):
                git(repo, "branch", "-D", branch)
            self.inf(f"{module}: reset to {revision}")

    def _clean(self, profile: str, patches, force: bool):
        self._reset(profile, self._group(patches), force=force)
