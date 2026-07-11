"""Darling workspace test orchestrator.

`west test` is a thin layer over CTest, in the same spirit as gVisor's Bazel
test targets and Wine's winetest: the runner sits ON TOP of the build system,
it does not reinvent discovery/parallelism/JUnit/WILL_FAIL. CTest owns those.

This command adds the three things CTest does not give for free in this repo:

  --changed   map changed submodules (from the west manifest + git diff) to the
              `submod:<name>` CTest labels, so a quick local cycle runs only the
              tests a PR could affect.
  --submodule PATH
              map an explicit West project path/name to `submod:<name>`.
  --bead ID   run the regression(s) attached to an issue (label `bead:<id>`),
              turning the beads graph into a live regression set.
  --executor  the darling-debug-runner binary used by the guarded/forensic
              diagnosis tiers, so a hang becomes a captured, timed-out failure
              instead of a stall (the tier is set per-test in add_compat_test).

Patch metadata can point at local scripts/build targets or at CTest labels.
CTest remains the execution backend for suite-style tests; west owns patch
selection, profile materialization, resource provisioning, and diagnostics.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from shlex import quote, join as shell_join

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefix_repair import (
    cleanup_prefix_mounts,
    darling_init_pid_is_usable,
    eunion_prefix_prerequisite_problems,
    guest_c_fixture_prerequisite_problems,
    prefix_boot_prerequisite_problems,
)
from test_ctest import (
    ctest_command,
    ctest_label_args,
    ctest_label_display,
    ctest_selection_command,
    ctest_selector_label_args,
    ctest_runtime_group_passthrough,
    ctest_test_name_regex,
    ctest_uses_prefix,
)
from test_dispatch import dispatch_fixture_runner
from test_cmake import archive_git_tree_to, archive_source_to, run_darling_cmake_target_fixture
from test_execution import run_bounded
from test_guest_execution import (
    resolve_guest_execution,
    run_guest_command_fixture,
    run_guest_shell,
    shutdown_guest_prefix,
)
try:
    from .test_guest_c import run_guest_c_fixture
except ImportError:  # Loaded as a West extension module, not a package.
    from test_guest_c import run_guest_c_fixture
from test_manifest import ManifestError, load_test_profile
from test_prefix import (
    cleanup_rootless_prefix_processes,
    darlingserver_pids_for_prefix,
    prefix_process_snapshot,
    remove_stale_init_pid,
    rootless_prefix_process_snapshot,
)
from test_resources import resource_context
from test_results import InvocationResult
from test_selection import select_metadata_tests
from test_runtime import (
    compose_ctest_runtime_profiles,
    describe_runtime_deploy_plan,
    is_macho_binary,
    load_ctest_runtime_profiles,
    parse_macho_dylib_dependencies,
    parse_macho_dylib_id,
    partition_ctest_runtime_profiles,
    runtime_artifact_deploy_paths,
    runtime_build_targets,
    runtime_deploy_targets,
    ROOTLESS_BOOTSTRAP_CLOSURE_RESOURCE,
    resolve_macho_runtime_closure,
)
from test_worktrees import prune_stale_west_temp_worktrees, remove_temporary_worktree


_BOOTSTRAP_FATAL_SIGNAL = re.compile(
    r"--- (?P<signal>SIG(?:SEGV|BUS|ILL|ABRT)) \{(?P<details>[^}]*)\} ---"
)
_ROOTLESS_BOOTSTRAP_TRACE_FILES = (
    ("host", Path(".west-rootless-boot.log")),
    ("guest-path", Path("private/var/tmp/.west-rootless-boot.log")),
    ("guest-fd", Path(".west-rootless-guest-fd.log")),
)


def bootstrap_trace_fatal_signal(trace_dir: Path) -> str | None:
    """Return a guest fault recorded by an opt-in bootstrap strace, if any."""

    for trace in sorted(trace_dir.glob("bootstrap*")):
        if not trace.is_file():
            continue
        match = _BOOTSTRAP_FATAL_SIGNAL.search(trace.read_text(errors="replace"))
        if match is None:
            continue
        fault = re.search(r"si_addr=([^, }]+)", match.group("details"))
        location = f" at {fault.group(1)}" if fault is not None else ""
        return f"{match.group('signal')}{location}"
    return None


def rootless_bootstrap_progress(prefix: Path) -> str | None:
    """Summarize the latest observable stage from each rootless boot trace."""

    stages = []
    for label, relative_path in _ROOTLESS_BOOTSTRAP_TRACE_FILES:
        trace_path = prefix / relative_path
        try:
            with trace_path.open("rb") as trace:
                trace.seek(0, os.SEEK_END)
                start = max(0, trace.tell() - 4096)
                trace.seek(start)
                content = trace.read().decode(errors="replace")
        except OSError:
            continue
        for line in reversed(content.splitlines()):
            stage = line.strip()
            if stage:
                stages.append(f"{label}={stage[:512]}")
                break
    return " | ".join(stages) if stages else None


class RuntimeProfileDeployment:
    """One materialized runtime provider currently deployed under a prefix."""

    def __init__(
        self,
        *,
        name: str,
        prefix: Path,
        build_root: Path,
        env: dict[str, str],
    ):
        self.name = name
        self.prefix = prefix
        self.build_root = build_root
        self.env = env


class DarlingTest(WestCommand):
    def __init__(self):
        super().__init__(
            "test",
            "Run Darling regression/compat tests (changed-only, by bead, or full)",
            "Discover and run compat tests via ctest with changed/bead targeting",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
            "--changed",
            action="store_true",
            help="run only tests labelled for submodules changed vs upstream",
        )
        parser.add_argument(
            "--bead",
            metavar="ID",
            help="run tests attached to a bead (label bead:<ID>)",
        )
        parser.add_argument(
            "--submodule",
            action="append",
            default=[],
            metavar="PATH",
            help="run CTest-backed tests labelled for a West project path/name",
        )
        parser.add_argument(
            "--profile",
            metavar="NAME",
            help="run tests declared by a patch profile's patches.yml metadata",
        )
        parser.add_argument(
            "--patch",
            metavar="PATH",
            help="run tests declared for one patch path in patches.yml metadata",
        )
        parser.add_argument(
            "--red-only",
            action="store_true",
            help="with --profile/--patch, select only tests marked red: true",
        )
        parser.add_argument(
            "--prove-red",
            action="store_true",
            help="with --profile/--patch, run RED proof mode; normal runs still expect GREEN on current checkout",
        )
        parser.add_argument(
            "--red-audit",
            action="store_true",
            help="with --profile, list patches missing tests or test-exception",
        )
        parser.add_argument(
            "--env",
            choices=("host", "darling", "macos"),
            help="restrict to one environment",
        )
        parser.add_argument(
            "--prefix",
            metavar="PATH",
            help="Darling prefix for guest tests; accepts PATH or existing:PATH",
        )
        parser.add_argument(
            "--prefix-profile",
            metavar="NAME",
            help="named Darling prefix shortcut (homebrew -> ~/work/darling-prefix-homebrew-test)",
        )
        parser.add_argument(
            "--with-runtime-profile",
            action="append",
            default=[],
            metavar="NAME",
            help="add a declared CTest guest runtime provider without changing test selection; useful for reproducing artifact interactions",
        )
        parser.add_argument(
            "--bootstrap-runtime-profile",
            metavar="NAME",
            help="with --prefix, --prefix-profile, or DPREFIX: build and retain one declared runtime provider as the selected prefix baseline, then prove it with a bounded guest smoke",
        )
        parser.add_argument(
            "--bootstrap-syscall-trace",
            metavar="DIR",
            help="with --bootstrap-runtime-profile, save strace -ff output for the bounded guest smoke in DIR",
        )
        parser.add_argument(
            "--bootstrap-syscall-stack",
            action="store_true",
            help="with --bootstrap-syscall-trace, include native stack frames in its strace output",
        )
        parser.add_argument(
            "--keep-prefix-running",
            action="store_true",
            help="do not shut down a Darling prefix after prefix-backed metadata tests",
        )
        parser.add_argument(
            "--no-overlayfs",
            action="store_true",
            help="run Darling prefix tests with DARLING_NOOVERLAYFS=1",
        )
        parser.add_argument(
            "--materialize-profile",
            action="store_true",
            help="run profile metadata tests from temporary worktrees built from manifest revisions plus patch files",
        )
        parser.add_argument(
            "--executor",
            metavar="PATH",
            help="darling-debug-runner binary for guarded/forensic tiers",
        )
        parser.add_argument(
            "--diag",
            choices=("bare", "guarded", "forensic"),
            help="restrict to one diagnosis tier; matches the RESOLVED tier, so "
            "guarded/forensic that fell back to bare (no executor) count as bare",
        )
        parser.add_argument(
            "--label",
            metavar="REGEX",
            help="restrict to tests whose CTest label matches (e.g. 'macos:15' "
            "for a CI version row); passed through as ctest -L",
        )
        parser.add_argument(
            "--fuzz",
            action="store_true",
            help="restrict CTest suite selection to tests labelled fuzz:*",
        )
        parser.add_argument(
            "--stress",
            action="store_true",
            help="restrict CTest suite selection to tests labelled stress:*",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="list selected tests and exit (no run)",
        )
        parser.add_argument(
            "--ctest-timeout-seconds",
            type=int,
            default=3600,
            metavar="SECONDS",
            help="outer deadline for one selected CTest invocation (default 3600)",
        )
        parser.add_argument(
            "--gc",
            action="store_true",
            help="prune old debug bundles (keep-last + size cap) and exit",
        )
        parser.add_argument(
            "--keep-last",
            type=int,
            default=20,
            metavar="N",
            help="bundles to keep when pruning (default 20)",
        )
        parser.add_argument(
            "--max-bundle-mb",
            type=int,
            default=64,
            metavar="MB",
            help="drop bundles larger than this when pruning (default 64)",
        )
        parser.add_argument(
            "--bundle-root",
            metavar="DIR",
            default="~/work/darling-debug",
            help="debug bundle directory (default ~/work/darling-debug)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="with --gc, show what would be pruned without deleting",
        )
        parser.add_argument(
            "--proof-scratch-root",
            metavar="DIR",
            default=tempfile.gettempdir(),
            help="with --gc, directory to scan for stale runtime scratch and guest "
            f"runner output (default {tempfile.gettempdir()})",
        )
        parser.add_argument(
            "--proof-scratch-max-age-hours",
            type=float,
            default=24.0,
            metavar="HOURS",
            help="with --gc, prune stale west runtime/source-profile scratch dirs older than this "
            "(default 24)",
        )
        parser.add_argument(
            "--proof-scratch-keep-last",
            type=int,
            default=2,
            metavar="N",
            help="with --gc, keep at most N newest west runtime/source-profile scratch dirs "
            "regardless of age (default 2)",
        )
        return parser

    # --- helpers ------------------------------------------------------------

    def _testkit_dir(self) -> Path:
        return Path(self.manifest.repo_abspath) / "testkit"

    def _require_runtime_scratch_space(self, deployment_name: str) -> None:
        configured_minimum = os.environ.get("WEST_RUNTIME_MIN_FREE_BYTES", str(8 * 1024**3))
        try:
            minimum = int(configured_minimum)
        except ValueError:
            self.die(
                "WEST_RUNTIME_MIN_FREE_BYTES must be an integer number of bytes "
                "greater than or equal to 0"
            )
        if minimum < 0:
            self.die("WEST_RUNTIME_MIN_FREE_BYTES must be >= 0")
        scratch_root = Path(tempfile.gettempdir())
        available = shutil.disk_usage(scratch_root).free
        if available < minimum:
            self.die(
                f"Runtime deployment {deployment_name} needs at least {minimum} free bytes "
                f"under {scratch_root}, but only {available} are available; "
                "run west test --gc or free disk space before materializing the runtime source forest"
            )

    def _preflight_runtime_profile_stack(
        self, source_profile: str, deployment_name: str
    ) -> None:
        """Verify every layer of a runtime source stack before materializing it.

        Runtime forests reconstruct a profile from manifest revisions, applying
        its base profiles in order.  A broken intermediate layer otherwise
        fails only after the runner has made a large disposable forest (and may
        be mistaken for a runtime RED result).  Reuse ``west patch verify`` as
        the single authority for patch integrity and applicability, and cache
        successful stacks for the current invocation.
        """

        verified = getattr(self, "_verified_runtime_profile_stacks", set())
        if source_profile in verified:
            return
        try:
            stack = self._profile_stack(source_profile)
        except SystemExit:
            raise
        except Exception as error:
            self.die(
                f"Runtime deployment {deployment_name} cannot resolve source profile "
                f"{source_profile!r}: {error}"
            )

        for profile in stack:
            self.inf(
                f"  runtime profile preflight: {profile} "
                f"for {deployment_name}"
            )
            result = run_bounded(
                ["west", "patch", "verify", "--profile", profile],
                cwd=Path(self.topdir),
                env=None,
                timeout_seconds=300,
                capture_output=True,
            )
            if result.returncode:
                self._dump_command_tail(
                    f"Runtime profile {profile} preflight", result
                )
                self.die(
                    f"Runtime deployment {deployment_name} cannot materialize "
                    f"source profile stack {source_profile!r}: {profile!r} failed "
                    "patch applicability preflight. Repair or rebase that profile "
                    "with `west patch verify --profile "
                    f"{profile}` before retrying; this is not a runtime test failure."
                )
        verified.add(source_profile)
        self._verified_runtime_profile_stacks = verified

    def _profile_path(self, profile: str) -> Path:
        return Path(self.manifest.repo_abspath) / "patches" / profile / "patches.yml"

    def _load_profile(self, profile: str) -> dict:
        path = self._profile_path(profile)
        if not path.is_file():
            self.die(f"patch profile not found: {path}")
        try:
            return load_test_profile(path)
        except ManifestError as error:
            self.die(str(error))

    def _profile_modules(self, profile: str) -> set[str]:
        modules = {
            patch["module"]
            for patch in self._load_profile(profile).get("patches", [])
            if patch.get("module")
        }
        if modules:
            modules.add("darling")
        return modules

    def _profile_is_applied(self, profile: str) -> bool:
        expected = f"integration/{profile}"
        for module in self._profile_modules(profile):
            repo = self._project_path(module)
            current = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if current != expected:
                return False
        return True

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        )

    def _worktree_dirty(self, repo: Path, *, parent: bool = False) -> bool:
        command = ["git", "status", "--porcelain"]
        if parent:
            command.extend(["--ignore-submodules=all", "--untracked-files=no"])
        return bool(
            subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        )

    def _checkout_state(self, repo: Path) -> tuple[str, str]:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if branch:
            return ("branch", branch)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return ("detach", head)

    def _restore_checkout_state(self, repo: Path, state: tuple[str, str]) -> None:
        kind, value = state
        args = ["git", "switch", value] if kind == "branch" else ["git", "switch", "--detach", value]
        subprocess.run(args, cwd=repo, check=True)

    @contextmanager
    def _profile_worktree_checkout(self, profile: str):
        projects = self._projects()
        modules = sorted(
            self._profile_stack_modules(profile),
            key=lambda module: (len(Path(module).parts), module),
        )
        repos = [(module, projects[module]) for module in modules]

        previous_overrides = getattr(self, "_project_overrides", {})
        added: list[tuple[Path, Path]] = []
        with tempfile.TemporaryDirectory(prefix=f"west-profile-{profile}-") as temp:
            root = Path(temp)
            overrides = dict(previous_overrides)
            try:
                for module, repo in repos:
                    target = root / module
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    revision = self._manifest_revision(module)
                    self.inf(f"  materialize {module}: {revision} -> {target}")
                    subprocess.run(
                        ["git", "worktree", "add", "--quiet", "--detach", str(target), revision],
                        cwd=repo,
                        check=True,
                    )
                    added.append((repo, target))
                    for ref, project_path in projects.items():
                        if project_path == repo:
                            overrides[ref] = target
                    overrides[module] = target
                self._project_overrides = overrides
                for stacked in self._profile_stack(profile):
                    data = self._load_profile(stacked)
                    profile_dir = Path(self.manifest.repo_abspath) / "patches" / stacked
                    for patch in data.get("patches", []):
                        target = overrides.get(patch["module"])
                        if target is None:
                            continue
                        patch_file = profile_dir / patch["path"]
                        self.inf(f"  apply {stacked}/{patch['path']}")
                        subprocess.run(
                            [
                                "git",
                                "-c",
                                "gc.auto=0",
                                "-c",
                                "maintenance.auto=false",
                                "am",
                                "--3way",
                                str(patch_file),
                            ],
                            cwd=target,
                            check=True,
                        )
                yield
            finally:
                self._project_overrides = previous_overrides
                # A Ctrl-C can arrive while the command is unwinding. Do not let
                # it interrupt the worktree removals and leave a live profile in
                # /tmp; restore the caller's handler immediately afterwards.
                previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    cleanup_errors = []
                    for repo, target in reversed(added):
                        error = remove_temporary_worktree(repo, target)
                        if error:
                            cleanup_errors.append(error)
                finally:
                    signal.signal(signal.SIGINT, previous_sigint)
                if cleanup_errors:
                    self.die(
                        f"{profile}: failed to remove temporary profile worktree(s): "
                        f"{'; '.join(cleanup_errors)}"
                    )

    @contextmanager
    def _profile_checkout(self, profile: str):
        branch = f"integration/{profile}"
        repos = [
            (module, self._project_path(module))
            for module in sorted(self._profile_modules(profile))
        ]
        dirty = [
            module
            for module, repo in repos
            if self._worktree_dirty(repo, parent=module == "darling")
        ]
        if dirty:
            self.die(
                f"cannot materialize profile {profile!r}; dirty worktree(s): "
                f"{', '.join(dirty)}"
            )

        states = [(repo, self._checkout_state(repo)) for _, repo in repos]
        try:
            missing = [
                module
                for module, repo in repos
                if not self._branch_exists(repo, branch)
            ]
            if missing:
                self.inf(
                    f"  profile {profile!r} missing integration branch in: "
                    f"{', '.join(missing)}"
                )
                self.inf(f"  generating integration/{profile} with west patch apply")
                subprocess.run(
                    ["west", "patch", "clean", "--profile", profile, "--force"],
                    cwd=self.topdir,
                    check=True,
                )
                subprocess.run(
                    ["west", "patch", "apply", "--profile", profile],
                    cwd=self.topdir,
                    check=True,
                )
            for module, repo in repos:
                self.inf(f"  materialize {module}: {branch}")
                subprocess.run(["git", "switch", branch], cwd=repo, check=True)
            yield
        finally:
            for repo, state in reversed(states):
                self._restore_checkout_state(repo, state)

    def _resolve_prefix(self, args) -> str | None:
        self._prefix_env = {}
        if args.no_overlayfs:
            self._prefix_env["DARLING_NOOVERLAYFS"] = "1"
        if args.prefix and args.prefix_profile:
            self.die("--prefix and --prefix-profile are mutually exclusive")
        if args.prefix:
            prefix = args.prefix
            if prefix.startswith("existing:"):
                prefix = prefix.removeprefix("existing:")
            return str(Path(prefix).expanduser())
        if args.prefix_profile:
            profiles = {
                "homebrew": "~/work/darling-prefix-homebrew-test",
                "smoke": "~/work/darling-prefix-smoke",
            }
            if args.prefix_profile == "homebrew":
                self._prefix_env["DARLING_NOOVERLAYFS"] = "1"
            return str(Path(profiles.get(args.prefix_profile, args.prefix_profile)).expanduser())
        if os.environ.get("DPREFIX"):
            return os.environ["DPREFIX"]
        return None

    def _resolve_darling_launcher(self, prefix: str | None) -> str | None:
        if prefix:
            candidate = Path(prefix).expanduser() / "bin" / "darling"
            if candidate.exists():
                return str(candidate)
            # An explicit prefix is a runtime identity, not just an artifact
            # directory. Falling back to another prefix's launcher silently
            # mixes launcher and DPREFIX, which can make a broken named prefix
            # appear usable for one test lifecycle.
            return None
        if os.environ.get("DARLING"):
            return os.environ["DARLING"]
        if os.environ.get("DARLING_LAUNCHER"):
            return os.environ["DARLING_LAUNCHER"]
        candidate = Path("~/work/darling-prefix/bin/darling").expanduser()
        if candidate.exists():
            return str(candidate)
        return None

    def _darling_prefix_env(self, prefix: str | Path) -> dict[str, str]:
        prefix_text = str(prefix)
        env = {
            "DPREFIX": prefix_text,
            "DARLING_PREFIX": prefix_text,
        }
        env.update(getattr(self, "_prefix_env", {}))
        return env

    def _resolve_executor(self, explicit: str | None) -> str | None:
        if explicit:
            return str(Path(explicit).expanduser())
        path = shutil.which("darling-debug-runner")
        if path:
            return path
        project = self._projects().get("darling-debug-runner")
        if project is None:
            return None
        repo = project
        candidates = [
            repo / "target" / "release" / "darling-debug-runner",
            repo / "target" / "debug" / "darling-debug-runner",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    @staticmethod
    def _resolved_diag(test) -> str:
        diag = test.get("diag")
        if diag:
            return diag
        return "guarded" if test.get("env") == "darling" else "bare"

    def _projects(self) -> dict[str, Path]:
        projects: dict[str, Path] = {}
        for project in self.manifest.projects:
            projects[project.name] = Path(project.abspath)
            projects[project.path] = Path(project.abspath)
        return projects

    def _manifest_revision(self, ref: str) -> str:
        for project in self.manifest.projects:
            if ref in {project.name, project.path}:
                revision = project.revision
                repo = Path(project.abspath)
                if not revision or subprocess.run(
                    ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                    cwd=repo,
                    check=False,
                ).returncode != 0:
                    self.die(
                        f"{ref}: manifest revision {revision or '<empty>'} "
                        f"is not available; run west update {project.name}"
                    )
                return revision
        self.die(f"unknown West project: {ref}")

    def _profile_stack(self, profile: str) -> list[str]:
        data = self._load_profile(profile)
        base = data.get("base-profile")
        if not base:
            return [profile]
        if base == profile:
            self.die(f"{profile}: base-profile cannot be itself")
        return [*self._profile_stack(base), profile]

    def _profile_stack_modules(self, profile: str) -> set[str]:
        modules: set[str] = set()
        for stacked in self._profile_stack(profile):
            modules.update(self._profile_modules(stacked))
        return modules

    def _project_path(self, ref: str) -> Path:
        overrides = getattr(self, "_project_overrides", {})
        if ref in overrides:
            return overrides[ref]
        projects = self._projects()
        if ref in projects:
            return projects[ref]
        path = Path(self.topdir) / ref
        if path.exists():
            return path
        self.die(f"unknown West project or path: {ref}")

    def _metadata_tests(
        self,
        profile: str,
        patch_path: str | None,
        bead: str | None,
        env: str | None,
        diag: str | None,
        red_only: bool,
    ):
        selection = select_metadata_tests(
            self._load_profile(profile),
            patch_path=patch_path,
            bead=bead,
            env=env,
            diag=diag,
            red_only=red_only,
            resolved_diag=self._resolved_diag,
        )
        if patch_path and not selection.found_patch:
            self.die(f"{profile}: patch not found or has no selected tests: {patch_path}")
        return selection.selected, selection.missing

    def _test_invocation(self, patch, test):
        """Resolve structured patch metadata to a concrete local invocation.

        `command` is intentionally still supported as an escape hatch, but the
        common cases should be structured so west owns how tests are launched.
        """
        proof = test.get("red-proof") if isinstance(test.get("red-proof"), dict) else {}
        source_env = test.get("source-env") or proof.get("source-env")
        source_module = proof.get("source-module", patch["module"])
        if test.get("command"):
            return {
                "key": f"shell:{test['command']}",
                "display": test["command"],
                "cwd": Path(self.topdir),
                "args": test["command"],
                "shell": True,
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
            }
        if test.get("ctest-label") and not test.get("runner"):
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            return {
                "key": f"ctest-label:{test['ctest-label']}",
                "display": self._display_ctest_label(test["ctest-label"]),
                "cwd": Path(self.topdir),
                "args": None,
                "shell": False,
                "env": env,
                "ctest_label": test["ctest-label"],
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
            }

        runner = test.get("runner", "script" if test.get("script") else None)
        if runner == "west-build":
            target = test["target"]
            args = [
                "west",
                "darling-build",
                "--force",
                "--skip-doctor",
                "--targets",
                target,
            ]
            return {
                "key": " ".join(args),
                "display": " ".join(args),
                "cwd": Path(self.topdir),
                "args": args,
                "shell": False,
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
            }
        if runner in {
            "script",
            "source-contract-script",
            "source-profile-script",
            "self-contract-script",
            "guest-runtime-script",
        }:
            repo = test.get("repo", patch["module"])
            script = test["script"]
            script_args = [str(arg) for arg in test.get("args", [])]
            args = [str(Path(script)), *script_args]
            prefix = ""
            if test.get("env-vars"):
                prefix = " ".join(
                    f"{quote(str(key))}={quote(str(value))}"
                    for key, value in test["env-vars"].items()
                ) + " "
            display_args = " ".join(quote(arg) for arg in args)
            if runner in {
                "source-contract-script",
                "source-profile-script",
                "self-contract-script",
                "guest-runtime-script",
            }:
                display = f"cd {quote(repo)} && <{runner}> {prefix}{display_args}"
            else:
                display = f"cd {quote(repo)} && {prefix}{display_args}"
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            cwd = self._project_path(repo)
            script_path = cwd / script
            return {
                "key": display,
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "repo": repo,
                "script": script,
                "args": args,
                "shell": False,
                "runner": runner,
                "env": env,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
                "host_trace_files": list(test.get("host-trace-files", [])),
                "host_temp_files": list(test.get("host-temp-files", [])),
                "host_trace_oracle": bool(test.get("host-trace-oracle", False)),
            }
        if runner == "python":
            repo = test.get("repo", patch["module"])
            script = test["script"]
            script_args = [str(arg) for arg in test.get("args", [])]
            args = ["python3", str(Path(script)), *script_args]
            prefix = ""
            if test.get("env-vars"):
                prefix = " ".join(
                    f"{quote(str(key))}={quote(str(value))}"
                    for key, value in test["env-vars"].items()
                ) + " "
            display = f"cd {quote(repo)} && {prefix}{' '.join(quote(arg) for arg in args)}"
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            cwd = self._project_path(repo)
            script_path = cwd / script
            return {
                "key": display,
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "repo": repo,
                "script": script,
                "args": args,
                "shell": False,
                "env": env,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
            }
        if runner == "c-fixture":
            repo = test.get("repo", patch["module"])
            script = test["script"]
            cwd = self._project_path(repo)
            script_path = cwd / script
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            cc = str(test.get("cc", os.environ.get("CC", "cc")))
            output = f"<temp>/{Path(script).stem}"
            display_parts = [quote(cc), *[quote(str(flag)) for flag in test.get("compile-flags", [])]]
            for include_dir in test.get("fixture-include-dirs", []):
                display_parts.extend(["-I", quote(str(include_dir))])
            for include_dir in test.get("include-dirs", []):
                display_parts.extend(["-I", quote(str(include_dir))])
            if test.get("stub-headers") or test.get("generated-headers"):
                display_parts.extend(["-I", "<generated-stubs>"])
            for source_file in test.get("source-files", []):
                display_parts.append(quote(str(source_file)))
            display_parts.extend([quote(script), "-o", quote(output)])
            display = f"cd {quote(repo)} && {' '.join(display_parts)} && {quote(output)}"
            return {
                "key": (
                    f"c-fixture:{repo}:{script}:"
                    f"{repr(test.get('compile-flags', []))}:"
                    f"{repr(test.get('source-files', []))}:"
                    f"{repr(test.get('include-dirs', []))}:"
                    f"{repr(test.get('fixture-include-dirs', []))}:"
                    f"{repr(test.get('stub-headers', []))}:"
                    f"{repr(sorted((test.get('generated-headers') or {}).keys()))}:"
                    f"{repr(test.get('source-root-module', ''))}"
                ),
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "repo": repo,
                "script": script,
                "args": None,
                "shell": False,
                "env": env,
                "c_fixture": True,
                "cc": cc,
                "include_dirs": [str(item) for item in test.get("include-dirs", [])],
                "fixture_include_dirs": [
                    str(item) for item in test.get("fixture-include-dirs", [])
                ],
                "stub_headers": [str(item) for item in test.get("stub-headers", [])],
                "generated_headers": {
                    str(path): str(content)
                    for path, content in (test.get("generated-headers") or {}).items()
                },
                "source_files": [str(item) for item in test.get("source-files", [])],
                "compile_flags": [str(item) for item in test.get("compile-flags", [])],
                "source_root_env": source_env,
                "source_root_module": str(test.get("source-root-module", "")),
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "object-symbol-fixture":
            repo = test.get("repo", patch["module"])
            cwd = self._project_path(repo)
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            cc = str(test.get("cc", os.environ.get("CC", "cc")))
            source_file = str(test["source-file"])
            display_parts = [
                quote(cc),
                "-c",
                *[quote(str(flag)) for flag in test.get("compile-flags", [])],
            ]
            for include_dir in test.get("fixture-include-dirs", []):
                display_parts.extend(["-I", quote(str(include_dir))])
            for include_dir in test.get("include-dirs", []):
                display_parts.extend(["-I", quote(str(include_dir))])
            display_parts.extend([quote(source_file), "-o", "<temp>/<variant>.o", "&&", "nm", "-u", "<temp>/<variant>.o"])
            if any(
                check.get("present-defined-symbols") or check.get("absent-defined-symbols")
                for check in test.get("symbol-checks", [])
            ):
                display_parts.extend(["&&", "nm", "-g", "<temp>/<variant>.o"])
            display = f"cd {quote(repo)} && {' '.join(display_parts)}"
            return {
                "key": (
                    f"object-symbol-fixture:{repo}:{source_file}:"
                    f"{repr(test.get('compile-flags', []))}:"
                    f"{repr(test.get('include-dirs', []))}:"
                    f"{repr(test.get('fixture-include-dirs', []))}:"
                    f"{repr(test.get('symbol-checks', []))}"
                ),
                "display": display,
                "cwd": cwd,
                "args": None,
                "shell": False,
                "env": env,
                "object_symbol_fixture": True,
                "cc": cc,
                "source_file": source_file,
                "include_dirs": [str(item) for item in test.get("include-dirs", [])],
                "fixture_include_dirs": [
                    str(item) for item in test.get("fixture-include-dirs", [])
                ],
                "compile_flags": [str(item) for item in test.get("compile-flags", [])],
                "symbol_checks": [
                    {
                        "name": str(check.get("name", f"check-{index}")),
                        "compile_flags": [str(item) for item in check.get("compile-flags", [])],
                        "present_undefined_symbols": [
                            str(item) for item in check.get("present-undefined-symbols", [])
                        ],
                        "absent_undefined_symbols": [
                            str(item) for item in check.get("absent-undefined-symbols", [])
                        ],
                        "present_defined_symbols": [
                            str(item) for item in check.get("present-defined-symbols", [])
                        ],
                        "absent_defined_symbols": [
                            str(item) for item in check.get("absent-defined-symbols", [])
                        ],
                    }
                    for index, check in enumerate(test.get("symbol-checks", []))
                ],
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "source-build-fixture":
            repo = test.get("repo", patch["module"])
            script = test["script"]
            cwd = self._project_path(repo)
            script_path = cwd / script
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            build_commands = [str(item) for item in test.get("build-commands", [])]
            run_commands = [str(item) for item in test.get("run-commands", [])]
            display_steps = [
                "<archive-source>",
                *build_commands,
                *run_commands,
            ]
            display = f"cd {quote(repo)} && " + " && ".join(display_steps)
            return {
                "key": f"source-build-fixture:{repo}:{script}",
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "repo": repo,
                "script": script,
                "args": None,
                "shell": False,
                "env": env,
                "source_build_fixture": True,
                "build_commands": build_commands,
                "run_commands": run_commands,
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "source-script-fixture":
            repo = test.get("repo", patch["module"])
            cwd = self._project_path(repo)
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            source_script = str(test["source-script"])
            cases = [
                {
                    "name": str(case.get("name", f"case-{index}")),
                    "args": [str(arg) for arg in case.get("args", [])],
                    "stdout": None if case.get("stdout") is None else str(case.get("stdout")),
                    "returncode": int(case.get("returncode", 0)),
                }
                for index, case in enumerate(test.get("cases", []))
            ]
            display = (
                f"cd {quote(repo)} && "
                f"<source-script-fixture> {quote(source_script)} "
                f"({len(cases)} case(s))"
            )
            return {
                "key": f"source-script-fixture:{repo}:{source_script}:{repr(cases)}",
                "display": display,
                "cwd": cwd,
                "args": None,
                "shell": False,
                "env": env,
                "source_script_fixture": True,
                "source_script": source_script,
                "cases": cases,
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "cmake-configure-fixture":
            repo = test.get("repo", patch["module"])
            cwd = self._project_path(repo)
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            configure_args = [str(arg) for arg in test.get("configure-args", [])]
            fake_tools = {
                str(name): {
                    "stdout": str(spec.get("stdout", "")),
                    "stderr": str(spec.get("stderr", "")),
                    "returncode": int(spec.get("returncode", 0)),
                    "log_args": bool(spec.get("log-args", False)),
                }
                for name, spec in (test.get("fake-tools") or {}).items()
            }
            display = (
                f"cd {quote(repo)} && <cmake-configure-fixture> "
                f"cmake -S <source> -B <temp>/build "
                f"{shell_join(configure_args)}"
            )
            return {
                "key": (
                    f"cmake-configure-fixture:{repo}:"
                    f"{repr(configure_args)}:{repr(fake_tools)}"
                ),
                "display": display,
                "cwd": cwd,
                "args": None,
                "shell": False,
                "env": env,
                "cmake_configure_fixture": True,
                "configure_args": configure_args,
                "fake_tools": fake_tools,
                "marker_files": [
                    {
                        "path": str(marker["path"]),
                        "content": str(marker.get("content", "")),
                    }
                    for marker in test.get("marker-files", [])
                ],
                "expect": test.get("expect", {}),
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "darling-cmake-target-fixture":
            repo = test.get("repo", patch["module"])
            cwd = self._project_path(repo)
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            target = str(test["target"])
            source_dir = str(test.get("source-dir", "source"))
            cmake_args = [str(arg) for arg in test.get("cmake-args", [])]
            build_args = [str(arg) for arg in test.get("build-args", [])]
            ctest_label = test.get("ctest-label")
            run_binary = str(test.get("run-binary", f"{source_dir}/{target}"))
            diag = self._resolved_diag(test)
            if ctest_label:
                ctest_step = (
                    f"ctest --test-dir <temp>/build --output-on-failure -L "
                    f"{quote(str(ctest_label))}"
                )
                final_step = (
                    f"<darling-debug-runner> run -- {ctest_step}"
                    if diag != "bare"
                    else ctest_step
                )
            else:
                final_step = f"<temp>/build/{quote(run_binary)}"
            display = (
                f"cd {quote(repo)} && <darling-cmake-target-fixture> "
                f"cmake -S <superproject> -B <temp>/build "
                f"{shell_join(cmake_args)} && "
                f"cmake --build <temp>/build --target {quote(target)} "
                f"{shell_join(build_args)} && "
                f"{final_step}"
            )
            return {
                "key": (
                    f"darling-cmake-target-fixture:{repo}:{target}:"
                    f"{source_dir}:{run_binary}:{ctest_label}:"
                    f"{repr(test.get('fixture-files', []))}:"
                    f"{repr(cmake_args)}:{repr(build_args)}:"
                    f"{repr(test.get('required-compile-options', []))}"
                ),
                "display": display,
                "cwd": cwd,
                "args": None,
                "shell": False,
                "env": env,
                "darling_cmake_target_fixture": True,
                "target": target,
                "source_dir": source_dir,
                "run_binary": run_binary,
                "ctest_label": str(ctest_label) if ctest_label else None,
                "fixture_files": [str(item) for item in test.get("fixture-files", [])],
                "cmake_args": cmake_args,
                "build_args": build_args,
                "fallback_executable_sources": [
                    str(item) for item in test.get("fallback-executable-sources", [])
                ],
                "fallback_include_dirs": [
                    str(item) for item in test.get("fallback-include-dirs", [])
                ],
                "fallback_link_libraries": [
                    str(item) for item in test.get("fallback-link-libraries", ["crypto44"])
                ],
                "required_compile_options": [
                    {
                        "source": str(check["source"]),
                        "options": [str(item) for item in check.get("options", [])],
                    }
                    for check in test.get("required-compile-options", [])
                ],
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": diag,
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "guest-c-fixture":
            repo = test.get("repo", patch["module"])
            script = test["script"]
            cwd = self._project_path(repo)
            script_path = cwd / script
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            resources = set(test.get("requires", []))
            resources.add("darling-prefix")
            name = test.get("name", Path(script).stem)
            guest_cc = str(
                test.get(
                    "guest-cc",
                    os.environ.get(
                        "DARLING_GUEST_CC",
                        "/Library/Developer/CommandLineTools/usr/bin/clang",
                    ),
                )
            )
            guest_cflags = str(
                test.get(
                    "guest-cflags",
                    os.environ.get(
                        "DARLING_GUEST_CFLAGS",
                        "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
                    ),
                )
            )
            compile_flags = [str(item) for item in test.get("compile-flags", [])]
            link_flags = [str(item) for item in test.get("link-flags", [])]
            run_args = [str(item) for item in test.get("run-args", [])]
            guest_env_vars = {
                str(k): str(v) for k, v in test.get("guest-env-vars", {}).items()
            }
            host_trace_oracle = bool(test.get("host-trace-oracle", False))
            host_stat_deltas = list(test.get("host-stat-deltas", []))
            ok_marker = test.get("ok-marker")
            if not ok_marker and not host_trace_oracle:
                self.die(f"{patch['path']}: guest-c-fixture needs ok-marker")
            dserver_path = self._project_path("darling/src/external/darlingserver")
            host_stat_tool = "darling-stat"
            if dserver_path is not None:
                host_stat_tool = str(dserver_path / "tools/darling-stat")
            display = (
                f"cd {quote(repo)} && <upload> {quote(script)} && "
                f"darling shell {quote(guest_cc)} {guest_cflags} "
                f"{shell_join(compile_flags)} -o /tmp/{quote(name)} /tmp/{quote(name)}.c "
                f"{shell_join(link_flags)} && darling shell /tmp/{quote(name)} "
                f"{shell_join(run_args)}"
            )
            return {
                "key": f"guest-c-fixture:{repo}:{script}:{repr(host_stat_deltas)}",
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "repo": repo,
                "script": script,
                "args": None,
                "shell": False,
                "runner": "guest-c-fixture",
                "env": env,
                "guest_c_fixture": True,
                "guest_cc": guest_cc,
                "guest_cflags": guest_cflags,
                "guest_prelude": str(test.get("guest-prelude", "")),
                "guest_env_vars": guest_env_vars,
                "compile_flags": compile_flags,
                "link_flags": link_flags,
                "run_args": run_args,
                "ok_marker": str(ok_marker or ""),
                "host_trace_files": list(test.get("host-trace-files", [])),
                "host_temp_files": list(test.get("host-temp-files", [])),
                "host_stat_deltas": host_stat_deltas,
                "host_stat_tool": host_stat_tool,
                "eunion_template_files": list(test.get("eunion-template-files", [])),
                "eunion_template_symlinks": list(test.get("eunion-template-symlinks", [])),
                "eunion_upper_files": list(test.get("eunion-upper-files", [])),
                "eunion_cleanup_dirs": list(test.get("eunion-cleanup-dirs", [])),
                "eunion_verify_template_files_after": bool(
                    test.get("eunion-verify-template-files-after", False)
                ),
                "host_trace_oracle": host_trace_oracle,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": sorted(resources),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": name,
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }
        if runner == "guest-command-fixture":
            repo = test.get("repo", patch["module"])
            cwd = self._project_path(repo)
            env = None
            if test.get("env-vars"):
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in test["env-vars"].items()})
            resources = set(test.get("requires", []))
            resources.add("darling-prefix")
            guest_command = str(test["guest-command"])
            guest_env_vars = {
                str(k): str(v) for k, v in test.get("guest-env-vars", {}).items()
            }
            expect = test.get("expect", {})
            display = f"cd {quote(repo)} && darling shell /bin/bash --login -c {quote(guest_command)}"
            return {
                "key": (
                    f"guest-command-fixture:{repo}:{guest_command}:"
                    f"{repr(test.get('guest-env-vars', {}))}:"
                    f"{repr(test.get('dcc-cache', {}))}:"
                    f"{repr(expect)}"
                ),
                "display": display,
                "cwd": cwd,
                "args": None,
                "shell": False,
                "runner": "guest-command-fixture",
                "env": env,
                "guest_command_fixture": True,
                "guest_command": guest_command,
                "guest_env_vars": guest_env_vars,
                "expect": expect,
                "dcc_cache": test.get("dcc-cache"),
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": sorted(resources),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }

        self.die(f"{patch['path']}: unsupported test runner {runner!r}")

    def _run_metadata_tests(self, tests, list_only: bool, unknown: list[str]) -> int:
        if unknown:
            self.die("metadata command tests do not accept raw ctest passthrough arguments")
        self._prune_stale_west_temp_worktrees()
        rc = 0
        seen_invocations: set[str] = set()
        for patch, test in tests:
            name = test.get("name", "-")
            env = test.get("env", "-")
            diag = self._resolved_diag(test)
            kind = test.get("kind", "-")
            red = "red" if test.get("red") else "non-red"
            invocation = self._test_invocation(patch, test)
            self.inf(
                f"{patch['path']}: {name} [{red}, env:{env}, diag:{diag}, kind:{kind}]"
            )
            self.inf(f"  {self._display_invocation(invocation)}")
            if list_only:
                continue
            script_path = invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                self.die(f"{patch['path']}: test script not found: {script_path}")
            missing_env = self._missing_requirements(invocation)
            if missing_env:
                self.die(
                    f"{patch['path']}: missing required environment for {test.get('name', '-')}: "
                    f"{', '.join(missing_env)}"
                )
            if invocation["key"] in seen_invocations:
                self.inf(f"  skipped duplicate invocation already run")
                continue
            seen_invocations.add(invocation["key"])
            with self._required_profile_context(patch, invocation):
                proof = test.get("red-proof")
                if (
                    isinstance(proof, dict)
                    and proof.get("mode") == "guest-runtime-deploy"
                ):
                    result_rc = self._run_guest_runtime_deploy_green(patch, proof, invocation)
                else:
                    exec_env = self._execution_env(invocation)
                    with self._resource_context(invocation, exec_env) as resource_env:
                        result_rc = self._run_invocation(invocation, env=resource_env)
            if result_rc:
                rc = result_rc
        return rc

    def _metadata_needs_prefix(self, tests) -> bool:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            resources = set(invocation.get("requires_resources", []))
            if resources & {"darling-prefix", "darling-eunion-prefix"}:
                return True
        return False

    def _prune_stale_west_temp_worktrees(self) -> None:
        projects = getattr(self.manifest, "projects", [])
        repos = [
            Path(project.abspath)
            for project in projects
            if getattr(project, "name", None) != "manifest"
        ]
        for path in prune_stale_west_temp_worktrees(repos):
            self.inf(f"  pruned stale west temp worktree metadata: {path}")

    def _metadata_needs_profile_worktree(self, tests) -> bool:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            required = invocation.get("requires_profile")
            if required and not self._profile_is_applied(required):
                return True
            script_path = invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                return True
        return False

    def _display_ctest_label(self, label: str) -> str:
        build = self._testkit_dir() / "build"
        return ctest_label_display(build, label)

    def _ensure_ctest_build(self) -> Path:
        build = getattr(self, "_ctest_build", None)
        if build is not None:
            return build
        build = self._configure_and_build(self._testkit_dir(), self._executor)
        self._ctest_build = build
        return build

    def _ctest_label_args(self, invocation) -> list[str]:
        return ctest_label_args(self._ensure_ctest_build(), invocation["ctest_label"])

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
    ) -> list[dict]:
        """Return lifecycle groups for exactly the CTest-selected cases."""

        discovery = run_bounded(
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
        selections: list[dict] = []
        for test in payload.get("tests", []):
            name = test.get("name")
            labels: list[str] = []
            for property_data in test.get("properties", []):
                if property_data.get("name") != "LABELS":
                    continue
                labels.extend(
                    label
                    for label in property_data.get("value", [])
                    if isinstance(label, str)
                )
            selections.append(
                {
                    "name": name,
                    "darling": "env:darling" in labels,
                    "profiles": [
                        label.removeprefix("runtime-profile:")
                        for label in labels
                        if label.startswith("runtime-profile:")
                        and label.removeprefix("runtime-profile:")
                    ],
                }
            )
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
    def _runtime_profile_deployment_context(
        self,
        profiles: list[str],
        *,
        label_prefix: str,
        retain_deployment: bool,
    ):
        """Materialize, build, and deploy a declared runtime provider.

        CTest uses the default transactional form and restores the prefix after
        each selected group.  Explicit prefix bootstrap uses the same provider
        plan but commits its deployment only after its caller's guest smoke
        succeeds.  Keeping both paths here prevents bootstrap from becoming a
        second, ad-hoc build/deploy implementation.
        """

        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{label_prefix} runtime profile requires a Darling prefix")
        try:
            definition = compose_ctest_runtime_profiles(
                self._ctest_runtime_profile_definitions(), profiles
            )
        except ValueError as error:
            self.die(f"invalid CTest runtime profile selection: {error}")
        assert definition is not None
        profile_name = definition["name"]
        source_profile = definition["source-profile"]
        proof = {
            "source-modules": definition["source-modules"],
            "runtime-artifacts": definition["runtime-artifacts"],
            "cmake-defines": definition.get("cmake-defines", {}),
        }
        launcher_env = {
            key: str(value)
            for key, value in definition.get("launcher-env", {}).items()
        }
        anchor = {
            "path": f"{label_prefix} runtime profile {profile_name}",
            "module": definition["source-module"],
        }
        previous_profile = getattr(self, "_active_profile", None)
        self._require_runtime_scratch_space(f"{label_prefix} profile {profile_name}")
        self._preflight_runtime_profile_stack(
            source_profile, f"{label_prefix} profile {profile_name}"
        )
        scratch = tempfile.mkdtemp(prefix=f"west-runtime-{profile_name}-")
        keep_on_failure = False
        self._active_profile = source_profile
        try:
            self.inf(f"{label_prefix} runtime profile: {profile_name} ({source_profile})")
            with self._guest_runtime_source_forest(anchor, proof, omit_patch=False) as source_root:
                build_root = self._runtime_red_build_artifacts(
                    source_root,
                    proof,
                    Path(prefix_text),
                    Path(scratch),
                    label=f"{label_prefix} {profile_name}",
                )
                with self._runtime_red_deployed_artifacts(
                    proof,
                    build_root,
                    Path(prefix_text),
                    label=f"{label_prefix} {profile_name}",
                    restore_deployment=not retain_deployment,
                ):
                    runtime_env = os.environ.copy()
                    runtime_env.update(self._darling_prefix_env(prefix_text))
                    runtime_launcher = Path(prefix_text) / "bin" / "darling"
                    if not runtime_launcher.is_file():
                        self.die(
                            f"{label_prefix} runtime profile {profile_name} did not deploy "
                            f"a launcher at {runtime_launcher}"
                        )
                    runtime_env["DARLING"] = str(runtime_launcher)
                    runtime_env["DARLING_LAUNCHER"] = str(runtime_launcher)
                    if definition.get("bootstrap") == "rootless-no-mount":
                        boot_trace = Path(prefix_text) / ".west-rootless-boot.log"
                        guest_boot_trace = (
                            Path(prefix_text)
                            / "private/var/tmp/.west-rootless-boot.log"
                        )
                        guest_fd_trace = (
                            Path(prefix_text) / ".west-rootless-guest-fd.log"
                        )
                        boot_trace.unlink(missing_ok=True)
                        guest_boot_trace.unlink(missing_ok=True)
                        guest_fd_trace.unlink(missing_ok=True)
                        runtime_env["DARLING_HOST_BOOT_TRACE"] = str(boot_trace)
                        runtime_env["DARLING_GUEST_BOOT_TRACE"] = str(guest_fd_trace)
                        if getattr(self, "_bootstrap_syscall_trace", None) is not None:
                            server_trace = (
                                Path(prefix_text)
                                / "private/var/log/dserver-rpc-trace.log"
                            )
                            server_trace.parent.mkdir(parents=True, exist_ok=True)
                            server_trace.unlink(missing_ok=True)
                            runtime_env["DSERVER_TEST_TRACE_FILE"] = str(server_trace)
                    runtime_env.update(launcher_env)
                    yield RuntimeProfileDeployment(
                        name=profile_name,
                        prefix=Path(prefix_text),
                        build_root=build_root,
                        env=runtime_env,
                    )
        except BaseException:
            keep_on_failure = True
            self.err(f"preserving failed {label_prefix} runtime scratch for inspection: {scratch}")
            raise
        finally:
            self._active_profile = previous_profile
            if not keep_on_failure:
                shutil.rmtree(scratch, ignore_errors=True)

    def _bad_revision(self, patch) -> str:
        if patch.get("source-base"):
            return patch["source-base"]
        source_commit = patch.get("source-commit")
        if not source_commit:
            self.die(f"{patch['path']}: source-base proof needs source-base or source-commit")
        return f"{source_commit}^"

    def _wrapped_args(self, invocation) -> list[str]:
        if invocation.get("ctest_label"):
            return self._ctest_label_args(invocation)
        if invocation["shell"]:
            return ["/bin/bash", "-lc", invocation["args"]]
        return [str(arg) for arg in invocation["args"]]

    def _debug_runner_args(self, invocation, *, display_only: bool = False) -> list[str]:
        diag = invocation.get("diag", "bare")
        if diag == "bare":
            return self._wrapped_args(invocation)
        executor = getattr(self, "_executor", None)
        if not executor:
            if display_only:
                executor = "<darling-debug-runner>"
            else:
                self.die(
                    f"{invocation['name']}: diag:{diag} requires darling-debug-runner. "
                    "Build the west project with `cargo build --release` in "
                    "`darling-debug-runner`, install it on PATH, or pass --executor."
                )
        name = f"west-test-{invocation['name']}"
        args = [
            executor,
            "run",
            "--name",
            name,
            "--bundle-root",
            str(getattr(self, "_bundle_root", "~/work/darling-debug")),
            "--timeout-seconds",
            str(invocation.get("debug_timeout_seconds", invocation.get("timeout_seconds", 600))),
        ]
        if diag == "forensic":
            args.extend(["--capture-gdb", "--capture-tree"])
        args.append("--")
        args.extend(self._wrapped_args(invocation))
        return args

    def _debug_bundle_root(self) -> Path:
        return Path(os.path.expanduser(str(getattr(self, "_bundle_root", "~/work/darling-debug"))))

    def _latest_debug_bundle(self, invocation, *, since: float) -> Path | None:
        root = self._debug_bundle_root()
        if not root.is_dir():
            return None
        suffix = f"west-test-{invocation['name']}"
        candidates = [
            path
            for path in root.iterdir()
            if path.is_dir()
            and path.name.endswith(suffix)
            and path.stat().st_mtime >= since - 1
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _debug_bundle_output(self, bundle: Path) -> str:
        parts = []
        for name in ("stdout.log", "stderr.log", "exit-status.txt"):
            path = bundle / name
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
        return "".join(parts)

    def _display_invocation(self, invocation) -> str:
        if invocation.get("darling_cmake_target_fixture"):
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

    def _run_invocation(self, invocation, env=None) -> int:
        return dispatch_fixture_runner(
            invocation,
            env,
            runners=(
                ("guest_c_fixture", self._run_guest_c_fixture),
                ("guest_command_fixture", self._run_guest_command_fixture),
                ("c_fixture", self._run_c_fixture),
                ("object_symbol_fixture", self._run_object_symbol_fixture),
                ("source_build_fixture", self._run_source_build_fixture),
                ("source_script_fixture", self._run_source_script_fixture),
                ("cmake_configure_fixture", self._run_cmake_configure_fixture),
                ("darling_cmake_target_fixture", self._run_darling_cmake_target_fixture),
            ),
            fallback=self._run_command_invocation,
        )

    def _run_darling_cmake_target_fixture(self, invocation, env=None) -> int:
        rc = run_darling_cmake_target_fixture(
            invocation,
            env=env,
            executor=getattr(self, "_executor", None),
            bundle_root=getattr(self, "_bundle_root", "~/work/darling-debug"),
            inf=self.inf,
            err=self.err,
            die=self.die,
        )
        if rc:
            self._record_failure_phase(invocation, "configure")
        return rc

    def _run_command_invocation(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        result = run_bounded(
            self._debug_runner_args(invocation),
            cwd=invocation["cwd"],
            env=run_env,
            timeout_seconds=int(invocation.get("timeout_seconds", 600)) + 15,
        )
        if result.timed_out:
            self.err(
                f"{invocation['name']}: timed out after "
                f"{invocation.get('timeout_seconds', 600)}s"
            )
        rc = result.returncode
        if rc:
            self._record_failure_phase(
                invocation,
                "ctest" if invocation.get("ctest_label") else "script",
            )
            return rc
        return self._check_host_traces(invocation, run_env)

    def _record_failure_phase(self, invocation, phase: str) -> None:
        """Record and report the runner stage that made an invocation fail."""

        self._failure_phase = phase
        print(f"WEST_TEST_FAILURE_PHASE={phase}", file=sys.stderr)

    def _run_invocation_captured(self, invocation, env=None) -> InvocationResult:
        """Run an invocation and return its output and structured failure phase."""
        prior_phase = getattr(self, "_failure_phase", None)
        self._failure_phase = None
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output:
            stdout_fd = os.dup(1)
            stderr_fd = os.dup(2)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(output.fileno(), 1)
                os.dup2(output.fileno(), 2)
                rc = self._run_invocation(invocation, env=env)
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                os.dup2(stdout_fd, 1)
                os.dup2(stderr_fd, 2)
                os.close(stdout_fd)
                os.close(stderr_fd)
            output.seek(0)
            result = InvocationResult(rc, output.read(), self._failure_phase)
        self._failure_phase = prior_phase
        return result

    @contextmanager
    def _host_trace_context(self, invocation, env):
        traces = invocation.get("host_trace_files", [])
        if not traces:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-trace-files need DPREFIX")
        trace_env = dict(env or os.environ.copy())
        trace_paths = []
        for index, trace in enumerate(traces):
            if not isinstance(trace, dict):
                self.die(f"{invocation['name']}: host-trace-files entries must be mappings")
            env_name = str(trace.get("env", ""))
            rel_path = str(trace.get("prefix-relative-path", ""))
            if not env_name or not rel_path:
                self.die(
                    f"{invocation['name']}: host-trace-files[{index}] needs env "
                    "and prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                self.die(
                    f"{invocation['name']}: host-trace-files[{index}] path must "
                    "be prefix-relative"
                )
            trace_path = Path(prefix) / rel_path
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                trace_path.unlink()
            except FileNotFoundError:
                pass
            trace_env[env_name] = str(trace_path)
            trace_paths.append(trace_path)
        invocation["_host_trace_paths"] = trace_paths
        yield trace_env

    @contextmanager
    def _host_temp_context(self, invocation, env):
        temp_files = invocation.get("host_temp_files", [])
        if not temp_files:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-temp-files need DPREFIX")
        temp_env = dict(env or os.environ.copy())
        temp_paths = []
        for index, temp_file in enumerate(temp_files):
            if not isinstance(temp_file, dict):
                self.die(f"{invocation['name']}: host-temp-files entries must be mappings")
            env_name = str(temp_file.get("env", ""))
            rel_path = str(temp_file.get("prefix-relative-path", ""))
            if not env_name or not rel_path:
                self.die(
                    f"{invocation['name']}: host-temp-files[{index}] needs env "
                    "and prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                self.die(
                    f"{invocation['name']}: host-temp-files[{index}] path must "
                    "be prefix-relative"
                )
            temp_path = Path(prefix) / rel_path
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            if "contents" in temp_file and temp_file["contents"] is not None:
                temp_path.write_text(str(temp_file["contents"]))
            temp_env[env_name] = str(temp_path)
            temp_paths.append(temp_path)
        invocation["_host_temp_paths"] = temp_paths
        try:
            yield temp_env
        finally:
            for temp_path in temp_paths:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _check_host_traces(self, invocation, env) -> int:
        traces = invocation.get("host_trace_files", [])
        if not traces:
            return 0
        for index, trace in enumerate(traces):
            trace_path = invocation.get("_host_trace_paths", [])[index]
            if not trace_path.is_file():
                self.err(f"  missing host trace file: {trace_path}")
                return 1
            content = trace_path.read_text(errors="replace")
            print(content, end="" if content.endswith("\n") else "\n")
            for expected in [str(item) for item in trace.get("contains", [])]:
                if expected not in content:
                    self.err(f"  missing host trace content in {trace_path}: {expected}")
                    return 1
        return 0

    def _run_c_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: c-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_module = invocation.get("source_root_module")
        if source_root_module:
            source_root = self._project_path(source_root_module)
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        with tempfile.TemporaryDirectory(prefix=f"west-c-fixture-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            stub_root = tempdir / "include"
            for header in invocation.get("stub_headers", []):
                header_path = stub_root / header
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_text("\n")
            for header, content in invocation.get("generated_headers", {}).items():
                header_path = stub_root / header
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_text(content)
            binary = tempdir / Path(invocation["script_path"]).stem
            args = [
                invocation.get("cc", "cc"),
                *invocation.get("compile_flags", []),
                "-I",
                str(stub_root),
            ]
            for include_dir in invocation.get("fixture_include_dirs", []):
                include_path = Path(include_dir)
                if not include_path.is_absolute():
                    include_path = invocation["cwd"] / include_path
                args.extend(["-I", str(include_path)])
            for include_dir in invocation.get("include_dirs", []):
                include_path = Path(include_dir)
                if not include_path.is_absolute():
                    include_path = source_root / include_path
                args.extend(["-I", str(include_path)])
            for source_file in invocation.get("source_files", []):
                source_path = Path(source_file)
                if not source_path.is_absolute():
                    source_path = source_root / source_path
                args.append(str(source_path))
            args.extend([str(invocation["script_path"]), "-o", str(binary)])
            compile_result = run_bounded(
                args,
                cwd=invocation["cwd"],
                env=run_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if compile_result.timed_out:
                self.err(f"{invocation['name']}: compile timed out")
            if compile_result.returncode:
                self._record_failure_phase(invocation, "compile")
                return compile_result.returncode
            run_result = run_bounded(
                [str(binary)],
                cwd=invocation["cwd"],
                env=run_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if run_result.timed_out:
                self.err(f"{invocation['name']}: test binary timed out")
            if run_result.returncode:
                self._record_failure_phase(invocation, "run")
            return run_result.returncode

    def _run_object_symbol_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: object-symbol-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        source_path = Path(invocation["source_file"])
        if not source_path.is_absolute():
            source_path = source_root / source_path
        with tempfile.TemporaryDirectory(prefix=f"west-object-symbol-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            for check in invocation.get("symbol_checks", []):
                object_path = tempdir / f"{check['name']}.o"
                args = [
                    invocation.get("cc", "cc"),
                    "-c",
                    *invocation.get("compile_flags", []),
                    *check.get("compile_flags", []),
                ]
                for include_dir in invocation.get("fixture_include_dirs", []):
                    include_path = Path(include_dir)
                    if not include_path.is_absolute():
                        include_path = invocation["cwd"] / include_path
                    args.extend(["-I", str(include_path)])
                for include_dir in invocation.get("include_dirs", []):
                    include_path = Path(include_dir)
                    if not include_path.is_absolute():
                        include_path = source_root / include_path
                    args.extend(["-I", str(include_path)])
                args.extend([str(source_path), "-o", str(object_path)])
                compile_result = run_bounded(
                    args,
                    cwd=invocation["cwd"],
                    env=run_env,
                    timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                )
                if compile_result.timed_out:
                    self.err(f"{invocation['name']}:{check['name']}: compile timed out")
                if compile_result.returncode:
                    self._record_failure_phase(invocation, "compile")
                    return compile_result.returncode
                nm = run_bounded(
                    ["nm", "-u", str(object_path)],
                    cwd=invocation["cwd"],
                    env=run_env,
                    timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                    capture_output=True,
                )
                if nm.timed_out:
                    self.err(f"{invocation['name']}:{check['name']}: nm timed out")
                if nm.returncode:
                    sys.stderr.write(nm.stdout)
                    sys.stderr.write(nm.stderr)
                    self._record_failure_phase(invocation, "inspect")
                    return nm.returncode
                symbols = {
                    line.split()[-1]
                    for line in nm.stdout.splitlines()
                    if line.split()
                }
                for symbol in check.get("present_undefined_symbols", []):
                    if symbol not in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: missing undefined symbol {symbol}")
                        self._record_failure_phase(invocation, "inspect")
                        return 1
                for symbol in check.get("absent_undefined_symbols", []):
                    if symbol in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: unexpected undefined symbol {symbol}")
                        self._record_failure_phase(invocation, "inspect")
                        return 1
                if check.get("present_defined_symbols") or check.get("absent_defined_symbols"):
                    defined_nm = run_bounded(
                        ["nm", "-g", str(object_path)],
                        cwd=invocation["cwd"],
                        env=run_env,
                        timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                        capture_output=True,
                    )
                    if defined_nm.timed_out:
                        self.err(f"{invocation['name']}:{check['name']}: nm timed out")
                    if defined_nm.returncode:
                        sys.stderr.write(defined_nm.stdout)
                        sys.stderr.write(defined_nm.stderr)
                        self._record_failure_phase(invocation, "inspect")
                        return defined_nm.returncode
                    defined_symbols = set()
                    for line in defined_nm.stdout.splitlines():
                        parts = line.split()
                        if not parts:
                            continue
                        if parts[0] == "U":
                            continue
                        if len(parts) >= 3:
                            defined_symbols.add(parts[-1])
                    for symbol in check.get("present_defined_symbols", []):
                        if symbol not in defined_symbols:
                            self.err(f"{invocation['name']}:{check['name']}: missing defined symbol {symbol}")
                            self._record_failure_phase(invocation, "inspect")
                            return 1
                    for symbol in check.get("absent_defined_symbols", []):
                        if symbol in defined_symbols:
                            self.err(f"{invocation['name']}:{check['name']}: unexpected defined symbol {symbol}")
                            return 1
        return 0

    def _run_cmake_configure_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: cmake-configure-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = os.environ.copy()
        else:
            run_env = dict(run_env)
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        if not (source_root / "CMakeLists.txt").is_file():
            self.err(f"{invocation['name']}: CMakeLists.txt not found: {source_root}")
            self._record_failure_phase(invocation, "setup")
            return 1

        with tempfile.TemporaryDirectory(prefix=f"west-cmake-configure-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            bin_dir = tempdir / "bin"
            build_dir = tempdir / "build"
            bin_dir.mkdir()
            build_dir.mkdir()
            for marker in invocation.get("marker_files", []):
                marker_path = source_root / marker["path"]
                if marker_path.exists():
                    continue
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                marker_path.write_text(marker.get("content", ""))
            for name, spec in invocation.get("fake_tools", {}).items():
                tool_path = bin_dir / name
                log_line = f"printf '%s\\n' \"$*\" >> {quote(str(tempdir / f'{name}.log'))}\n" if spec.get("log_args") else ""
                tool_path.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    f"{log_line}"
                    f"printf '%s' {quote(spec.get('stdout', ''))}\n"
                    f"printf '%s' {quote(spec.get('stderr', ''))} >&2\n"
                    f"exit {int(spec.get('returncode', 0))}\n"
                )
                tool_path.chmod(0o755)
            child_env = dict(run_env)
            child_env["PATH"] = f"{bin_dir}:{child_env.get('PATH', '')}"
            args = [
                "cmake",
                "-S",
                str(source_root),
                "-B",
                str(build_dir),
                *invocation.get("configure_args", []),
            ]
            result = run_bounded(
                args,
                cwd=invocation["cwd"],
                env=child_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                capture_output=True,
            )
            if result.timed_out:
                self.err(f"{invocation['name']}: cmake configure timed out")
                self._record_failure_phase(invocation, "configure")
                return 124
            output = result.stdout + result.stderr
            def write_output_tail() -> None:
                lines = output.splitlines()
                tail = "\n".join(lines[-120:])
                if tail:
                    sys.stderr.write(tail + "\n")
            expect = invocation.get("expect") or {}
            rc_mode = expect.get("returncode", 0)
            if rc_mode == "nonzero":
                if result.returncode == 0:
                    self.err(f"{invocation['name']}: cmake configure succeeded unexpectedly")
                    self._record_failure_phase(invocation, "configure")
                    return 1
            elif result.returncode != int(rc_mode):
                write_output_tail()
                self._record_failure_phase(invocation, "configure")
                self.err(
                    f"{invocation['name']}: cmake configure rc {result.returncode}, "
                    f"want {rc_mode}"
                )
                return 1
            for needle in expect.get("output-contains", []):
                if str(needle) not in output:
                    write_output_tail()
                    self.err(f"{invocation['name']}: cmake output missing {needle!r}")
                    self._record_failure_phase(invocation, "configure")
                    return 1
            for tool, checks in (expect.get("tool-args-contains") or {}).items():
                log_path = tempdir / f"{tool}.log"
                log = log_path.read_text() if log_path.is_file() else ""
                for needle in checks:
                    if str(needle) not in log:
                        write_output_tail()
                        self.err(f"{invocation['name']}: {tool} args missing {needle!r}")
                        return 1
            return 0

    def _run_source_script_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: source-script-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        script_path = source_root / invocation["source_script"]
        if not script_path.is_file():
            self.err(f"{invocation['name']}: source script not found: {script_path}")
            self._record_failure_phase(invocation, "setup")
            return 1

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        for case in invocation.get("cases", []):
            if os.access(script_path, os.X_OK):
                args = [str(script_path), *case.get("args", [])]
            else:
                args = ["sh", str(script_path), *case.get("args", [])]
            result = run_bounded(
                args,
                cwd=source_root,
                env=run_env,
                timeout_seconds=timeout_seconds,
                capture_output=True,
            )
            if result.timed_out:
                self.err(
                    f"{invocation['name']}:{case['name']}: timed out after "
                    f"{timeout_seconds}s"
                )
                self._record_failure_phase(invocation, "script")
                return 124
            expected_rc = case.get("returncode", 0)
            if result.returncode != expected_rc:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
                self._record_failure_phase(invocation, "script")
                self.err(
                    f"{invocation['name']}:{case['name']}: rc {result.returncode}, "
                    f"want {expected_rc}"
                )
                return 1
            expected_stdout = case.get("stdout")
            if expected_stdout is not None and result.stdout != expected_stdout:
                sys.stderr.write(result.stderr)
                self._record_failure_phase(invocation, "script")
                self.err(
                    f"{invocation['name']}:{case['name']}: stdout "
                    f"{result.stdout!r}, want {expected_stdout!r}"
                )
                return 1
        return 0

    @contextmanager
    def _host_stat_context(self, invocation, env):
        deltas = invocation.get("host_stat_deltas", [])
        if not deltas:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-stat-deltas need DPREFIX")
        tool = Path(str(invocation.get("host_stat_tool", "darling-stat")))
        if not tool.is_absolute():
            resolved = shutil.which(str(tool))
            if resolved:
                tool = Path(resolved)
        if not tool.is_file() or not os.access(tool, os.X_OK):
            self.die(f"{invocation['name']}: missing darling stat tool: {tool}")
        invocation["_host_stat_tool"] = str(tool)
        yield env

    def _run_source_build_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: source-build-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = os.environ.copy()
        else:
            run_env = dict(run_env)
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        relative_script = invocation["script_path"].relative_to(invocation["cwd"])
        fixture_path = invocation["script_path"]
        if not fixture_path.is_file():
            source_fixture = source_root / relative_script
            if source_fixture.is_file():
                fixture_path = source_fixture
            else:
                self.die(f"{invocation['name']}: fixture not found: {fixture_path}")
        with tempfile.TemporaryDirectory(prefix=f"west-source-build-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            build_root = tempdir / "source"
            rc = archive_source_to(
                source_root,
                build_root,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if rc:
                return rc
            build_fixture = build_root / relative_script
            if not build_fixture.is_file():
                build_fixture.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fixture_path, build_fixture)
            child_env = dict(run_env)
            child_env["WEST_TEST_TMP"] = str(tempdir)
            child_env["WEST_TEST_SOURCE_ROOT"] = str(build_root)
            timeout_seconds = int(invocation.get("timeout_seconds", 600))
            for command in [*invocation.get("build_commands", []), *invocation.get("run_commands", [])]:
                self.inf(f"  source-build-fixture: {command}")
                result = run_bounded(
                    ["/bin/bash", "-lc", command],
                    cwd=build_root,
                    env=child_env,
                    timeout_seconds=timeout_seconds,
                )
                if result.timed_out:
                    self.err(
                        f"  source-build-fixture timed out after "
                        f"{timeout_seconds}s: {command}"
                    )
                    self._record_failure_phase(
                        invocation,
                        "build" if command in invocation.get("build_commands", []) else "run",
                    )
                    return 124
                if result.returncode:
                    self._record_failure_phase(
                        invocation,
                        "build" if command in invocation.get("build_commands", []) else "run",
                    )
                    return result.returncode
            return 0

    def _run_guest_c_fixture(self, invocation, env=None) -> int:
        return run_guest_c_fixture(self, invocation, env)

    def _run_guest_command_fixture(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = self._execution_env(invocation)
        if not run_env:
            run_env = os.environ.copy()
        return run_guest_command_fixture(
            invocation,
            env=run_env,
            prefix=getattr(self, "_prefix", None),
            resolve_launcher=self._resolve_darling_launcher,
            die=self.die,
            err=self.err,
            record_failure_phase=self._record_failure_phase,
        )

    def _execution_env(self, invocation) -> dict[str, str] | None:
        env = invocation.get("env")
        resources = set(invocation.get("requires_resources", []))
        needs_prefix = bool(resources & {"darling-prefix", "darling-eunion-prefix"})
        source_env = invocation.get("source_env")
        if not needs_prefix and not source_env:
            return env
        merged = os.environ.copy()
        if env:
            merged.update(env)
        if source_env and not merged.get(source_env):
            source_root = self._project_path(invocation.get("source_module"))
            if source_root is not None:
                merged[source_env] = str(source_root)
        if not needs_prefix:
            return merged
        prefix = getattr(self, "_prefix", None)
        if not prefix:
            return merged
        merged.update(self._darling_prefix_env(prefix))
        if "darling-eunion-prefix" in resources:
            merged["DARLING_EUNION"] = "1"
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            merged["DARLING"] = launcher
            merged["DARLING_LAUNCHER"] = launcher
        return merged

    def _missing_requirements(self, invocation) -> list[str]:
        resources = set(invocation.get("requires_resources", []))
        missing = [
            env_name
            for env_name in invocation.get("requires_env", [])
            if not os.environ.get(env_name)
        ]
        if (
            resources & {"darling-prefix", "darling-eunion-prefix"}
            and not getattr(self, "_prefix", None)
        ):
            missing.append("darling-prefix (--prefix, --prefix-profile, or DPREFIX)")
        if resources & {"darling-prefix", "darling-eunion-prefix"}:
            prefix = getattr(self, "_prefix", None)
            launcher = self._resolve_darling_launcher(prefix)
            if not launcher:
                missing.append(
                    "darling-launcher (DARLING, DARLING_LAUNCHER, "
                    "prefix/bin/darling, or ~/work/darling-prefix/bin/darling)"
                )
            if prefix:
                missing.extend(self._prefix_boot_prerequisite_problems(Path(prefix)))
                if invocation.get("guest_c_fixture"):
                    missing.extend(
                        self._guest_c_fixture_prerequisite_problems(
                            Path(prefix),
                            invocation.get("guest_cc", ""),
                            invocation.get("guest_cflags", ""),
                        )
                    )
                if "darling-eunion-prefix" in resources:
                    missing.extend(self._eunion_prefix_prerequisite_problems(Path(prefix)))
        return missing

    def _prefix_boot_prerequisite_problems(self, prefix: Path) -> list[str]:
        return prefix_boot_prerequisite_problems(prefix)

    def _guest_c_fixture_prerequisite_problems(
        self,
        prefix: Path,
        guest_cc: str,
        guest_cflags: str,
    ) -> list[str]:
        return guest_c_fixture_prerequisite_problems(prefix, guest_cc, guest_cflags)

    def _eunion_prefix_prerequisite_problems(self, prefix: Path) -> list[str]:
        return eunion_prefix_prerequisite_problems(prefix)

    @contextmanager
    def _resource_context(self, invocation, env):
        with resource_context(self, invocation, env) as resource_env:
            yield resource_env

    @contextmanager
    def _eunion_prefix_context(self, invocation, env):
        resources = set(invocation.get("requires_resources", []))
        if "darling-eunion-prefix" not in resources:
            yield
            return

        prefix_text = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs DPREFIX")
        prefix = Path(prefix_text)
        marker = prefix / ".union-work"
        created_marker = False
        created_template_files: list[Path] = []
        created_template_symlinks: list[Path] = []
        created_template_dirs: list[Path] = []
        created_upper_files: list[Path] = []
        created_upper_dirs: list[Path] = []
        cleanup_dirs: list[tuple[Path, Path]] = []
        template_assertions: list[dict] = []
        probe_dirs: list[tuple[Path, Path]] = []
        blocked_upper_files: list[Path] = []

        def cleanup_fixture_state() -> None:
            for path in reversed(created_upper_files):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_template_files):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_template_symlinks):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_upper_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for path in reversed(created_template_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for upper_dir, lower_dir in reversed(cleanup_dirs):
                shutil.rmtree(upper_dir, ignore_errors=True)
                shutil.rmtree(lower_dir, ignore_errors=True)
            for upper_dir, lower_dir in reversed(probe_dirs):
                shutil.rmtree(upper_dir, ignore_errors=True)
                shutil.rmtree(lower_dir, ignore_errors=True)
            if created_marker:
                try:
                    marker.rmdir()
                except OSError:
                    self.err(f"{invocation['name']}: preserving non-empty E-UNION marker {marker}")

        try:
            if marker.exists() and not marker.is_dir():
                self.die(f"{invocation['name']}: E-UNION marker is not a directory: {marker}")
            if not marker.exists():
                marker.mkdir(parents=True, mode=0o700)
                created_marker = True

            for index, guest_path in enumerate(invocation.get("eunion_cleanup_dirs", [])):
                guest_path = str(guest_path)
                if (
                    not guest_path.startswith("/private/var/tmp/west-")
                    or ".." in Path(guest_path).parts
                ):
                    self.die(
                        f"{invocation['name']}: eunion-cleanup-dirs[{index}] needs "
                        "an absolute /private/var/tmp/west-* guest path without '..'"
                    )
                rel = Path(guest_path.lstrip("/"))
                cleanup_dirs.append((prefix / rel, prefix / "libexec/darling" / rel))

            self._shutdown_runtime_prefix(prefix)
            self._boot_eunion_runtime_prefix(invocation, env, prefix)

            for index, spec in enumerate(invocation.get("eunion_template_files", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-template-files entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-template-files[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                rel = Path(guest_path.lstrip("/"))
                upper_path = prefix / rel
                lower_path = prefix / "libexec/darling" / rel
                if upper_path.exists():
                    blocked_upper_files.append(upper_path)
                    continue
                created_template_dirs.extend(
                    self._mkdirs_for_fixture(lower_path.parent, prefix / "libexec/darling")
                )
                if not lower_path.exists():
                    lower_path.write_text(str(spec.get("contents", "")))
                    created_template_files.append(lower_path)
                if "mode" in spec:
                    lower_path.chmod(self._parse_file_mode(invocation, "eunion-template-files", index, spec["mode"]))
                for name, value in (spec.get("xattrs") or {}).items():
                    try:
                        os.setxattr(lower_path, str(name).encode(), str(value).encode())
                    except OSError as exc:
                        self.die(
                            f"{invocation['name']}: failed to set E-UNION template xattr "
                            f"{name} on {lower_path}: {exc}"
                        )
                template_assertions.append(
                    {
                        "path": lower_path,
                        "contents": str(spec.get("contents", "")),
                        "mode": spec.get("mode"),
                        "xattrs": {str(k): str(v) for k, v in (spec.get("xattrs") or {}).items()},
                        "absent_xattrs": [str(item) for item in spec.get("absent-xattrs", [])],
                    }
                )
            if blocked_upper_files:
                self.die(
                    f"{invocation['name']}: E-UNION lower fixture would be shadowed by "
                    f"upper file(s): {', '.join(str(path) for path in blocked_upper_files)}"
                )

            for index, spec in enumerate(invocation.get("eunion_template_symlinks", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-template-symlinks entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                target = str(spec.get("target", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-template-symlinks[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                if not target or target.startswith("/") or ".." in Path(target).parts:
                    self.die(
                        f"{invocation['name']}: eunion-template-symlinks[{index}] needs "
                        "a non-empty relative target without '..'"
                    )
                rel = Path(guest_path.lstrip("/"))
                upper_path = prefix / rel
                lower_path = prefix / "libexec/darling" / rel
                if upper_path.exists() or upper_path.is_symlink():
                    self.die(f"{invocation['name']}: E-UNION symlink fixture shadowed by upper path: {upper_path}")
                created_template_dirs.extend(
                    self._mkdirs_for_fixture(lower_path.parent, prefix / "libexec/darling")
                )
                if lower_path.exists() or lower_path.is_symlink():
                    self.die(f"{invocation['name']}: E-UNION symlink fixture already exists: {lower_path}")
                lower_path.symlink_to(target)
                created_template_symlinks.append(lower_path)

            for index, spec in enumerate(invocation.get("eunion_upper_files", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-upper-files entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-upper-files[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                upper_path = prefix / guest_path.lstrip("/")
                if upper_path.exists():
                    self.die(f"{invocation['name']}: E-UNION upper fixture already exists: {upper_path}")
                created_upper_dirs.extend(self._mkdirs_for_fixture(upper_path.parent, prefix))
                upper_path.write_text(str(spec.get("contents", "")))
                created_upper_files.append(upper_path)
            self._verify_eunion_runtime_prefix(invocation, env, prefix, probe_dirs)
        except BaseException:
            self._shutdown_runtime_prefix(prefix)
            cleanup_fixture_state()
            raise

        try:
            yield
        finally:
            self._shutdown_runtime_prefix(prefix)
            try:
                if invocation.get("eunion_verify_template_files_after"):
                    self._verify_eunion_template_files_after(invocation, template_assertions)
            finally:
                cleanup_fixture_state()

    @contextmanager
    def _dcc_cache_context(self, invocation, env):
        spec = invocation.get("dcc_cache")
        if spec is None:
            yield
            return
        if not isinstance(spec, dict):
            self.die(f"{invocation['name']}: dcc-cache must be a mapping")
        prefix_text = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{invocation['name']}: dcc-cache needs DPREFIX")
        prefix = Path(prefix_text)
        source_module = str(spec.get("source-module", "darling/src/external/darlingserver"))
        tools_dir_name = str(spec.get("tools-dir", "tools/closure-cache"))
        builder_name = str(spec.get("builder", "dcc5-builder.c"))
        list_name = str(spec.get("closure-list", "closure-list.txt"))
        source_ref = spec.get("source-ref")
        install_root_mode = str(spec.get("install-root", "guest-visible"))
        guest_env_name = str(spec.get("env", "DARLING_DYLD_DCC2_PATH"))
        enable_env_name = str(spec.get("enable-env", "DARLING_DYLD_DCC2"))
        if source_ref is not None and (not isinstance(source_ref, str) or not source_ref):
            self.die(f"{invocation['name']}: dcc-cache source-ref must be a non-empty string")
        if install_root_mode not in {"guest-visible", "base", "prefix"}:
            self.die(
                f"{invocation['name']}: dcc-cache install-root must be "
                "guest-visible, base, or prefix"
            )
        if not guest_env_name or not guest_env_name.isidentifier():
            self.die(f"{invocation['name']}: dcc-cache env must be a shell variable name")
        if enable_env_name and not enable_env_name.isidentifier():
            self.die(f"{invocation['name']}: dcc-cache enable-env must be a shell variable name")

        old_guest_env = dict(invocation.get("guest_env_vars", {}))
        work_rel = Path("private/var/tmp") / f"west-dcc-cache-{os.getpid()}-{int(time.time() * 1000)}"
        host_dir = prefix / "libexec/darling" / work_rel
        guest_dir = "/" + str(work_rel)
        install_root = self._dcc_install_root(prefix, env, install_root_mode)
        with tempfile.TemporaryDirectory(prefix=f"west-dcc-cache-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            source_root = self._dcc_cache_source_root(
                invocation,
                env,
                source_module,
                source_ref,
                tools_dir_name,
                tempdir,
            )
            tools_dir = source_root / tools_dir_name
            builder_source = tools_dir / builder_name
            closure_list = tools_dir / list_name
            if not builder_source.is_file():
                self.die(f"{invocation['name']}: DCC builder not found: {builder_source}")
            if not closure_list.is_file():
                self.die(f"{invocation['name']}: DCC closure list not found: {closure_list}")
            builder = tempdir / Path(builder_name).stem
            host_cache = host_dir / "system-closure.dcc6"
            guest_cache = f"{guest_dir}/system-closure.dcc6"
            try:
                host_dir.mkdir(parents=True, exist_ok=False)
                compile_args = ["gcc", "-O2", "-o", str(builder), str(builder_source)]
                self.inf(f"  DCC cache builder: {' '.join(quote(str(arg)) for arg in compile_args)}")
                self._run_dcc_cache_command(invocation, "compile", compile_args, tools_dir)
                build_args = [
                    str(builder),
                    str(install_root),
                    str(closure_list),
                    str(host_cache),
                ]
                self.inf(
                    f"  DCC cache build: {host_cache} "
                    f"(install-root={install_root})"
                )
                self._run_dcc_cache_command(invocation, "build", build_args, tools_dir)
                if spec.get("stale"):
                    self._make_dcc_cache_stale(host_cache)
                guest_env = dict(old_guest_env)
                if enable_env_name:
                    guest_env[enable_env_name] = str(spec.get("enable-value", "1"))
                guest_env[guest_env_name] = guest_cache
                if spec.get("soft"):
                    guest_env["DARLING_DYLD_DCC2_SOFT"] = "1"
                invocation["guest_env_vars"] = guest_env
                yield
            finally:
                invocation["guest_env_vars"] = old_guest_env
                shutil.rmtree(host_dir, ignore_errors=True)

    def _run_dcc_cache_command(self, invocation, stage: str, args, cwd: Path) -> None:
        """Run bounded host-side DCC preparation and preserve its failure tail."""

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        result = run_bounded(
            args,
            cwd=cwd,
            env=None,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        if result.timed_out:
            self.err(
                f"{invocation['name']}: DCC cache {stage} timed out after "
                f"{timeout_seconds}s"
            )
        self._dump_command_tail(f"DCC cache {stage}", result)
        self._record_failure_phase(invocation, "setup")
        self.die(f"{invocation['name']}: DCC cache {stage} failed with rc {result.returncode}")

    def _dcc_cache_source_root(
        self,
        invocation,
        env,
        source_module: str,
        source_ref: str | None,
        tools_dir_name: str,
        tempdir: Path,
    ) -> Path:
        if source_ref:
            source_root = tempdir / "source"
            source_root.mkdir()
            repo = self._project_path(source_module)
            if repo is None:
                self.die(f"{invocation['name']}: unknown dcc-cache source module {source_module}")
            result = archive_git_tree_to(
                repo,
                source_root,
                revision=source_ref,
                paths=[tools_dir_name],
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if result.returncode:
                streams = (result.stdout, result.stderr)
                detail = "".join(
                    stream.decode(errors="replace") if isinstance(stream, bytes) else stream
                    for stream in streams
                    if stream
                )
                if detail:
                    sys.stderr.write(detail)
                self.die(
                    f"{invocation['name']}: failed to materialize DCC cache "
                    f"tools from {source_module}@{source_ref}"
                )
            return source_root

        runtime_source_root = (env or {}).get("WEST_RUNTIME_SOURCE_ROOT")
        if runtime_source_root:
            module_path = Path(source_module)
            try:
                rel = module_path.relative_to("darling")
            except ValueError:
                rel = module_path
            return Path(runtime_source_root) / rel
        return self._project_path(source_module)

    def _dcc_install_root(self, prefix: Path, env, mode: str) -> Path:
        if mode == "base":
            return prefix / "libexec/darling"
        if mode == "prefix":
            return prefix
        run_env = dict(getattr(self, "_prefix_env", {}))
        if env:
            run_env.update(env)
        if run_env.get("DARLING_NOOVERLAYFS") == "1":
            return prefix
        return prefix / "libexec/darling"

    def _make_dcc_cache_stale(self, cache_path: Path) -> None:
        """Mutate the first image's recorded src_size so reader validation rejects it."""
        header_size = 424
        first_image_src_size_offset = header_size + 256 + 16 + 8 + 8
        with cache_path.open("r+b") as handle:
            handle.seek(first_image_src_size_offset)
            raw = handle.read(8)
            if len(raw) != 8:
                self.die(f"DCC cache too small to stale-mutate: {cache_path}")
            value = int.from_bytes(raw, "little", signed=False)
            handle.seek(first_image_src_size_offset)
            handle.write((value + 1).to_bytes(8, "little", signed=False))

    def _boot_eunion_runtime_prefix(self, invocation, env, prefix: Path) -> None:
        launcher = (
            (env or {}).get("DARLING_LAUNCHER")
            or (env or {}).get("DARLING")
            or self._resolve_darling_launcher(str(prefix))
        )
        if not launcher:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs a Darling launcher")

        child_env = dict(env or os.environ.copy())
        child_env.update(self._darling_prefix_env(prefix))
        result = run_guest_shell(
            str(launcher),
            prefix,
            ":",
            cwd=Path.cwd(),
            env=child_env,
            timeout_seconds=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            self.die(
                f"{invocation['name']}: failed to boot Darling E-UNION prefix "
                f"before fixture setup (rc={result.returncode})"
            )

    def _parse_file_mode(self, invocation, field: str, index: int, value) -> int:
        try:
            if isinstance(value, int):
                return value
            return int(str(value), 8)
        except (TypeError, ValueError):
            self.die(f"{invocation['name']}: {field}[{index}] has invalid mode: {value!r}")

    def _verify_eunion_template_files_after(self, invocation, assertions) -> None:
        for assertion in assertions:
            path = assertion["path"]
            expected = assertion["contents"]
            try:
                got = path.read_text()
            except FileNotFoundError:
                self.die(f"{invocation['name']}: E-UNION template fixture was removed: {path}")
            if got != expected:
                self.die(f"{invocation['name']}: E-UNION template fixture was modified: {path}")
            if assertion.get("mode") is not None:
                expected_mode = self._parse_file_mode(invocation, "eunion-template-files", 0, assertion["mode"])
                actual_mode = path.stat().st_mode & 0o7777
                if actual_mode != expected_mode:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture mode changed: "
                        f"{path} got {actual_mode:o} want {expected_mode:o}"
                    )
            for name, expected_value in assertion.get("xattrs", {}).items():
                try:
                    got_value = os.getxattr(path, name.encode()).decode()
                except OSError as exc:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr missing "
                        f"{name} on {path}: {exc}"
                    )
                if got_value != expected_value:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr changed: "
                        f"{path} {name} got {got_value!r} want {expected_value!r}"
                    )
            for name in assertion.get("absent_xattrs", []):
                try:
                    got_value = os.getxattr(path, name.encode())
                except OSError:
                    continue
                self.die(
                    f"{invocation['name']}: E-UNION template fixture xattr was added: "
                    f"{path} {name}={got_value!r}"
                )

    def _verify_eunion_runtime_prefix(self, invocation, env, prefix: Path, probe_dirs) -> None:
        launcher = (
            (env or {}).get("DARLING_LAUNCHER")
            or (env or {}).get("DARLING")
            or self._resolve_darling_launcher(str(prefix))
        )
        if not launcher:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs a Darling launcher")

        name = f"west-eunion-probe-{os.getpid()}-{int(time.time() * 1000)}"
        guest_dir = f"/private/var/tmp/{name}"
        upper_dir = prefix / "private/var/tmp" / name
        lower_dir = prefix / "libexec/darling/private/var/tmp" / name
        upper_dir.mkdir(parents=True)
        lower_dir.mkdir(parents=True)
        probe_dirs.append((upper_dir, lower_dir))
        (lower_dir / "lower.txt").write_text("LOWER\n")
        (lower_dir / "shadow.txt").write_text("LOWER_SHADOW\n")
        (upper_dir / "upper.txt").write_text("UPPER\n")
        (upper_dir / "shadow.txt").write_text("UPPER_SHADOW\n")

        child_env = dict(env or os.environ.copy())
        child_env.update(self._darling_prefix_env(prefix))
        script = (
            "set -e; "
            f"test \"$(cat {quote(guest_dir + '/lower.txt')})\" = LOWER; "
            f"test \"$(cat {quote(guest_dir + '/upper.txt')})\" = UPPER; "
            f"test \"$(cat {quote(guest_dir + '/shadow.txt')})\" = UPPER_SHADOW"
        )
        output_path = lower_dir / "probe-output.txt"
        with output_path.open("w+") as output:
            result = run_guest_shell(
                str(launcher),
                prefix,
                script,
                cwd=Path.cwd(),
                env=child_env,
                timeout_seconds=15,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        if result.returncode != 0:
            output = output_path.read_text(errors="replace").strip()
            if output:
                self.err(output)
            self.die(
                f"{invocation['name']}: Darling prefix is not running as an "
                "active E-UNION upper-over-template root; upper/lower probe failed"
            )

    def _mkdirs_for_fixture(self, target: Path, root: Path) -> list[Path]:
        root = root.resolve()
        to_create = []
        current = target
        while current != root and root in current.resolve().parents:
            if current.exists():
                break
            to_create.append(current)
            current = current.parent
        for path in reversed(to_create):
            path.mkdir()
        return list(reversed(to_create))

    def _check_requires_profile(self, patch, invocation) -> None:
        required = invocation.get("requires_profile")
        if not required:
            return
        if required in getattr(self, "_worktree_materialized_profiles", set()):
            return
        if self._profile_is_applied(required):
            return
        if getattr(self, "_materialize_profile", False):
            return
        self.die(
            f"{patch['path']}: test requires materialized patch profile {required!r}; "
            f"current checkout is not fully on integration/{required}. "
            f"Run `west patch apply --profile {required}` first, or pass "
            "`west test --materialize-profile` to switch temporarily."
        )

    @contextmanager
    def _required_profile_context(self, patch, invocation):
        required = invocation.get("requires_profile")
        if required in getattr(self, "_worktree_materialized_profiles", set()):
            yield
            return
        if not required or self._profile_is_applied(required):
            yield
            return
        if not getattr(self, "_materialize_profile", False):
            self._check_requires_profile(patch, invocation)
            yield
            return
        self.inf(f"{patch['path']}: temporarily materializing profile {required!r}")
        with self._profile_checkout(required):
            yield

    @contextmanager
    def _selected_profile_context(self, profile: str, *, list_only: bool = False):
        if list_only or not getattr(self, "_materialize_profile", False):
            yield
            return
        if self._profile_is_applied(profile):
            yield
            return
        self.inf(f"temporarily materializing selected profile {profile!r} in worktrees")
        active = set(getattr(self, "_worktree_materialized_profiles", set()))
        active.add(profile)
        previous = getattr(self, "_worktree_materialized_profiles", set())
        self._worktree_materialized_profiles = active
        try:
            with self._profile_worktree_checkout(profile):
                yield
        finally:
            self._worktree_materialized_profiles = previous

    def _reject_guest_source_base_red_proof(self, patch) -> None:
        self.die(
            f"{patch['path']}: guest-c-fixture cannot use source-base RED proof "
            "because it would run against the already deployed Darling prefix. "
            "Use a GREEN-only guest gate or add an isolated bad/fixed deploy runner."
        )

    def _display_guest_runtime_deploy_plan(self, proof) -> str:
        return describe_runtime_deploy_plan(proof)

    def _red_source_patch_path(self, path: str) -> Path:
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            self.die(f"red-proof source-patches path must be workspace-relative: {path}")
        result = Path(self.manifest.repo_abspath) / rel
        if not result.is_file():
            self.die(f"red-proof source patch not found: {result}")
        return result

    def _project_manifest_path(self, ref: str) -> Path:
        for project in self.manifest.projects:
            if ref in {project.name, project.path}:
                return Path(project.path)
        path = Path(ref)
        if path.exists():
            workspace_root = Path(self.topdir).parent
            try:
                return path.resolve().relative_to(workspace_root)
            except ValueError:
                pass
        self.die(f"unknown West project or path: {ref}")

    def _remove_path_for_materialize(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _has_symlink_parent(self, path: Path, stop: Path) -> bool:
        current = path.parent
        while current != stop and stop in current.parents:
            if current.is_symlink():
                return True
            current = current.parent
        return False

    def _apply_profile_module_patches(
        self,
        profile: str,
        module: str,
        target: Path,
        *,
        skip_patch_paths: set[str] | None = None,
    ) -> None:
        skips = skip_patch_paths or set()
        for stacked in self._profile_stack(profile):
            data = self._load_profile(stacked)
            profile_dir = self._profile_path(stacked).parent
            for patch in data.get("patches", []):
                if patch.get("module") != module:
                    continue
                if patch.get("path") in skips:
                    self.inf(f"  skip {stacked}/{patch['path']} for current-minus-patch")
                    continue
                patch_file = profile_dir / patch["path"]
                if self._patch_already_in_history(target, patch_file, patch):
                    self.inf(f"  skip {stacked}/{patch['path']} already in {module}")
                    continue
                self.inf(f"  apply {stacked}/{patch['path']} -> {module}")
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "gc.auto=0",
                        "-c",
                        "maintenance.auto=false",
                        "am",
                        "--3way",
                        "--committer-date-is-author-date",
                        str(patch_file),
                    ],
                    cwd=target,
                    check=True,
                )

    def _patch_ids_from_text(self, patch_text: str, *, cwd: Path | None = None) -> set[str]:
        result = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=patch_text,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            return set()
        return {
            line.split()[0]
            for line in result.stdout.splitlines()
            if line.split()
        }

    def _history_patch_ids(self, repo: Path) -> set[str]:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        cache = getattr(self, "_history_patch_id_cache", None)
        if cache is None:
            cache = {}
            self._history_patch_id_cache = cache
        key = (str(repo), head)
        if key in cache:
            return cache[key]
        history = subprocess.run(
            [
                "git",
                "log",
                "--patch",
                "--no-ext-diff",
                "--no-color",
                "--format=email",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        )
        ids = self._patch_ids_from_text(history.stdout, cwd=repo)
        cache[key] = ids
        return ids

    def _commit_is_ancestor(self, repo: Path, commit: str) -> bool:
        if not commit:
            return False
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if exists.returncode:
            return False
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0

    def _patch_subject_from_text(self, patch_text: str) -> str | None:
        lines = patch_text.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("Subject: "):
                continue
            subject = line.removeprefix("Subject: ").strip()
            for continuation in lines[index + 1:]:
                if continuation.startswith((" ", "\t")):
                    subject = f"{subject} {continuation.strip()}".strip()
                    continue
                break
            if subject.startswith("[PATCH"):
                _, _, subject = subject.partition("]")
                subject = subject.strip()
            return subject or None
        return None

    def _history_subjects(self, repo: Path) -> set[str]:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        cache = getattr(self, "_history_subject_cache", None)
        if cache is None:
            cache = {}
            self._history_subject_cache = cache
        key = (str(repo), head)
        if key in cache:
            return cache[key]
        history = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        )
        subjects = {line.strip() for line in history.stdout.splitlines() if line.strip()}
        cache[key] = subjects
        return subjects

    def _patch_already_in_history(self, repo: Path, patch_file: Path, patch=None) -> bool:
        patch_text = patch_file.read_text(errors="replace")
        patch_ids = self._patch_ids_from_text(patch_text, cwd=repo)
        if patch_ids and patch_ids <= self._history_patch_ids(repo):
            return True
        if patch and self._commit_is_ancestor(repo, str(patch.get("source-commit", ""))):
            return True
        if not patch or not patch.get("source-commit"):
            return False
        subject = self._patch_subject_from_text(patch_text)
        return bool(subject and subject in self._history_subjects(repo))

    def _current_minus_skip_patch_paths(self, patch, proof) -> set[str]:
        return {
            str(patch["path"]),
            *[str(path) for path in proof.get("current-minus-skip-patches", [])],
        }

    def _active_runtime_profile(self, patch) -> str:
        profile = getattr(self, "_active_profile", None)
        if not profile:
            self.die(f"{patch['path']}: current-minus-patch needs an active profile")
        return profile

    def _apply_current_minus_profile(self, patch, proof, module: str, target: Path) -> None:
        profile = self._active_runtime_profile(patch)
        self._apply_profile_module_patches(
            profile,
            module,
            target,
            skip_patch_paths=self._current_minus_skip_patch_paths(patch, proof),
        )

    def _apply_full_runtime_profile(self, patch, module: str, target: Path) -> None:
        self._apply_profile_module_patches(
            self._active_runtime_profile(patch),
            module,
            target,
        )

    @contextmanager
    def _source_base_green_source_tree(self, patch, module: str):
        """Materialize the fixed/profile source tree for source-base proofs."""
        cache = getattr(self, "_source_base_green_cache", None)
        key = (getattr(self, "_active_profile", None), module)
        if cache is not None and key in cache:
            yield cache[key]
            return
        if cache is not None:
            tree = getattr(self, "_source_base_green_stack").enter_context(
                self._materialize_source_base_green_tree(patch, module)
            )
            cache[key] = tree
            yield tree
            return
        with self._materialize_source_base_green_tree(patch, module) as tree:
            yield tree

    @contextmanager
    def _materialize_source_base_green_tree(self, patch, module: str):
        """Create one disposable fixed source tree for a source-base proof."""
        profile = getattr(self, "_active_profile", None)
        if not profile:
            yield None
            return
        if self._profile_is_applied(profile):
            yield None
            return

        module_repo = self._project_path(module)
        revision = self._manifest_revision(module)
        temp = tempfile.mkdtemp(prefix="west-green-proof-source-")
        target = Path(temp) / "source"
        keep_on_failure = False
        try:
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(target), revision],
                cwd=module_repo,
                check=True,
            )
            self._apply_profile_module_patches(profile, module, target)
            yield target
        except BaseException:
            keep_on_failure = True
            self.err(f"preserving failed GREEN source tree for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                error = remove_temporary_worktree(module_repo, target)
                if error:
                    self.die(f"failed to remove GREEN source worktree: {error}")
                shutil.rmtree(temp, ignore_errors=True)

    def _apply_red_source_patches(self, proof, module_label: str, target: Path) -> None:
        for source_patch in proof.get("source-patches", []):
            patch_path = self._red_source_patch_path(str(source_patch))
            rel = patch_path.relative_to(self.manifest.repo_abspath)
            self.inf(f"  apply RED source patch {rel} -> {module_label}")
            subprocess.run(
                ["git", "apply", "--3way", str(patch_path)],
                cwd=target,
                check=True,
            )

    def _guest_runtime_source_modules(self, patch, proof) -> set[Path]:
        modules = {self._project_manifest_path(patch["module"])}
        source_modules = proof.get("source-modules", [])
        if not isinstance(source_modules, list):
            self.die(f"{patch['path']}: red-proof.source-modules must be a list of West project paths")
        for module in source_modules:
            if not isinstance(module, str) or not module:
                self.die(f"{patch['path']}: red-proof.source-modules must be a list of West project paths")
            modules.add(self._project_manifest_path(module))
        return modules

    def _guest_runtime_source_revision(
        self,
        patch,
        project_path: Path,
        patch_module_path: Path,
        omit_patch: bool,
        bad_revision,
    ) -> tuple[str, bool]:
        module = str(project_path)
        if not omit_patch:
            return self._manifest_revision(module), False

        if project_path == patch_module_path:
            return bad_revision, False

        return self._manifest_revision(module), False

    @contextmanager
    def _guest_runtime_source_forest(self, patch, proof, *, omit_patch: bool):
        """Create a temporary Darling source forest for a runtime build.

        The top-level Darling tree is a detached worktree. Nested West projects
        are symlinked to the current checkout unless the proof needs them
        materialized. When omit_patch is true, the target patch and explicit
        current-minus skips are removed to build the RED runtime. Otherwise the
        full active profile is materialized to build the GREEN runtime. This
        keeps live checkouts and the caller's prefix stable while giving CMake
        one coherent source root.
        """
        projects_by_path = {
            Path(project.path): Path(project.abspath)
            for project in self.manifest.projects
            if project.name != "manifest"
        }
        darling_repo = projects_by_path.get(Path("darling"))
        if darling_repo is None:
            self.die("guest-runtime-deploy needs a West project at path 'darling'")
        patch_module_path = self._project_manifest_path(patch["module"])
        materialized_modules = self._guest_runtime_source_modules(patch, proof)
        patch_module_is_darling_root = patch_module_path == Path("darling")
        current_minus_patch = proof.get("bad-profile") == "current-minus-patch"
        if omit_patch and not current_minus_patch:
            self.die(f"{patch['path']}: only current-minus-patch runtime proofs are supported")
        bad_revision = self._bad_revision(patch) if omit_patch else None
        added: list[tuple[Path, Path]] = []
        temp = tempfile.mkdtemp(prefix="west-red-proof-source-")
        yielded = False
        keep_on_failure = False
        try:
            root = Path(temp)
            source_root = root / "darling"
            if omit_patch and patch_module_is_darling_root:
                darling_ref = bad_revision
            else:
                darling_ref = self._manifest_revision("darling")
            bad_text = "current-minus-patch" if omit_patch else "profile-current"
            self.inf(
                f"  runtime source forest: {patch_module_path}={bad_text} under {source_root}"
            )
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(source_root),
                    darling_ref,
                ],
                cwd=darling_repo,
                check=True,
            )
            added.append((darling_repo, source_root))
            if patch_module_is_darling_root or Path("darling") in materialized_modules:
                if omit_patch:
                    self._apply_current_minus_profile(patch, proof, "darling", source_root)
                else:
                    self._apply_full_runtime_profile(patch, "darling", source_root)
                self._apply_red_source_patches(proof, "darling", source_root)
            for project_path, repo in sorted(
                projects_by_path.items(),
                key=lambda item: (len(item[0].parts), str(item[0])),
            ):
                if project_path == Path("darling"):
                    continue
                try:
                    rel = project_path.relative_to("darling")
                except ValueError:
                    continue
                target = source_root / rel
                if self._has_symlink_parent(target, source_root):
                    if project_path == patch_module_path:
                        self.die(
                            f"{patch['path']}: cannot materialize nested patch module "
                            f"{project_path} inside a symlinked parent source tree"
                        )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    self._remove_path_for_materialize(target)
                if project_path in materialized_modules:
                    module_text = str(project_path)
                    revision, uses_profile_source_commit = (
                        self._guest_runtime_source_revision(
                            patch,
                            project_path,
                            patch_module_path,
                            omit_patch,
                            bad_revision,
                        )
                    )
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "add",
                            "--quiet",
                            "--detach",
                            str(target),
                            revision,
                        ],
                        cwd=repo,
                        check=True,
                    )
                    added.append((repo, target))
                    if not uses_profile_source_commit:
                        if omit_patch:
                            self._apply_current_minus_profile(patch, proof, module_text, target)
                        else:
                            self._apply_full_runtime_profile(patch, module_text, target)
                    if omit_patch:
                        self._apply_red_source_patches(proof, module_text, target)
                else:
                    os.symlink(repo, target, target_is_directory=True)
            yielded = True
            yield source_root
        except BaseException:
            keep_on_failure = True
            suffix = " before yield" if not yielded else ""
            self.err(f"preserving failed runtime source forest{suffix} for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                for repo, target in reversed(added):
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(target)],
                        cwd=repo,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                shutil.rmtree(temp, ignore_errors=True)

    def _cmake_cache_value(self, build_dir: Path, key: str) -> str | None:
        cache = build_dir / "CMakeCache.txt"
        if not cache.exists():
            return None
        prefix = f"{key}:"
        for line in cache.read_text(errors="replace").splitlines():
            if line.startswith(prefix):
                return line.split("=", 1)[1]
        return None

    def _runtime_red_configure_args(self, proof, prefix: Path) -> list[str]:
        current_build = Path(
            os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build"))
        )
        args = ["-G", self._cmake_cache_value(current_build, "CMAKE_GENERATOR") or "Ninja"]
        # A runtime proof is a clean test build, not a clone of the developer's
        # active build. Darling's objc4 target requires a Debug configuration.
        cmake_defines = {"CMAKE_BUILD_TYPE": "Debug", **(proof.get("cmake-defines") or {})}
        active_profile = getattr(self, "_active_profile", None)
        if active_profile and "DARLING_PATCH_PROFILE" not in cmake_defines:
            cmake_defines["DARLING_PATCH_PROFILE"] = active_profile
        for key in ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"):
            value = self._cmake_cache_value(current_build, key)
            if value:
                args.append(f"-D{key}={value}")
        inherited_feature_flags = set(proof.get("inherit-cmake-cache", []))
        if "all" in inherited_feature_flags:
            inherited_feature_flags.update(
                {
                    "DARLING_RING_TRANSPORT",
                    "DSERVER_RING_TRANSPORT",
                }
            )
        for key in (
            "DARLING_COREDUMP_SANITIZE",
            "DARLING_EUNION",
            "DARLING_GUEST_RECVSPIN",
            "DARLING_RPC_SLEEP_ACCOUNT",
        ):
            value = self._cmake_cache_value(current_build, key)
            if value is not None:
                args.append(f"-D{key}={value}")
        for key in ("DARLING_RING_TRANSPORT", "DSERVER_RING_TRANSPORT"):
            if key in inherited_feature_flags:
                value = self._cmake_cache_value(current_build, key)
                if value is not None:
                    args.append(f"-D{key}={value}")
                continue
            args.append(f"-D{key}=OFF")
        for key, value in sorted(cmake_defines.items()):
            if isinstance(value, bool):
                value_text = "ON" if value else "OFF"
            elif value is None:
                value_text = ""
            else:
                value_text = str(value)
            args.append(f"-D{key}={value_text}")
        args.append(f"-DCMAKE_INSTALL_PREFIX={prefix}")
        return args

    def _runtime_red_build_artifacts(
        self,
        source_root: Path,
        proof,
        prefix: Path,
        scratch_root: Path,
        *,
        label: str = "RED",
    ) -> Path:
        targets = runtime_build_targets(proof)
        build_root = scratch_root / "build"
        timeout_seconds = int(proof.get("build-timeout-seconds", 1800))
        self.inf(f"  {label} configure: {source_root} -> {build_root}")
        configure = run_bounded(
            ["cmake", "-S", str(source_root), "-B", str(build_root), *self._runtime_red_configure_args(proof, prefix)],
            cwd=self.topdir,
            env=None,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if configure.returncode:
            self._dump_command_tail(f"{label} configure", configure)
            self.die(f"{label} configure failed with rc {configure.returncode}")
        self.inf(f"  {label} build: {', '.join(targets)}")
        build = run_bounded(
            ["ninja", "-C", str(build_root), *targets],
            cwd=self.topdir,
            env=None,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if build.returncode:
            self._dump_command_tail(f"{label} build", build)
            self.die(f"{label} build failed with rc {build.returncode}")
        return build_root

    def _dump_command_tail(self, label: str, result) -> None:
        streams = [stream for stream in (result.stdout, result.stderr) if stream]
        output = "\n".join(stream.rstrip("\n") for stream in streams)
        lines = output.splitlines()
        tail = "\n".join(lines[-200:])
        failed = [index for index, line in enumerate(lines) if line.startswith("FAILED:")]
        if failed:
            start = failed[-1]
            excerpt = "\n".join(lines[start : start + 80])
            if excerpt not in tail:
                tail = f"Actionable failure:\n{excerpt}\n\nCommand tail:\n{tail}"
        if tail:
            sys.stderr.write(tail + "\n")
        self.err(f"{label} failed with rc {result.returncode}")

    def _runtime_red_find_build_output(
        self, build_root: Path, deploy_path: str
    ) -> Path:
        name = Path(deploy_path).name
        best: tuple[float, Path] | None = None
        for path in build_root.rglob(name):
            if not path.is_file() or "CMakeFiles" in path.parts:
                continue
            mtime = path.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, path)
        if best is None:
            self.die(f"guest-runtime-deploy built artifact not found for {deploy_path}")
        return best[1]

    def _runtime_macho_inspect(self, path: Path, flag: str) -> str:
        command = ["llvm-objdump", "--macho", flag, str(path)]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            self.die(
                "guest-runtime-deploy rootless bootstrap closure requires llvm-objdump"
            )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            self.die(
                f"guest-runtime-deploy could not inspect Mach-O {path}: "
                f"{detail or f'rc {result.returncode}'}"
            )
        return result.stdout

    def _runtime_macho_dependencies(self, path: Path) -> list[str]:
        if not is_macho_binary(path):
            return []
        return parse_macho_dylib_dependencies(
            self._runtime_macho_inspect(path, "--dylibs-used")
        )

    def _runtime_macho_dylib_providers(self, build_root: Path) -> dict[str, Path]:
        providers: dict[str, Path] = {}
        for path in build_root.rglob("*.dylib"):
            if not path.is_file() or "CMakeFiles" in path.parts:
                continue
            # These are deliberately intermediate circular-link products, not
            # deployable guest runtime libraries.
            if path.name.endswith("_firstpass.dylib") or not is_macho_binary(path):
                continue
            install_name = parse_macho_dylib_id(
                self._runtime_macho_inspect(path, "--dylib-id")
            )
            if install_name is None:
                self.die(
                    f"guest-runtime-deploy built dylib has no Mach-O install name: {path}"
                )
            existing = providers.get(install_name)
            if existing is not None and existing != path:
                self.die(
                    "guest-runtime-deploy found multiple built providers for "
                    f"{install_name}: {existing}, {path}"
                )
            providers[install_name] = path
        return providers

    def _runtime_rootless_bootstrap_closure(
        self,
        proof,
        build_root: Path,
        explicit_deployments: dict[str, Path],
    ) -> dict[str, Path]:
        resources = {
            artifact.get("resource")
            for artifact in proof.get("runtime-artifacts", [])
            if isinstance(artifact, dict)
        }
        if ROOTLESS_BOOTSTRAP_CLOSURE_RESOURCE not in resources:
            return {}
        roots = {
            "/" + deploy_path: source
            for deploy_path, source in explicit_deployments.items()
            if is_macho_binary(source)
        }
        if not roots:
            self.die(
                "guest-runtime-deploy rootless bootstrap closure has no Mach-O roots"
            )
        try:
            closure = resolve_macho_runtime_closure(
                roots,
                self._runtime_macho_dylib_providers(build_root),
                self._runtime_macho_dependencies,
            )
        except ValueError as exc:
            self.die(f"guest-runtime-deploy {exc}")
        return {
            guest_path.removeprefix("/"): source
            for guest_path, source in closure.items()
            if guest_path.removeprefix("/") not in explicit_deployments
        }

    def _runtime_red_deploy_targets(self, prefix: Path, deploy_path: str) -> list[Path]:
        try:
            return runtime_deploy_targets(prefix, deploy_path)
        except ValueError:
            self.die(f"guest-runtime-deploy deploy path must be relative: {deploy_path}")

    def _runtime_replace_file(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{dst.name}.west-deploy-",
            dir=dst.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        try:
            shutil.copy2(src, temp_path)
            os.replace(temp_path, dst)
        except Exception:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def _runtime_red_deployed_artifacts(
        self,
        proof,
        build_root: Path,
        prefix: Path,
        *,
        label: str = "RED",
        restore_deployment: bool = True,
    ):
        backups: list[tuple[Path, Path | None]] = []
        deployment_succeeded = False
        with tempfile.TemporaryDirectory(prefix="west-red-proof-deploy-") as temp:
            backup_root = Path(temp)
            if not self._shutdown_runtime_prefix(prefix):
                self.die(f"guest-runtime-deploy could not stop Darling prefix before deploy: {prefix}")
            try:
                deployments: dict[str, Path] = {}
                for artifact in proof.get("runtime-artifacts", []):
                    for deploy_path in runtime_artifact_deploy_paths(artifact):
                        deployments[deploy_path] = self._runtime_red_find_build_output(
                            build_root, deploy_path
                        )
                deployments.update(
                    self._runtime_rootless_bootstrap_closure(
                        proof, build_root, deployments
                    )
                )
                for deploy_path, src in deployments.items():
                    for dst in self._runtime_red_deploy_targets(prefix, deploy_path):
                            backup = None
                            if dst.exists():
                                backup = backup_root / str(len(backups))
                                backup.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(dst, backup)
                            backups.append((dst, backup))
                            self._runtime_replace_file(src, dst)
                            self.inf(f"  {label} deploy: {src} -> {dst}")
                yield
                deployment_succeeded = True
            finally:
                if not self._shutdown_runtime_prefix(prefix):
                    self.err(f"guest-runtime-deploy could not stop Darling prefix before restore: {prefix}")
                if restore_deployment or not deployment_succeeded:
                    for dst, backup in reversed(backups):
                        if backup is None:
                            try:
                                dst.unlink()
                            except FileNotFoundError:
                                pass
                        else:
                            self._runtime_replace_file(backup, dst)
                elif backups:
                    self.inf(f"  {label} deployment retained after successful smoke")
                self._shutdown_runtime_prefix(prefix)

    def _bootstrap_runtime_profile(self, profile_name: str) -> None:
        """Retain one declared runtime provider only after a real guest smoke."""

        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(
                "--bootstrap-runtime-profile requires --prefix, --prefix-profile, or DPREFIX "
                "(for example: --prefix-profile homebrew)"
            )
        if not profile_name:
            self.die("--bootstrap-runtime-profile needs a runtime provider name")
        definition = self._ctest_runtime_profile_definitions().get(profile_name)
        if definition is None:
            self.die(f"unknown prefix baseline runtime profile: {profile_name}")
        if definition.get("purpose") != "prefix-baseline":
            self.die(
                f"runtime profile {profile_name} is not a prefix-baseline; "
                "bootstrap only accepts declared rootless baselines"
            )
        smoke_timeout_seconds = definition["bootstrap-smoke-timeout-seconds"]
        trace_dir = getattr(self, "_bootstrap_syscall_trace", None)
        command_prefix: tuple[str, ...] = ()
        if trace_dir is not None:
            if shutil.which("strace") is None:
                self.die("--bootstrap-syscall-trace requires strace on the host")
            trace_dir.mkdir(parents=True, exist_ok=True)
            command_prefix_parts = ["strace", "-ff", "-i"]
            if getattr(self, "_bootstrap_syscall_stack", False):
                command_prefix_parts.append("-k")
            command_prefix_parts.extend(
                ("-tt", "-v", "-s", "160", "-o", str(trace_dir / "bootstrap"))
            )
            command_prefix = tuple(command_prefix_parts)
            self.inf(f"prefix bootstrap syscall trace: {trace_dir}")
        with self._prefix_resource_context(True):
            with self._runtime_profile_deployment_context(
                [profile_name], label_prefix="Prefix bootstrap", retain_deployment=True
            ) as deployment:
                doctor = run_bounded(
                    [
                        "west",
                        "darling-doctor",
                        "--prefix",
                        str(deployment.prefix),
                        "--build-dir",
                        str(deployment.build_root),
                        "--no-baseline-file",
                    ],
                    cwd=Path(self.topdir),
                    env=None,
                    timeout_seconds=60,
                    capture_output=True,
                )
                doctor_output = f"{doctor.stdout}{doctor.stderr}"
                if doctor.timed_out:
                    self.die("prefix bootstrap doctor timed out after 60s")
                if doctor.returncode != 0:
                    self.die(
                        "prefix bootstrap doctor failed "
                        f"with rc {doctor.returncode}: {doctor_output[-1000:]}"
                    )
                result = run_guest_shell(
                    deployment.env["DARLING_LAUNCHER"],
                    prefix_text,
                    "set -eu\nprintf '%s\\n' WEST_PREFIX_BOOTSTRAP_OK",
                    cwd=Path(self.topdir),
                    env=deployment.env,
                    timeout_seconds=smoke_timeout_seconds,
                    capture_output=True,
                    command_prefix=command_prefix,
                )
                if trace_dir is not None:
                    server_trace = (
                        deployment.prefix / "private/var/log/dserver-rpc-trace.log"
                    )
                    if server_trace.is_file():
                        captured_server_trace = trace_dir / "darlingserver-rpc.log"
                        shutil.copy2(server_trace, captured_server_trace)
                        self.inf(f"prefix bootstrap server trace: {captured_server_trace}")
                output = f"{result.stdout}{result.stderr}"
                trace_fault = (
                    bootstrap_trace_fatal_signal(trace_dir)
                    if trace_dir is not None
                    else None
                )
                if trace_fault is not None:
                    self.die(
                        "prefix bootstrap guest smoke crashed before its verdict: "
                        f"{trace_fault}; syscall trace: {trace_dir}"
                    )
                if result.timed_out:
                    trace_hint = f"; syscall trace: {trace_dir}" if trace_dir is not None else ""
                    progress = rootless_bootstrap_progress(deployment.prefix)
                    progress_hint = f"; progress: {progress}" if progress is not None else ""
                    self.die(
                        "prefix bootstrap guest smoke timed out after "
                        f"{smoke_timeout_seconds}s{trace_hint}{progress_hint}"
                    )
                if result.returncode != 0:
                    self.die(
                        "prefix bootstrap guest smoke failed "
                        f"with rc {result.returncode}: {output[-1000:]}"
                    )
                if "WEST_PREFIX_BOOTSTRAP_OK" not in output:
                    self.die("prefix bootstrap guest smoke returned without its verdict marker")
                self.inf(f"prefix bootstrap passed for {prefix_text}: {profile_name}")

    def _shutdown_runtime_prefix(self, prefix: Path) -> bool:
        launcher = self._resolve_darling_launcher(str(prefix))
        if launcher:
            env = os.environ.copy()
            env.update(self._darling_prefix_env(prefix))
            timeout_seconds = int(os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", "15"))
            result = shutdown_guest_prefix(
                launcher,
                prefix,
                cwd=Path.cwd(),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if result.timed_out:
                self.err(f"Darling prefix shutdown timed out for {prefix}; forcing cleanup")
        self._kill_dserver_for_prefix(prefix)
        rootless_cleanup = cleanup_rootless_prefix_processes(prefix)
        for message in rootless_cleanup.changed:
            self.inf(f"cleanup rootless Darling prefix: {message}")
        for message in rootless_cleanup.problems:
            self.err(message)
        leftovers = self._prefix_process_snapshot(prefix)
        if leftovers:
            self.err(f"leftover Darling prefix process(es) for {prefix}:")
            for entry in leftovers:
                self.err(f"  {entry}")
            return False
        if not rootless_cleanup.success:
            return False
        if not self._cleanup_prefix_mounts(prefix):
            return False
        self._remove_stale_init_pid(prefix)
        return True

    def _invocation_from_runtime_source(self, invocation, source_root: Path):
        repo = invocation.get("repo")
        script = invocation.get("script")
        if not repo or not script:
            return invocation

        repo_path = Path(repo)
        if repo_path == Path("darling"):
            runtime_cwd = source_root
        else:
            try:
                runtime_cwd = source_root / repo_path.relative_to("darling")
            except ValueError:
                return invocation

        runtime_invocation = dict(invocation)
        runtime_invocation["cwd"] = runtime_cwd
        runtime_invocation["script_path"] = runtime_cwd / script
        return runtime_invocation

    def _invocation_from_source_profile(self, invocation, source_root: Path):
        """Run a profile-owned test script from a materialized source tree."""
        script = invocation.get("script")
        if not script:
            return invocation
        profile_invocation = dict(invocation)
        profile_invocation["cwd"] = source_root
        profile_invocation["script_path"] = source_root / script
        return profile_invocation

    def _guest_runtime_red_invocation(self, patch, proof, invocation):
        red_runner = proof.get("red-runner")
        if red_runner is None:
            return invocation
        if not isinstance(red_runner, dict):
            self.die(f"{patch['path']}: red-proof.red-runner must be a mapping")
        red_test = dict(red_runner)
        red_test.setdefault("name", f"{invocation['name']}_red")
        red_test.setdefault("diag", invocation.get("diag", "bare"))
        red_test.setdefault("timeout-seconds", invocation.get("timeout_seconds", 600))
        resources = set(red_test.get("requires", []))
        inherited_resources = set(invocation.get("requires_resources", []))
        prefix_resources = inherited_resources & {"darling-prefix", "darling-eunion-prefix"}
        resources.update(prefix_resources or {"darling-prefix"})
        red_test["requires"] = sorted(resources)
        return self._test_invocation(patch, red_test)

    def _run_guest_runtime_deploy_green(self, patch, proof, invocation) -> int:
        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{patch['path']}: guest-runtime-deploy needs a Darling prefix")
        prefix = Path(prefix_text)
        self._require_runtime_scratch_space(
            f"{patch['path']}: {invocation['name']} GREEN"
        )
        self._preflight_runtime_profile_stack(
            self._active_runtime_profile(patch),
            f"{patch['path']}: {invocation['name']} GREEN",
        )
        temp = tempfile.mkdtemp(prefix="west-green-proof-runtime-")
        keep_on_failure = False
        try:
            scratch_root = Path(temp)
            with self._guest_runtime_source_forest(patch, proof, omit_patch=False) as source_root:
                build_root = self._runtime_red_build_artifacts(
                    source_root,
                    proof,
                    prefix,
                    scratch_root,
                    label="GREEN",
                )
                with self._runtime_red_deployed_artifacts(
                    proof,
                    build_root,
                    prefix,
                    label="GREEN",
                ):
                    green_env = self._execution_env(invocation)
                    if green_env is None:
                        green_env = os.environ.copy()
                    else:
                        green_env = dict(green_env)
                    green_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                    runtime_invocation = self._invocation_from_runtime_source(invocation, source_root)
                    with self._resource_context(runtime_invocation, green_env) as resource_env:
                        green_started_at = time.time()
                        green_rc = self._run_invocation(runtime_invocation, env=resource_env)
                    if green_rc != 0:
                        keep_on_failure = True
                        self.err(f"preserving failed GREEN runtime scratch for inspection: {temp}")
                        return green_rc
                    if not self._check_guest_runtime_green_success(
                        runtime_invocation,
                        since=green_started_at,
                    ):
                        keep_on_failure = True
                        self.err(f"preserving failed GREEN runtime scratch for inspection: {temp}")
                        return 1
                    return 0
        except BaseException:
            keep_on_failure = True
            self.err(f"preserving failed GREEN runtime scratch for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                shutil.rmtree(temp, ignore_errors=True)

    def _check_guest_runtime_green_success(self, invocation, *, since: float) -> bool:
        if invocation.get("host_trace_oracle"):
            return True
        ok_marker = invocation.get("ok_marker")
        if not ok_marker:
            return True
        bundle = self._latest_debug_bundle(invocation, since=since)
        if bundle is None:
            self.err(
                f"{invocation['name']}: GREEN output requested, "
                f"but no recent debug bundle was found under {self._debug_bundle_root()}"
            )
            return False
        output = self._debug_bundle_output(bundle)
        if str(ok_marker) not in output:
            self.err(f"{invocation['name']}: GREEN output missing {ok_marker!r} in {bundle}")
            return False
        if "ORACLE_RC=0" not in output:
            self.err(f"{invocation['name']}: GREEN output missing 'ORACLE_RC=0' in {bundle}")
            return False
        return True

    def _check_guest_runtime_red_failure(
        self,
        proof,
        invocation,
        *,
        since: float,
        captured_output: str | None = None,
    ) -> bool:
        contains, lacks = self._red_output_expectations(proof)
        if not contains and not lacks:
            return True

        bundle = self._latest_debug_bundle(invocation, since=since)
        if bundle is None:
            if captured_output is not None:
                return self._check_red_output_expectations(
                    proof,
                    invocation,
                    captured_output,
                    where="in captured RED output",
                )
            self.err(
                f"{invocation['name']}: RED failure output requested, "
                f"but no recent debug bundle was found under {self._debug_bundle_root()}"
            )
            return False

        output = self._debug_bundle_output(bundle)
        return self._check_red_output_expectations(
            proof,
            invocation,
            output,
            where=f"in {bundle}",
        )

    def _red_output_expectations(self, proof) -> tuple[list[str], list[str]]:
        contains = proof.get("expect-output-contains", [])
        lacks = proof.get("expect-output-lacks", [])
        if isinstance(contains, str):
            contains = [contains]
        if isinstance(lacks, str):
            lacks = [lacks]
        return [str(item) for item in contains], [str(item) for item in lacks]

    def _red_failure_phases(self, proof) -> list[str]:
        phases = proof.get("expect-failure-phase", [])
        if isinstance(phases, str):
            phases = [phases]
        return [str(phase) for phase in phases]

    def _check_red_failure_phase(self, proof, invocation, observed: str | None) -> bool:
        phases = self._red_failure_phases(proof)
        if not phases:
            return True
        if observed in phases:
            return True
        self.err(
            f"{invocation['name']}: RED failed in phase "
            f"{observed or '<unclassified>'}, want "
            f"{', '.join(phases)}"
        )
        return False

    def _check_red_output_expectations(self, proof, invocation, output: str, *, where: str) -> bool:
        contains, lacks = self._red_output_expectations(proof)
        for needle in contains:
            if needle not in output:
                self.err(
                    f"{invocation['name']}: RED failure output missing {needle!r} "
                    f"{where}"
                )
                return False
        for needle in lacks:
            if needle in output:
                self.err(
                    f"{invocation['name']}: RED failure output unexpectedly contains "
                    f"{needle!r} {where}"
                )
                return False
        return True

    def _guest_runtime_red_has_positive_reason(self, proof) -> bool:
        contains = proof.get("expect-output-contains")
        if isinstance(contains, str):
            return bool(contains)
        return isinstance(contains, list) and any(
            isinstance(item, str) and item for item in contains
        )

    def _run_guest_runtime_deploy_proof(self, patch, proof, invocation) -> int:
        if (
            not invocation.get("guest_c_fixture")
            and not invocation.get("guest_command_fixture")
            and invocation.get("runner") != "script"
        ):
            self.die(f"{patch['path']}: guest-runtime-deploy requires guest-c-fixture, guest-command-fixture, or script")
        if not self._guest_runtime_red_has_positive_reason(proof):
            self.die(
                f"{patch['path']}: {invocation['name']} guest-runtime-deploy "
                "RED proof needs expect-output-contains"
            )
        if invocation.get("runner") == "script":
            resources = set(invocation.get("requires_resources", []))
            if not resources & {"darling-prefix", "darling-eunion-prefix"}:
                self.die(f"{patch['path']}: guest-runtime-deploy script runner requires darling-prefix")
        missing_env = self._missing_requirements(invocation)
        if missing_env:
            self.die(
                f"{patch['path']}: missing required environment for {invocation['name']}: "
                f"{', '.join(missing_env)}"
            )
        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{patch['path']}: guest-runtime-deploy needs a Darling prefix")
        prefix = Path(prefix_text)
        self._require_runtime_scratch_space(
            f"{patch['path']}: {invocation['name']} RED"
        )
        self._preflight_runtime_profile_stack(
            self._active_runtime_profile(patch),
            f"{patch['path']}: {invocation['name']} RED",
        )
        temp = tempfile.mkdtemp(prefix="west-red-proof-runtime-")
        keep_on_failure = False
        try:
            scratch_root = Path(temp)
            with self._guest_runtime_source_forest(patch, proof, omit_patch=True) as source_root:
                build_root = self._runtime_red_build_artifacts(
                    source_root,
                    proof,
                    prefix,
                    scratch_root,
                    label="RED",
                )
                fixture_id = f"{invocation['name']}.{os.getpid()}.{int(time.time() * 1000)}"
                if invocation.get("guest_c_fixture") and proof.get("prepare-fixture-before-deploy"):
                    prepare_env = self._execution_env(invocation)
                    if prepare_env is None:
                        prepare_env = os.environ.copy()
                    else:
                        prepare_env = dict(prepare_env)
                    prepare_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                    prepare_env["WEST_GUEST_C_FIXTURE_ID"] = fixture_id
                    prepare_env["WEST_GUEST_C_FIXTURE_PREPARE_ONLY"] = "1"
                    prepare_invocation = self._invocation_from_runtime_source(invocation, source_root)
                    self.inf("  RED prepare guest fixture before bad runtime deploy")
                    with self._resource_context(prepare_invocation, prepare_env) as resource_env:
                        prepare_rc = self._run_invocation(prepare_invocation, env=resource_env)
                    if prepare_rc != 0:
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return prepare_rc
                with self._runtime_red_deployed_artifacts(proof, build_root, prefix, label="RED"):
                    red_invocation = self._guest_runtime_red_invocation(patch, proof, invocation)
                    bad_env = self._execution_env(red_invocation)
                    if bad_env is None:
                        bad_env = os.environ.copy()
                    else:
                        bad_env = dict(bad_env)
                    bad_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                    if red_invocation.get("guest_c_fixture") and proof.get("prepare-fixture-before-deploy"):
                        bad_env["WEST_GUEST_C_FIXTURE_ID"] = fixture_id
                        bad_env["WEST_GUEST_C_FIXTURE_RUN_ONLY"] = "1"
                    runtime_invocation = self._invocation_from_runtime_source(red_invocation, source_root)
                    with self._resource_context(runtime_invocation, bad_env) as resource_env:
                        red_started_at = time.time()
                    bad_result = self._run_invocation_captured(
                        runtime_invocation,
                        env=resource_env,
                    )
                    if bad_result.returncode == 0:
                        self.err("  RED proof failed: deployed bad runtime unexpectedly passed")
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return 1
                    if not self._check_red_failure_phase(
                        proof,
                        runtime_invocation,
                        bad_result.failure_phase,
                    ):
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return 1
                    if not self._check_guest_runtime_red_failure(
                        proof,
                        runtime_invocation,
                        since=red_started_at,
                        captured_output=bad_result.output,
                    ):
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return 1
                    self.inf(f"  RED runtime failed as expected (rc={bad_result.returncode})")
        except BaseException:
            keep_on_failure = True
            self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                shutil.rmtree(temp, ignore_errors=True)
        self.inf("  GREEN profile runtime")
        if not self._shutdown_runtime_prefix(prefix):
            self.die(f"guest-runtime-deploy could not clean Darling prefix before GREEN runtime: {prefix}")
        return self._run_guest_runtime_deploy_green(patch, proof, invocation)

    def _run_source_base_proof(self, patch, proof, invocation) -> int:
        if invocation["shell"]:
            self.die(f"{patch['path']}: source-base proof requires a structured runner")
        if invocation.get("guest_c_fixture"):
            self._reject_guest_source_base_red_proof(patch)
        source_env = proof.get("source-env")
        if not source_env:
            self.die(f"{patch['path']}: source-base proof needs red-proof.source-env")
        module = proof.get("source-module", patch["module"])
        with self._source_base_green_source_tree(patch, module) as green_source:
            if green_source is not None:
                green_source_env = green_source
                self.inf(f"  GREEN profile source tree: {source_env}={green_source_env}")
            else:
                green_source_env = self._project_path(module)
                self.inf(f"  GREEN current tree: {source_env}={green_source_env}")

            if invocation.get("runner") == "source-profile-script":
                proof_invocation = self._invocation_from_source_profile(invocation, green_source_env)
            else:
                proof_invocation = invocation

            script_path = proof_invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                self.die(f"{patch['path']}: test script not found: {script_path}")

            module_repo = self._project_path(module)
            bad_revision = self._bad_revision(patch)
            with tempfile.TemporaryDirectory(prefix="west-red-proof-") as temp:
                worktree = Path(temp) / "source-base"
                subprocess.run(
                    ["git", "worktree", "add", "--quiet", "--detach", str(worktree), bad_revision],
                    cwd=module_repo,
                    check=True,
                )
                try:
                    bad_env = os.environ.copy()
                    exec_env = self._execution_env(proof_invocation)
                    if exec_env:
                        bad_env.update(exec_env)
                    bad_env[source_env] = str(worktree)
                    self.inf(f"  RED source tree: {bad_revision} via {source_env}={worktree}")
                    bad_result = self._run_invocation_captured(proof_invocation, env=bad_env)
                    if bad_result.returncode == 0:
                        self.err("  RED proof failed: source-base run unexpectedly passed")
                        return 1
                    if not self._check_red_failure_phase(
                        proof,
                        proof_invocation,
                        bad_result.failure_phase,
                    ):
                        return 1
                    if not self._check_red_output_expectations(
                        proof,
                        proof_invocation,
                        bad_result.output,
                        where="in source-base RED output",
                    ):
                        return 1
                    self.inf(f"  RED path failed as expected (rc={bad_result.returncode})")
                finally:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree)],
                        cwd=module_repo,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

            green_env = self._execution_env(invocation)
            if green_env is None:
                green_env = os.environ.copy()
            else:
                green_env = dict(green_env)
            if source_env:
                green_env[source_env] = str(green_source_env)
            return self._run_invocation(proof_invocation, env=green_env)

    def _run_red_proofs(self, tests, list_only: bool, unknown: list[str]) -> int:
        """Run the proof that a regression test really distinguishes old/bad behavior.

        A normal metadata test run always expects GREEN on the current checkout.
        RED proof is an explicit second mode. `mode: self` means the
        test binary/script contains its own bad-path oracle
        (for example, run an old algorithm and require that it fails, then run
        the fixed algorithm and require that it passes). `mode: source-base`
        keeps the current test asset and points it at a bad/source-base worktree
        through an explicit source-root environment variable.
        """
        if unknown:
            self.die("metadata RED proofs do not accept raw ctest passthrough arguments")
        rc = 0
        seen_invocations: set[str] = set()
        self._prune_stale_west_temp_worktrees()
        with ExitStack() as source_base_green_stack:
            self._source_base_green_stack = source_base_green_stack
            self._source_base_green_cache = {}
            try:
                for patch, test in tests:
                    proof = test.get("red-proof")
                    name = test.get("name", "-")
                    if not proof:
                        self.die(
                            f"{patch['path']}: {name} is marked red but has no red-proof metadata"
                        )
                    mode = proof.get("mode") if isinstance(proof, dict) else proof
                    invocation = self._test_invocation(patch, test)
                    self.inf(f"{patch['path']}: {name} RED proof [{mode}]")
                    self.inf(f"  {self._display_invocation(invocation)}")
                    if mode == "guest-runtime-deploy" and isinstance(proof, dict):
                        self.inf(f"  {self._display_guest_runtime_deploy_plan(proof)}")
                    if list_only:
                        continue
                    if mode not in {"self", "source-base", "guest-runtime-deploy"}:
                        self.die(
                            f"{patch['path']}: RED proof mode {mode!r} is not implemented; "
                            "use mode: self, source-base, or guest-runtime-deploy"
                        )
                    if not self._red_failure_phases(proof):
                        self.die(
                            f"{patch['path']}: {name} RED proof needs expect-failure-phase"
                        )
                    if (
                        mode in {"self", "guest-runtime-deploy"}
                        and not self._guest_runtime_red_has_positive_reason(proof)
                    ):
                        self.die(
                            f"{patch['path']}: {name} RED proof needs expect-output-contains"
                        )
                    if mode == "source-base" and invocation.get("guest_c_fixture"):
                        self._reject_guest_source_base_red_proof(patch)
                    script_path = invocation.get("script_path")
                    if script_path is not None and not script_path.is_file():
                        self.die(f"{patch['path']}: test script not found: {script_path}")
                    missing_env = self._missing_requirements(invocation)
                    if missing_env:
                        self.die(
                            f"{patch['path']}: missing required environment for {name}: "
                            f"{', '.join(missing_env)}"
                        )
                    invocation_key = (
                        f"{patch['path']}:{invocation['key']}"
                        if mode == "source-base"
                        else invocation["key"]
                    )
                    if invocation_key in seen_invocations:
                        self.inf("  skipped duplicate invocation already run")
                        continue
                    seen_invocations.add(invocation_key)
                    with self._required_profile_context(patch, invocation):
                        if mode == "source-base":
                            result_rc = self._run_source_base_proof(patch, proof, invocation)
                        elif mode == "guest-runtime-deploy":
                            result_rc = self._run_guest_runtime_deploy_proof(patch, proof, invocation)
                        else:
                            exec_env = self._execution_env(invocation)
                            with self._resource_context(invocation, exec_env):
                                self_result = self._run_invocation_captured(invocation, env=exec_env)
                            if self_result.returncode:
                                result_rc = self_result.returncode
                            elif not self._check_red_failure_phase(proof, invocation, "self"):
                                result_rc = 1
                            elif not self._check_red_output_expectations(
                                proof,
                                invocation,
                                self_result.output,
                                where="in self-contained RED output",
                            ):
                                result_rc = 1
                            else:
                                self.inf("  self-contained RED arm observed as expected")
                                result_rc = 0
                    if result_rc:
                        rc = result_rc
            finally:
                del self._source_base_green_cache
                del self._source_base_green_stack
        return rc

    def _reject_unsupported_red_proof_models(self, tests) -> None:
        for patch, test in tests:
            proof = test.get("red-proof")
            if not isinstance(proof, dict) or proof.get("mode") != "source-base":
                continue
            if test.get("runner") == "guest-c-fixture":
                self._reject_guest_source_base_red_proof(patch)

    def _check_red_proof_requirements(self, tests) -> None:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            missing = self._missing_requirements(invocation)
            if missing:
                self.die(
                    f"{patch['path']}: missing required environment for "
                    f"{test.get('name', '-')}: {', '.join(missing)}"
                )

    def _red_proof_audit(self, tests) -> list[str]:
        """Return manifest gaps that would make a RED result ambiguous."""
        missing = []
        for patch, test in tests:
            proof = test.get("red-proof")
            if not isinstance(proof, dict):
                continue
            mode = proof.get("mode")
            if mode not in {"self", "source-base", "guest-runtime-deploy"}:
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} has unsupported RED mode {mode!r}"
                )
                continue
            if not self._red_failure_phases(proof):
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} RED proof needs expect-failure-phase"
                )
            if mode in {"self", "guest-runtime-deploy"} and not self._guest_runtime_red_has_positive_reason(proof):
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} "
                    "RED proof needs expect-output-contains"
                )
        return missing

    def _shutdown_test_prefix(self) -> bool:
        prefix = getattr(self, "_prefix", None)
        if not prefix or getattr(self, "_keep_prefix_running", False):
            return True
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            env = os.environ.copy()
            env.update(self._darling_prefix_env(prefix))
            self.inf(f"shutdown Darling prefix: {prefix}")
            timeout_seconds = int(os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", "15"))
            result = shutdown_guest_prefix(
                launcher,
                prefix,
                cwd=Path.cwd(),
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if result.timed_out:
                self.err(f"Darling prefix shutdown timed out for {prefix}; forcing cleanup")
        self._kill_dserver_for_prefix(Path(prefix))
        leftovers = self._prefix_process_snapshot(Path(prefix))
        if not leftovers and self._cleanup_prefix_mounts(Path(prefix)):
            self._remove_stale_init_pid(Path(prefix))
            return True
        if not leftovers:
            return False
        self.err(f"leftover Darling prefix process(es) after cleanup for {prefix}:")
        for entry in leftovers:
            self.err(f"  {entry}")
        return False

    def _cleanup_prefix_mounts(self, prefix: Path) -> bool:
        result = cleanup_prefix_mounts(prefix)
        for message in result.changed:
            self.inf(f"cleanup Darling prefix mount: {message}")
        for message in result.problems:
            self.err(f"leftover Darling prefix mount for {prefix}: {message}")
        return result.success

    def _remove_stale_init_pid(self, prefix: Path) -> None:
        remove_stale_init_pid(prefix, pid_is_usable=darling_init_pid_is_usable)

    def _ps_entries(self) -> list[tuple[int, int, str]]:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
        entries = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            entries.append((int(parts[0]), int(parts[1]), parts[2]))
        return entries

    def _prefix_process_snapshot(self, prefix: Path) -> list[str]:
        entries = prefix_process_snapshot(prefix, self._ps_entries())
        entries.extend(rootless_prefix_process_snapshot(prefix))
        return sorted(set(entries))

    def _kill_dserver_for_prefix(self, prefix: Path) -> None:
        pids = darlingserver_pids_for_prefix(prefix, self._ps_entries())
        if not pids:
            return
        self.wrn(f"stopping live darlingserver for {prefix}: pids={pids}")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            live = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    live.append(pid)
                except ProcessLookupError:
                    pass
            if not live:
                return
            for pid in live:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            time.sleep(1)

    @contextmanager
    def _prefix_resource_context(self, enabled: bool):
        prefix = getattr(self, "_prefix", None)
        if not enabled or not prefix:
            yield
            return

        lock_path = Path(prefix).expanduser() / ".west-test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            self.inf(f"lock Darling prefix: {prefix}")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._prefix_cleanup_failed = False
            try:
                if not self._shutdown_test_prefix():
                    self._prefix_cleanup_failed = True
                    self.die(
                        f"could not reset Darling prefix before test run: {prefix}"
                    )
                yield
            finally:
                try:
                    if not self._shutdown_test_prefix():
                        self._prefix_cleanup_failed = True
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _changed_submodules(self) -> list[str]:
        """Submodules whose checkout differs from their manifest revision.

        Prefer West's local manifest-rev ref when available. It records the
        exact revision selected by the manifest, regardless of whether the
        manifest used a branch name or SHA. Dirty worktrees are always selected.
        """
        changed: list[str] = []
        for project in self.manifest.projects:
            if not self.manifest.is_active(project):
                continue
            path = Path(self.topdir) / project.path
            if path == Path(self.manifest.repo_abspath):
                continue
            if not (path / ".git").exists():
                continue
            label_name = Path(project.path).name
            if self._worktree_dirty(path, parent=project.name == "darling"):
                changed.append(label_name)
                continue
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            manifest_rev = subprocess.run(
                ["git", "rev-parse", "--verify", "manifest-rev^{commit}"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if not manifest_rev and project.revision:
                manifest_rev = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{project.revision}^{{commit}}"],
                    cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if head and manifest_rev and head != manifest_rev:
                changed.append(label_name)
        return changed

    def _configure_and_build(
        self,
        testkit: Path,
        executor: str | None,
        *,
        darling_launcher: str | None = None,
        prefix: str | None = None,
        bundle_root: str | None = None,
    ) -> Path:
        build = testkit / "build"
        cfg = ["cmake", "-S", str(testkit), "-B", str(build), "-G", "Ninja"]
        if executor:
            cfg.append(f"-DDARLING_TEST_EXECUTOR={executor}")
        if prefix:
            cfg.append(f"-DDARLING_TEST_PREFIX={prefix}")
        if getattr(self, "_prefix_env", {}).get("DARLING_NOOVERLAYFS") == "1":
            cfg.append("-DDARLING_TEST_NO_OVERLAYFS=ON")
        if bundle_root:
            cfg.append(f"-DDARLING_TEST_BUNDLE_ROOT={bundle_root}")
        self.inf(f"configuring: {testkit}")
        self._run_testkit_build_command("configure", cfg)
        self._run_testkit_build_command("build", ["ninja", "-C", str(build)])
        return build

    def _run_testkit_build_command(self, stage: str, args) -> None:
        """Run a CTest suite build without letting toolchain hangs escape west."""

        timeout_seconds = int(os.environ.get("WEST_TEST_BUILD_TIMEOUT_SECONDS", "1800"))
        result = run_bounded(
            args,
            cwd=Path(self.topdir),
            env=None,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        if result.timed_out:
            self.err(f"testkit {stage} timed out after {timeout_seconds}s")
        self._dump_command_tail(f"testkit {stage}", result)
        self.die(f"testkit {stage} failed with rc {result.returncode}")

    @staticmethod
    def _clear_ctest_failure_record(build: Path) -> None:
        """Discard CTest's prior-run failure list before a new invocation.

        CTest does not clear ``LastTestsFailed.log`` after a later green run.
        Leaving it in place makes a fresh successful selection look failed to
        humans and to any diagnostic tooling that inspects the build tree.
        """

        (build / "Testing" / "Temporary" / "LastTestsFailed.log").unlink(
            missing_ok=True
        )

    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(
            entry.stat().st_size
            for entry in path.rglob("*")
            if entry.is_file() and not entry.is_symlink()
        )

    @staticmethod
    def _format_size(size: int) -> str:
        """Format a byte count compactly for cleanup diagnostics."""

        for unit, scale in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
            if size >= scale:
                return f"{size / scale:.1f}{unit}"
        return f"{size}B"

    def _gc_bundles(
        self, root: Path, keep_last: int, max_mb: int, dry_run: bool = False
    ) -> None:
        """Prune debug bundles so the dir cannot balloon (we saw 7.4G/980).

        Drop any bundle over max_mb (forensic cores/rpctrace), then keep only
        the newest keep_last of the rest. Bundles are timestamp-named dirs.
        Non-directory entries (stray files) are left untouched.
        """
        root = root.expanduser()
        if not root.is_dir():
            self.inf(f"no bundle dir at {root}")
            return
        bundles = sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        cap = max_mb * 1024 * 1024
        freed = 0
        kept = 0
        verb = "would prune" if dry_run else "pruned"
        for bundle in bundles:
            size = self._dir_size(bundle)
            over_cap = size > cap
            over_count = kept >= keep_last
            if over_cap or over_count:
                why = "size" if over_cap else "count"
                freed += size
                self.inf(f"{verb} ({why}, {size // (1024 * 1024)}M): {bundle.name}")
                if not dry_run:
                    shutil.rmtree(bundle, ignore_errors=True)
            else:
                kept += 1
        action = "would free" if dry_run else "freed"
        self.inf(
            f"gc: kept {kept}, {action} {freed // (1024 * 1024)}M from {root}"
        )

    def _gc_runtime_proof_scratch(
        self,
        root: Path,
        max_age_hours: float,
        keep_last: int,
        dry_run: bool = False,
    ) -> None:
        root = root.expanduser()
        if max_age_hours < 0:
            self.die("--proof-scratch-max-age-hours must be >= 0")
        if keep_last < 0:
            self.die("--proof-scratch-keep-last must be >= 0")
        if not root.is_dir():
            self.inf(f"no proof scratch root at {root}")
            return
        cutoff = time.time() - (max_age_hours * 3600)
        patterns = (
            "west-red-proof-runtime-*",
            "west-green-proof-runtime-*",
            "west-red-proof-source-*",
            "west-ctest-runtime-*",
            "west-runtime-*",
        )
        all_scratch_dirs = sorted(
            {
                path
                for pattern in patterns
                for path in root.glob(pattern)
                if path.is_dir() and not path.is_symlink()
            },
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        freed = 0
        retained = 0
        pruned = 0
        verb = "would prune" if dry_run else "pruned"
        now = time.time()
        for index, scratch in enumerate(all_scratch_dirs):
            size = self._dir_size(scratch)
            age_hours = max(0.0, (now - scratch.stat().st_mtime) / 3600)
            over_count = index >= keep_last
            stale = scratch.stat().st_mtime <= cutoff
            if over_count or stale:
                if over_count and stale:
                    reason = "count+age"
                elif over_count:
                    reason = "count"
                else:
                    reason = "age"
                freed += size
                pruned += 1
                self.inf(
                    f"{verb} proof scratch ({reason}, {self._format_size(size)}, "
                    f"age {age_hours:.1f}h): {scratch}"
                )
                if not dry_run:
                    shutil.rmtree(scratch, ignore_errors=True)
            else:
                retained += 1
                self.inf(
                    f"retained proof scratch (newest, {self._format_size(size)}, "
                    f"age {age_hours:.1f}h): {scratch}"
                )
        action = "would free" if dry_run else "freed"
        self.inf(
            "proof-scratch gc: "
            f"retained {retained}, {verb} {pruned} dir(s), "
            f"{action} {self._format_size(freed)} from {root}"
        )

    def _gc_guest_runner_output(
        self,
        root: Path,
        max_age_hours: float,
        dry_run: bool = False,
    ) -> None:
        """Prune stale local output files left by pre-cleanup guest C runners.

        The guest runner now unlinks its output on every exit path.  This pass
        only repairs historical files, so it uses the same age threshold as
        runtime scratch and deliberately ignores directories, symlinks, and
        fresh output that may belong to a still-running test.
        """

        root = root.expanduser()
        if max_age_hours < 0:
            self.die("--proof-scratch-max-age-hours must be >= 0")
        if not root.is_dir():
            self.inf(f"no guest runner output root at {root}")
            return
        cutoff = time.time() - (max_age_hours * 3600)
        outputs = sorted(
            (
                path
                for path in root.glob("west-ctest-guest-c.*")
                if path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime <= cutoff
            ),
            key=lambda path: path.stat().st_mtime,
        )
        freed = 0
        verb = "would prune" if dry_run else "pruned"
        for output in outputs:
            size = output.stat().st_size
            freed += size
            self.inf(f"{verb} guest runner output ({size}B): {output}")
            if not dry_run:
                output.unlink(missing_ok=True)
        action = "would free" if dry_run else "freed"
        self.inf(
            "guest-runner gc: "
            f"{verb} {len(outputs)} file(s), {action} {freed}B from {root}"
        )

    # --- entrypoint ---------------------------------------------------------

    def do_run(self, args, unknown):
        self._prefix = self._resolve_prefix(args)
        self._executor = self._resolve_executor(args.executor)
        self._bundle_root = str(Path(args.bundle_root).expanduser())
        self._materialize_profile = args.materialize_profile
        self._keep_prefix_running = args.keep_prefix_running

        if args.ctest_timeout_seconds <= 0:
            self.die("--ctest-timeout-seconds must be > 0")

        if args.gc:
            self._gc_bundles(
                Path(args.bundle_root), args.keep_last, args.max_bundle_mb,
                dry_run=args.dry_run,
            )
            self._gc_runtime_proof_scratch(
                Path(args.proof_scratch_root),
                args.proof_scratch_max_age_hours,
                args.proof_scratch_keep_last,
                dry_run=args.dry_run,
            )
            self._gc_guest_runner_output(
                Path(args.proof_scratch_root),
                args.proof_scratch_max_age_hours,
                dry_run=args.dry_run,
            )
            return

        if args.red_audit:
            profile = args.profile or "homebrew"
            selected, missing = self._metadata_tests(
                profile, args.patch, args.bead, args.env, args.diag, red_only=False
            )
            missing_reasons = self._red_proof_audit(selected)
            for patch in missing:
                self.inf(f"MISSING {patch['path']} [{patch.get('bead', '-')}]")
            for message in missing_reasons:
                self.inf(f"RED-REASON-MISSING {message}")
            self.inf(f"red-audit: {len(missing)} patch(es) missing tests/exception")
            self.inf(
                "red-audit: "
                f"{len(missing_reasons)} RED proof contract gap(s)"
            )
            if missing or missing_reasons:
                self.die("red-audit failed")
            return

        if args.patch and not args.profile:
            self.die("--patch requires --profile")
        if args.profile and args.submodule:
            self.die("--submodule selects CTest suite tests; use --patch/--profile for patch metadata")
        if args.profile and (args.fuzz or args.stress):
            self.die("--fuzz/--stress select CTest suite tests; use --patch/--profile for patch metadata")
        bootstrap_runtime_profile = getattr(args, "bootstrap_runtime_profile", None)
        bootstrap_syscall_trace = getattr(args, "bootstrap_syscall_trace", None)
        bootstrap_syscall_stack = getattr(args, "bootstrap_syscall_stack", False)
        if bootstrap_syscall_stack and not bootstrap_syscall_trace:
            self.die("--bootstrap-syscall-stack requires --bootstrap-syscall-trace")
        if bootstrap_syscall_trace and not bootstrap_runtime_profile:
            self.die("--bootstrap-syscall-trace requires --bootstrap-runtime-profile")
        if bootstrap_runtime_profile:
            incompatible = []
            if args.profile or args.patch or args.prove_red or args.red_only or args.red_audit:
                incompatible.append("patch metadata selection")
            if args.changed or args.bead or args.submodule or args.label or args.fuzz or args.stress:
                incompatible.append("CTest selection")
            if args.list or args.with_runtime_profile or unknown:
                incompatible.append("CTest execution options")
            if incompatible:
                self.die(
                    "--bootstrap-runtime-profile is a prefix provisioning operation; "
                    "do not combine it with " + ", ".join(incompatible)
                )
            self._bootstrap_syscall_trace = Path(bootstrap_syscall_trace) if bootstrap_syscall_trace else None
            self._bootstrap_syscall_stack = bootstrap_syscall_stack
            try:
                self._bootstrap_runtime_profile(bootstrap_runtime_profile)
                if getattr(self, "_prefix_cleanup_failed", False):
                    raise SystemExit(1)
                return
            finally:
                self._bootstrap_syscall_trace = None
                self._bootstrap_syscall_stack = False

        if args.profile:
            selected, missing = self._metadata_tests(
                args.profile, args.patch, args.bead, args.env, args.diag, args.red_only
            )
            if args.prove_red:
                selected = [
                    (patch, test)
                    for patch, test in selected
                    if test.get("red") or test.get("red-proof")
                ]
                if not selected:
                    self.die("no red-proof tests selected from patch metadata")
                self._reject_unsupported_red_proof_models(selected)
                if not args.list:
                    self._check_red_proof_requirements(selected)
            materialize_was_requested = self._materialize_profile
            previous_active_profile = getattr(self, "_active_profile", None)
            self._active_profile = args.profile
            if (
                selected
                and not args.list
                and not self._materialize_profile
                and not self._profile_is_applied(args.profile)
                and self._metadata_needs_profile_worktree(selected)
            ):
                self.inf(
                    f"{args.profile}: selected tests need the profile checkout; "
                    "temporarily materializing profile in worktrees"
                )
                self._materialize_profile = True
            try:
                with self._selected_profile_context(args.profile, list_only=args.list):
                    if missing:
                        for patch in missing:
                            self.inf(f"missing test metadata: {patch['path']} [{patch.get('bead', '-')}]")
                    if selected:
                        needs_prefix = self._metadata_needs_prefix(selected) and not args.list
                        if args.prove_red:
                            needs_prefix = self._metadata_needs_prefix(selected) and not args.list
                            with self._prefix_resource_context(needs_prefix):
                                result = self._run_red_proofs(selected, args.list, unknown)
                            if getattr(self, "_prefix_cleanup_failed", False):
                                result = result or 1
                            raise SystemExit(result)
                        with self._prefix_resource_context(needs_prefix):
                            result = self._run_metadata_tests(selected, args.list, unknown)
                        if getattr(self, "_prefix_cleanup_failed", False):
                            result = result or 1
                        raise SystemExit(result)
                    if args.list:
                        return
                    self.die("no tests selected from patch metadata")
            finally:
                self._materialize_profile = materialize_was_requested
                self._active_profile = previous_active_profile

        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f"no testkit at {testkit}")

        launcher = self._resolve_darling_launcher(self._prefix)
        if args.env == "darling" and not launcher and not args.list:
            if self._prefix:
                self.die(
                    "env:darling CTest runs need the selected prefix launcher: "
                    f"{Path(self._prefix).expanduser() / 'bin' / 'darling'}"
                )
            self.die(
                "env:darling CTest runs need a Darling launcher; pass --prefix, "
                "set DARLING/DARLING_LAUNCHER, or install ~/work/darling-prefix/bin/darling"
            )
        build = self._configure_and_build(
            testkit,
            self._executor,
            darling_launcher=launcher,
            prefix=self._prefix,
            bundle_root=str(getattr(self, "_bundle_root", "")),
        )

        changed = None
        if args.changed:
            changed = self._changed_submodules()
            if not changed:
                self.inf("no changed submodules; nothing selected by --changed")
                return
            self.inf(f"changed submodules: {', '.join(changed)}")
        # CTest -L selectors are ANDed per flag; changed submodules use one
        # alternation label regex to select any touched submodule.
        label_args = ctest_selector_label_args(
            bead=args.bead,
            env=args.env,
            diag=args.diag,
            label=args.label,
            fuzz=args.fuzz,
            stress=args.stress,
            changed_submodules=changed,
            submodules=args.submodule,
        )
        ctest = ctest_command(
            build,
            label_args=label_args,
            list_only=args.list,
            passthrough=unknown,
        )

        runtime_groups: list[dict] = []
        if not args.list:
            runtime_groups = self._selected_ctest_runtime_groups(
                build, label_args, unknown, args.with_runtime_profile
            )
        needs_prefix = (
            ctest_uses_prefix(env=args.env, list_only=args.list)
            or any(group["profiles"] for group in runtime_groups)
        )
        commands: list[tuple[list[str], list[str]]] = []
        if args.list or not runtime_groups:
            commands.append((ctest, []))
        else:
            for group in runtime_groups:
                try:
                    group_passthrough = ctest_runtime_group_passthrough(unknown)
                except ValueError as error:
                    self.die(f"invalid CTest passthrough selection: {error}")
                group_ctest = ctest_command(
                    build,
                    passthrough=[
                        *group_passthrough,
                        "-R",
                        ctest_test_name_regex(group["tests"]),
                    ],
                )
                commands.append((group_ctest, group["profiles"]))
        with self._prefix_resource_context(needs_prefix):
            self._clear_ctest_failure_record(build)
            rc = 0
            for command, profiles in commands:
                profile_text = ", ".join(profiles) if profiles else "no runtime deployment"
                self.inf(f"running ({profile_text}): {' '.join(command)}")
                with self._ctest_runtime_profile_context(profiles) as runtime_env:
                    result = run_bounded(
                        command,
                        cwd=Path(self.topdir),
                        env=runtime_env,
                        timeout_seconds=int(args.ctest_timeout_seconds),
                    )
                if result.timed_out:
                    self.err(
                        "CTest selection timed out after "
                        f"{args.ctest_timeout_seconds}s"
                    )
                rc = rc or result.returncode
        if getattr(self, "_prefix_cleanup_failed", False):
            rc = rc or 1
        raise SystemExit(rc)
