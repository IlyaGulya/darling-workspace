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
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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
    ctest_selector_label_args,
)
from test_cmake import archive_source_to, run_darling_cmake_target_fixture
from test_manifest import ManifestError, load_test_profile
from test_prefix import (
    darlingserver_pids_for_prefix,
    prefix_process_snapshot,
    remove_stale_init_pid,
)
from test_resources import resource_context
from test_runtime import (
    describe_runtime_deploy_plan,
    runtime_build_targets,
    runtime_deploy_targets,
)
from test_worktrees import prune_stale_west_temp_worktrees


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
        return parser

    # --- helpers ------------------------------------------------------------

    def _testkit_dir(self) -> Path:
        return Path(self.manifest.repo_abspath) / "testkit"

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
                            ["git", "-c", "gc.auto=0", "am", "--3way", str(patch_file)],
                            cwd=target,
                            check=True,
                        )
                yield
            finally:
                self._project_overrides = previous_overrides
                for repo, target in reversed(added):
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(target)],
                        cwd=repo,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
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
        if os.environ.get("DARLING"):
            return os.environ["DARLING"]
        if os.environ.get("DARLING_LAUNCHER"):
            return os.environ["DARLING_LAUNCHER"]
        candidates = []
        if prefix:
            candidates.append(Path(prefix).expanduser() / "bin" / "darling")
        candidates.append(Path("~/work/darling-prefix/bin/darling").expanduser())
        for candidate in candidates:
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
        data = self._load_profile(profile)
        selected = []
        missing = []
        found_patch = False
        for patch in data.get("patches", []):
            if patch_path and patch["path"] != patch_path:
                continue
            found_patch = True
            if bead and patch.get("bead") != bead:
                continue
            all_tests = [
                test
                for test in (patch.get("tests") or [])
                if not test.get("blocked")
            ]
            tests = all_tests
            if red_only:
                tests = [test for test in tests if test.get("red")]
            if env:
                tests = [test for test in tests if test.get("env") == env]
            if diag:
                tests = [test for test in tests if self._resolved_diag(test) == diag]
            if tests:
                for test in tests:
                    selected.append((patch, test))
            elif not all_tests and not patch.get("test-exception"):
                missing.append(patch)
        if patch_path and not found_patch:
            self.die(f"{profile}: patch not found or has no selected tests: {patch_path}")
        return selected, missing

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
        if invocation.get("guest_c_fixture"):
            return self._run_guest_c_fixture(invocation, env=env)
        if invocation.get("guest_command_fixture"):
            return self._run_guest_command_fixture(invocation, env=env)
        if invocation.get("c_fixture"):
            return self._run_c_fixture(invocation, env=env)
        if invocation.get("object_symbol_fixture"):
            return self._run_object_symbol_fixture(invocation, env=env)
        if invocation.get("source_build_fixture"):
            return self._run_source_build_fixture(invocation, env=env)
        if invocation.get("source_script_fixture"):
            return self._run_source_script_fixture(invocation, env=env)
        if invocation.get("cmake_configure_fixture"):
            return self._run_cmake_configure_fixture(invocation, env=env)
        if invocation.get("darling_cmake_target_fixture"):
            return run_darling_cmake_target_fixture(
                invocation,
                env=env,
                executor=getattr(self, "_executor", None),
                bundle_root=getattr(self, "_bundle_root", "~/work/darling-debug"),
                inf=self.inf,
                err=self.err,
                die=self.die,
            )
        run_env = env if env is not None else invocation.get("env")
        result = subprocess.run(
            self._debug_runner_args(invocation),
            cwd=invocation["cwd"],
            env=run_env,
            shell=False,
            check=False,
        )
        rc = result.returncode
        if rc:
            return rc
        return self._check_host_traces(invocation, run_env)

    def _run_invocation_captured(self, invocation, env=None) -> tuple[int, str]:
        """Run an invocation while capturing subprocess stdout/stderr."""
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
            return rc, output.read()

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
            compile_rc = subprocess.run(
                args,
                cwd=invocation["cwd"],
                env=run_env,
                check=False,
            ).returncode
            if compile_rc:
                return compile_rc
            return subprocess.run(
                [str(binary)],
                cwd=invocation["cwd"],
                env=run_env,
                check=False,
            ).returncode

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
                compile_rc = subprocess.run(
                    args,
                    cwd=invocation["cwd"],
                    env=run_env,
                    check=False,
                ).returncode
                if compile_rc:
                    return compile_rc
                nm = subprocess.run(
                    ["nm", "-u", str(object_path)],
                    cwd=invocation["cwd"],
                    env=run_env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if nm.returncode:
                    sys.stderr.write(nm.stdout)
                    sys.stderr.write(nm.stderr)
                    return nm.returncode
                symbols = {
                    line.split()[-1]
                    for line in nm.stdout.splitlines()
                    if line.split()
                }
                for symbol in check.get("present_undefined_symbols", []):
                    if symbol not in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: missing undefined symbol {symbol}")
                        return 1
                for symbol in check.get("absent_undefined_symbols", []):
                    if symbol in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: unexpected undefined symbol {symbol}")
                        return 1
                if check.get("present_defined_symbols") or check.get("absent_defined_symbols"):
                    defined_nm = subprocess.run(
                        ["nm", "-g", str(object_path)],
                        cwd=invocation["cwd"],
                        env=run_env,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if defined_nm.returncode:
                        sys.stderr.write(defined_nm.stdout)
                        sys.stderr.write(defined_nm.stderr)
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
            try:
                result = subprocess.run(
                    args,
                    cwd=invocation["cwd"],
                    env=child_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=int(invocation.get("timeout_seconds", 600)),
                )
            except subprocess.TimeoutExpired:
                self.err(f"{invocation['name']}: cmake configure timed out")
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
                    return 1
            elif result.returncode != int(rc_mode):
                write_output_tail()
                self.err(
                    f"{invocation['name']}: cmake configure rc {result.returncode}, "
                    f"want {rc_mode}"
                )
                return 1
            for needle in expect.get("output-contains", []):
                if str(needle) not in output:
                    write_output_tail()
                    self.err(f"{invocation['name']}: cmake output missing {needle!r}")
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
            return 1

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        for case in invocation.get("cases", []):
            if os.access(script_path, os.X_OK):
                args = [str(script_path), *case.get("args", [])]
            else:
                args = ["sh", str(script_path), *case.get("args", [])]
            try:
                result = subprocess.run(
                    args,
                    cwd=source_root,
                    env=run_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                self.err(
                    f"{invocation['name']}:{case['name']}: timed out after "
                    f"{timeout_seconds}s"
                )
                return 124
            expected_rc = case.get("returncode", 0)
            if result.returncode != expected_rc:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
                self.err(
                    f"{invocation['name']}:{case['name']}: rc {result.returncode}, "
                    f"want {expected_rc}"
                )
                return 1
            expected_stdout = case.get("stdout")
            if expected_stdout is not None and result.stdout != expected_stdout:
                sys.stderr.write(result.stderr)
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
            rc = archive_source_to(source_root, build_root)
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
                try:
                    result = subprocess.run(
                        ["/bin/bash", "-lc", command],
                        cwd=build_root,
                        env=child_env,
                        check=False,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    self.err(
                        f"  source-build-fixture timed out after "
                        f"{timeout_seconds}s: {command}"
                    )
                    return 124
                if result.returncode:
                    return result.returncode
            return 0

    def _run_guest_c_fixture(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = self._execution_env(invocation)
        if not run_env:
            run_env = os.environ.copy()

        prefix = run_env.get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: guest-c-fixture needs DPREFIX")
        launcher = (
            run_env.get("DARLING_LAUNCHER")
            or run_env.get("DARLING")
            or self._resolve_darling_launcher(prefix)
        )
        if not launcher:
            self.die(f"{invocation['name']}: guest-c-fixture needs a Darling launcher")

        with tempfile.TemporaryDirectory(prefix=f"west-guest-c-fixture-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            host_runner = tempdir / "run.sh"
            verdict = tempdir / "verdict.txt"
            name = invocation["name"]
            run_id = run_env.get("WEST_GUEST_C_FIXTURE_ID") or f"{os.getpid()}.{int(time.time() * 1000)}"
            guest_src = f"/tmp/{name}.{run_id}.c"
            guest_bin = f"/tmp/{name}.{run_id}"
            compile_parts = [
                '"$guest_cc"',
                *[quote(arg) for arg in invocation.get("guest_cflags", "").split() if arg],
                *[quote(arg) for arg in invocation.get("compile_flags", [])],
                "-o",
                quote(guest_bin),
                quote(guest_src),
                *[quote(arg) for arg in invocation.get("link_flags", [])],
            ]
            run_parts = [
                quote(guest_bin),
                *[quote(arg) for arg in invocation.get("run_args", [])],
            ]
            guest_prelude = invocation.get("guest_prelude", "")
            if not guest_prelude:
                guest_prelude = ":"
            guest_env_setup = "\n".join(
                f"export {key}={quote(value)}"
                for key, value in invocation.get("guest_env_vars", {}).items()
            ) or ":"
            trace_setup_lines = []
            trace_check_lines = []
            trace_dump_lines = []
            for index, temp_file in enumerate(invocation.get("host_temp_files", [])):
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
                temp_var = f"host_temp_{index}"
                trace_setup_lines.extend(
                    [
                        f"{temp_var}=\"$DPREFIX/{rel_path}\"",
                        f"rm -f \"${temp_var}\"",
                        f"mkdir -p \"$(dirname \"${temp_var}\")\"",
                        f"export {env_name}=\"${temp_var}\"",
                    ]
                )
                if "contents" in temp_file:
                    trace_setup_lines.append(
                        f"printf %s {quote(str(temp_file['contents']))} > \"${temp_var}\""
                    )
                trace_dump_lines.extend(
                    [
                        f"if [ -f \"${temp_var}\" ]; then",
                        f"\tprintf '%s\\n' \"--- host temp file: ${temp_var} ---\" >&2",
                        f"\tcat \"${temp_var}\" >&2 || true",
                        "fi",
                    ]
                )
            for index, trace in enumerate(invocation.get("host_trace_files", [])):
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
                contains = [str(item) for item in trace.get("contains", [])]
                trace_var = f"host_trace_{index}"
                trace_setup_lines.extend(
                    [
                        f"{trace_var}=\"$DPREFIX/{rel_path}\"",
                        f"rm -f \"${trace_var}\"",
                        f"mkdir -p \"$(dirname \"${trace_var}\")\"",
                        f"export {env_name}=\"${trace_var}\"",
                    ]
                )
                trace_dump_lines.extend(
                    [
                        f"if [ -f \"${trace_var}\" ]; then",
                        f"\tprintf '%s\\n' \"--- host trace file: ${trace_var} ---\" >&2",
                        f"\tcat \"${trace_var}\" >&2 || true",
                        "fi",
                    ]
                )
                trace_check_lines.extend(
                    [
                        f"if [ ! -f \"${trace_var}\" ]; then",
                        f"\tprintf 'missing host trace file: %s\\n' \"${trace_var}\" >&2",
                        "\texit 1",
                        "fi",
                        f"cat \"${trace_var}\"",
                    ]
                )
                for expected in contains:
                    trace_check_lines.append(f"grep -F -q {quote(expected)} \"${trace_var}\"")
            trace_setup = "\n".join(trace_setup_lines) or ":"
            trace_check = "\n".join(trace_check_lines) or ":"
            trace_dump = "\n".join(trace_dump_lines) or ":"
            trace_settle = "sleep 0.25" if invocation.get("host_trace_files") else ":"
            host_stat_deltas = invocation.get("host_stat_deltas", [])
            host_stat_specs = quote(json.dumps(host_stat_deltas))
            host_stat_tool = quote(
                str(invocation.get("_host_stat_tool", invocation.get("host_stat_tool", "darling-stat")))
            )
            host_stat_setup = ":"
            host_stat_before = ":"
            host_stat_after = ":"
            host_stat_dump = ":"
            host_stat_check = ":"
            if host_stat_deltas:
                host_stat_setup = f"""host_stat_tool={host_stat_tool}
host_stat_before={quote(str(tempdir / "stat-before.json"))}
host_stat_after={quote(str(tempdir / "stat-after.json"))}
host_stat_specs={host_stat_specs}
if [ ! -x "$host_stat_tool" ]; then
\tprintf 'missing darling stat tool: %s\\n' "$host_stat_tool" >&2
\texit 1
fi"""
                host_stat_before = '"$host_stat_tool" "$DPREFIX" > "$host_stat_before"'
                host_stat_after = '"$host_stat_tool" "$DPREFIX" > "$host_stat_after"'
                host_stat_dump = """if [ -f "$host_stat_before" ]; then
\tprintf '%s\\n' '--- host stat before ---' >&2
\tcat "$host_stat_before" >&2 || true
fi
if [ -f "$host_stat_after" ]; then
\tprintf '%s\\n' '--- host stat after ---' >&2
\tcat "$host_stat_after" >&2 || true
fi"""
                host_stat_check = """python3 - "$host_stat_specs" "$host_stat_before" "$host_stat_after" <<'PY'
import json
import sys

specs = json.loads(sys.argv[1])
with open(sys.argv[2], encoding="utf-8") as handle:
    before = json.load(handle)
with open(sys.argv[3], encoding="utf-8") as handle:
    after = json.load(handle)

def value_at(snapshot, path):
    current = snapshot
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    if not isinstance(current, (int, float)):
        raise TypeError(path)
    return current

failed = False
for spec in specs:
    path = str(spec["path"])
    minimum = float(spec.get("min-delta", 1))
    old = value_at(before, path)
    new = value_at(after, path)
    delta = new - old
    print(f"HOST_STAT_DELTA {path} {delta:g}")
    if delta < minimum:
        print(
            f"host stat delta too small for {path}: {delta:g} < {minimum:g}",
            file=sys.stderr,
        )
        failed = True
if failed:
    sys.exit(1)
PY"""
            needs_server_env_restart = bool(
                invocation.get("host_temp_files")
                or invocation.get("host_trace_files")
                or host_stat_deltas
            )
            server_env_restart = (
                "\"$launch\" shutdown >/dev/null 2>&1 || true"
                if needs_server_env_restart
                else ":"
            )
            guest_compile_body = f"""
{guest_prelude}
{guest_env_setup}
guest_cc={quote(invocation["guest_cc"])}
if [ ! -x "$guest_cc" ]; then guest_cc=clang; fi
{' '.join(compile_parts)}
compile_rc=$?
if [ "$compile_rc" -ne 0 ]; then
\tprintf 'ORACLE_RC=%s\\n' "$compile_rc"
\texit "$compile_rc"
fi
"""
            guest_run_body = f"""
{guest_prelude}
{guest_env_setup}
{' '.join(run_parts)}
run_rc=$?
printf 'ORACLE_RC=%s\\n' "$run_rc"
exit "$run_rc"
"""
            if host_stat_deltas:
                guest_workload = f"""
set +e
printf 'WEST_GUEST_STAGE=compile\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
compile_rc=$?
set -e
if [ "$compile_rc" -ne 0 ]; then
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi
{host_stat_before}
set +e
printf 'WEST_GUEST_STAGE=run\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_run_body)} >> "$verdict" 2>&1
rc=$?
set -e
{host_stat_after}
"""
            else:
                guest_workload = f"""
set +e
printf 'WEST_GUEST_STAGE=compile\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
compile_rc=$?
set -e
if [ "$compile_rc" -ne 0 ]; then
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi
set +e
printf 'WEST_GUEST_STAGE=run\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_run_body)} >> "$verdict" 2>&1
rc=$?
set -e
"""
            script = f"""#!/usr/bin/env bash
set -euo pipefail
: "${{DPREFIX:?set DPREFIX}}"
launch={quote(str(launcher))}
host_src={quote(str(invocation["script_path"]))}
verdict={quote(str(verdict))}
guest_src={quote(guest_src)}
guest_bin={quote(guest_bin)}
timeout_seconds={int(invocation.get("timeout_seconds", 600))}
ok_marker={quote(invocation["ok_marker"])}
host_trace_oracle={quote("1" if invocation.get("host_trace_oracle") else "0")}
prepare_only="${{WEST_GUEST_C_FIXTURE_PREPARE_ONLY:-0}}"
run_only="${{WEST_GUEST_C_FIXTURE_RUN_ONLY:-0}}"

{trace_setup}
{host_stat_setup}
{server_env_restart}

dump_namespace_state() {{
\tlocal init_pid_file="$DPREFIX/.init.pid"
\tlocal init_pid=""
\tif [ -r "$init_pid_file" ]; then
\t\tinit_pid="$(tr -d '[:space:]' < "$init_pid_file" || true)"
\t\tprintf 'WEST_GUEST_NAMESPACE_INIT_PID=%s\\n' "${{init_pid:-<empty>}}" >&2
\telse
\t\tprintf 'WEST_GUEST_NAMESPACE_INIT_PID=<missing>\\n' >&2
\tfi
\tif [ -n "$init_pid" ]; then
\t\tif [ -e "/proc/$init_pid/ns/mnt" ]; then
\t\t\tprintf 'WEST_GUEST_NAMESPACE_MNT=%s\\n' "$(readlink "/proc/$init_pid/ns/mnt" 2>/dev/null || printf '<unreadable>')" >&2
\t\telse
\t\t\tprintf 'WEST_GUEST_NAMESPACE_MNT=<missing:/proc/%s/ns/mnt>\\n' "$init_pid" >&2
\t\tfi
\tfi
}}

dump_file_sha() {{
\tlocal label="$1"
\tlocal path="$2"
\tif [ -e "$path" ]; then
\t\tsha256sum "$path" 2>/dev/null | sed "s#^#WEST_GUEST_FILE_SHA256 $label #; s#  # #g" >&2 || true
\telse
\t\tprintf 'WEST_GUEST_FILE_MISSING %s %s\\n' "$label" "$path" >&2
\tfi
}}

dump_runtime_file_state() {{
\tlocal launcher_dir install_root
\tlauncher_dir="$(dirname "$launch")"
\tinstall_root="$(cd "$launcher_dir/.." && pwd -P)"
\tdump_file_sha launcher "$launch"
\tdump_file_sha launcher_server "$launcher_dir/darlingserver"
\tdump_file_sha prefix_server "$DPREFIX/bin/darlingserver"
\tdump_file_sha install_mldr "$install_root/usr/libexec/darling/mldr"
\tdump_file_sha install_nested_mldr "$install_root/libexec/darling/usr/libexec/darling/mldr"
\tdump_file_sha prefix_mldr "$DPREFIX/usr/libexec/darling/mldr"
\tdump_file_sha prefix_nested_mldr "$DPREFIX/libexec/darling/usr/libexec/darling/mldr"
\tdump_file_sha install_dyld "$install_root/usr/lib/dyld"
\tdump_file_sha install_nested_dyld "$install_root/libexec/darling/usr/lib/dyld"
\tdump_file_sha install_libsystem_kernel "$install_root/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha install_nested_libsystem_kernel "$install_root/libexec/darling/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_libsystem_kernel "$DPREFIX/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_nested_libsystem_kernel "$DPREFIX/libexec/darling/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_dyld "$DPREFIX/usr/lib/dyld"
\tdump_file_sha prefix_nested_dyld "$DPREFIX/libexec/darling/usr/lib/dyld"
}}

dump_rpc_client_log() {{
\tlocal log=/tmp/dserver-client-rpc.log
\tif [ -s "$log" ]; then
\t\tprintf 'WEST_GUEST_RPC_CLIENT_LOG_BEGIN\\n' >&2
\t\ttail -80 "$log" >&2 || true
\t\tprintf 'WEST_GUEST_RPC_CLIENT_LOG_END\\n' >&2
\tfi
}}

dump_runtime_process_state() {{
\tlocal snapshot pid comm args exe found=0
\tsnapshot="$(mktemp /tmp/west-dserver-ps.XXXXXX)"
\tps -eo pid=,comm=,args= > "$snapshot" 2>/dev/null || true
\twhile read -r pid comm args; do
\t\tif [ "$comm" != "darlingserver" ]; then
\t\t\tcontinue
\t\tfi
\t\tcase "$args" in
\t\t\t*"$DPREFIX"*)
\t\t\t\tfound=1
\t\t\t\tprintf 'WEST_GUEST_DSERVER_PID=%s\\n' "$pid" >&2
\t\t\t\tprintf 'WEST_GUEST_DSERVER_ARGS=%s\\n' "$args" >&2
\t\t\t\texe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
\t\t\t\tprintf 'WEST_GUEST_DSERVER_EXE=%s\\n' "${{exe:-<unreadable>}}" >&2
\t\t\t\tsha256sum "/proc/$pid/exe" 2>/dev/null | sed 's/^/WEST_GUEST_DSERVER_EXE_SHA256=/' >&2 || true
\t\t\t\t;;
\t\tesac
\tdone < "$snapshot"
\tif [ "$found" -eq 0 ]; then
\t\twhile read -r pid comm args; do
\t\t\tif [ "$comm" != "darlingserver" ]; then
\t\t\t\tcontinue
\t\t\tfi
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_PID=%s\\n' "$pid" >&2
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_ARGS=%s\\n' "$args" >&2
\t\t\texe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_EXE=%s\\n' "${{exe:-<unreadable>}}" >&2
\t\t\tsha256sum "/proc/$pid/exe" 2>/dev/null | sed 's/^/WEST_GUEST_DSERVER_OTHER_EXE_SHA256=/' >&2 || true
\t\tdone < "$snapshot"
\tfi
\trm -f "$snapshot"
}}

clear_stale_init_pid() {{
\tlocal init_pid_file="$DPREFIX/.init.pid"
\tlocal init_pid=""
\tif [ ! -r "$init_pid_file" ]; then
\t\treturn 0
\tfi
\tinit_pid="$(tr -d '[:space:]' < "$init_pid_file" || true)"
\tif [ -z "$init_pid" ] || [ ! -e "/proc/$init_pid/ns/mnt" ]; then
\t\trm -f "$init_pid_file"
\tfi
}}

guest_shell() {{
\tlocal seconds="$1"
\tshift
\tlocal ns_log
\tlocal restore_errexit=0
\tcase "$-" in
\t\t*e*)
\t\t\trestore_errexit=1
\t\t\tset +e
\t\t\t;;
\tesac
\tclear_stale_init_pid
\tns_log="$(mktemp /tmp/west-guest-shell-stderr.XXXXXX)"
\ttimeout --kill-after=5 "$seconds" env DPREFIX="$DPREFIX" DARLING_PREFIX="$DPREFIX" "$launch" shell /bin/bash --login -c "$@" 2> "$ns_log"
\tlocal rc=$?
\tcat "$ns_log" >&2 || true
\tif [ "$rc" -ne 0 ] && grep -q 'Cannot open mnt namespace file' "$ns_log"; then
\t\tdump_namespace_state
\t\tprintf 'WEST_GUEST_STAGE=namespace-retry\\n' >&2
\t\t"$launch" shutdown >/dev/null 2>&1 || true
\t\tclear_stale_init_pid
\t\ttimeout --kill-after=5 "$seconds" env DPREFIX="$DPREFIX" DARLING_PREFIX="$DPREFIX" "$launch" shell /bin/bash --login -c "$@" 2> "$ns_log"
\t\trc=$?
\t\tcat "$ns_log" >&2 || true
\t\tif [ "$rc" -ne 0 ] && grep -q 'Cannot open mnt namespace file' "$ns_log"; then
\t\t\tdump_namespace_state
\t\tfi
\tfi
\tif [ "$rc" -ne 0 ]; then
\t\tdump_runtime_file_state
\t\tdump_runtime_process_state
\t\tdump_rpc_client_log
\t\tprintf 'WEST_GUEST_SHELL_RC=%s\\n' "$rc" >&2
\tfi
\trm -f "$ns_log"
\tif [ "$restore_errexit" -eq 1 ]; then
\t\tset -e
\tfi
\treturn "$rc"
}}

: > /tmp/dserver-client-rpc.log 2>/dev/null || true
dump_runtime_file_state
if [ "$run_only" != 1 ]; then
\tprintf 'WEST_GUEST_STAGE=cleanup\\n' >&2
\tguest_shell 10 "rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1 || true
\t"$launch" shutdown >/dev/null 2>&1 || true
\tclear_stale_init_pid
\tprintf 'WEST_GUEST_STAGE=upload\\n' >&2
\tguest_shell 10 "cat > '$guest_src'" < "$host_src"
fi

if [ "$prepare_only" = 1 ]; then
\tset +e
\tprintf 'WEST_GUEST_STAGE=compile\\n' >&2
\tguest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
\tcompile_rc=$?
\tset -e
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi

if [ "$run_only" = 1 ]; then
\tset +e
\tprintf 'WEST_GUEST_STAGE=run\\n' >&2
\tguest_shell "$timeout_seconds" {quote(guest_run_body)} > "$verdict" 2>&1
\trc=$?
\tset -e
else
\t{guest_workload}
fi

{trace_settle}
cat "$verdict" 2>/dev/null || true
if [ "$rc" -ne 0 ] && [ "$host_trace_oracle" != 1 ]; then
\t{trace_dump}
\t{host_stat_dump}
\texit "$rc"
fi
if [ "$host_trace_oracle" != 1 ]; then
\tgrep -q "^$ok_marker" "$verdict"
\tgrep -q '^ORACLE_RC=0$' "$verdict"
fi
{trace_check}
{host_stat_check}
"""
            host_runner.write_text(script)
            host_runner.chmod(0o755)
            child = dict(invocation)
            child.pop("guest_c_fixture", None)
            child.update(
                {
                    "key": f"guest-c-fixture-runner:{invocation['key']}",
                    "display": str(host_runner),
                    "cwd": invocation["cwd"],
                    "args": [str(host_runner)],
                    "shell": False,
                    "debug_timeout_seconds": int(invocation.get("timeout_seconds", 600)) + 15,
                }
            )
            result = subprocess.run(
                self._debug_runner_args(child),
                cwd=invocation["cwd"],
                env=run_env,
                shell=False,
                check=False,
            )
            return result.returncode

    def _run_guest_command_fixture(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = self._execution_env(invocation)
        if not run_env:
            run_env = os.environ.copy()

        prefix = run_env.get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: guest-command-fixture needs DPREFIX")
        launcher = (
            run_env.get("DARLING_LAUNCHER")
            or run_env.get("DARLING")
            or self._resolve_darling_launcher(prefix)
        )
        if not launcher:
            self.die(f"{invocation['name']}: guest-command-fixture needs a Darling launcher")

        guest_env_setup = "\n".join(
            f"export {key}={quote(value)}"
            for key, value in invocation.get("guest_env_vars", {}).items()
        ) or ":"
        guest_script = f"""set -u
{guest_env_setup}
{invocation["guest_command"]}
"""
        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        command = [
            "timeout",
            "--kill-after=5",
            str(timeout_seconds),
            "env",
            f"DPREFIX={prefix}",
            f"DARLING_PREFIX={prefix}",
            str(launcher),
            "shell",
            "/bin/bash",
            "--login",
            "-c",
            guest_script,
        ]
        with tempfile.TemporaryDirectory(prefix=f"west-guest-command-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            stdout_path = tempdir / "stdout.log"
            stderr_path = tempdir / "stderr.log"
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=invocation["cwd"],
                    env=run_env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=True,
                )
                try:
                    returncode = process.wait(timeout=timeout_seconds + 15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    returncode = process.wait()
                    stdout_file.flush()
                    stderr_file.flush()
                    output = stdout_path.read_text(errors="replace") + stderr_path.read_text(
                        errors="replace"
                    )
                    if output:
                        print(output, end="" if output.endswith("\n") else "\n")
                    expect = invocation.get("expect") or {}
                    if expect.get("returncode") == "timeout":
                        for needle in expect.get("output-contains", []):
                            if str(needle) not in output:
                                self.err(f"{invocation['name']}: output missing {needle!r}")
                                return 1
                        return 0
                    self.err(
                        f"{invocation['name']}: guest command watchdog timed out after "
                        f"{timeout_seconds + 15}s"
                    )
                    return 124
                stdout_file.flush()
                stderr_file.flush()

            output = stdout_path.read_text(errors="replace") + stderr_path.read_text(
                errors="replace"
            )
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        expect = invocation.get("expect") or {}
        rc_mode = expect.get("returncode", 0)
        if rc_mode == "timeout":
            self.err(f"{invocation['name']}: guest command returned before expected timeout")
            return 1
        if rc_mode == "any":
            pass
        elif rc_mode == "nonzero":
            if returncode == 0:
                self.err(f"{invocation['name']}: guest command succeeded unexpectedly")
                return 1
        elif returncode != int(rc_mode):
            self.err(
                f"{invocation['name']}: guest command rc {returncode}, "
                f"want {rc_mode}"
            )
            return 1
        for needle in expect.get("output-contains", []):
            if str(needle) not in output:
                self.err(f"{invocation['name']}: output missing {needle!r}")
                return 1
        for needle in expect.get("output-lacks", []):
            if str(needle) in output:
                self.err(f"{invocation['name']}: output unexpectedly contains {needle!r}")
                return 1
        return 0

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
                subprocess.run(compile_args, cwd=tools_dir, check=True)
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
                subprocess.run(build_args, cwd=tools_dir, check=True)
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
            archive = subprocess.Popen(
                ["git", "archive", "--format=tar", source_ref, tools_dir_name],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert archive.stdout is not None
            extract = subprocess.run(
                ["tar", "-C", str(source_root), "-xf", "-"],
                stdin=archive.stdout,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            archive.stdout.close()
            _, archive_stderr = archive.communicate()
            if archive.returncode or extract.returncode:
                detail = (archive_stderr or "") + (extract.stderr or "")
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
        result = subprocess.run(
            [
                "timeout",
                "--kill-after=5",
                "15",
                str(launcher),
                "shell",
                "/bin/bash",
                "--login",
                "-c",
                ":",
            ],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
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
            result = subprocess.run(
                [
                    "timeout",
                    "--kill-after=5",
                    "15",
                    str(launcher),
                    "shell",
                    "/bin/bash",
                    "--login",
                    "-c",
                    script,
                ],
                env=child_env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
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
        except Exception:
            keep_on_failure = True
            self.err(f"preserving failed GREEN source tree for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(target)],
                    cwd=module_repo,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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
            if patch_module_is_darling_root:
                darling_ref = bad_revision if omit_patch else "HEAD"
            else:
                darling_ref = "HEAD" if current_minus_patch else self._manifest_revision("darling")
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
            if patch_module_is_darling_root:
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
        except Exception:
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

    def _runtime_build_install_prefix(self, proof, prefix: Path) -> Path:
        for artifact in proof.get("runtime-artifacts", []):
            if "bin/darlingserver" not in artifact.get("deploy", []):
                continue
            launcher = self._resolve_darling_launcher(str(prefix))
            if launcher:
                return Path(launcher).resolve().parent.parent
        return prefix

    def _runtime_red_configure_args(self, proof, prefix: Path) -> list[str]:
        current_build = Path(
            os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build"))
        )
        args = ["-G", self._cmake_cache_value(current_build, "CMAKE_GENERATOR") or "Ninja"]
        for key in ("CMAKE_BUILD_TYPE", "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"):
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
            "DARLING_SKIP_DRIFT_GATE",
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
        for key, value in sorted((proof.get("cmake-defines") or {}).items()):
            if isinstance(value, bool):
                value_text = "ON" if value else "OFF"
            elif value is None:
                value_text = ""
            else:
                value_text = str(value)
            args.append(f"-D{key}={value_text}")
        args.append(f"-DCMAKE_INSTALL_PREFIX={self._runtime_build_install_prefix(proof, prefix)}")
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
        self.inf(f"  {label} configure: {source_root} -> {build_root}")
        configure = subprocess.run(
            ["cmake", "-S", str(source_root), "-B", str(build_root), *self._runtime_red_configure_args(proof, prefix)],
            cwd=self.topdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if configure.returncode:
            self._dump_command_tail(f"{label} configure", configure)
            configure.check_returncode()
        self.inf(f"  {label} build: {', '.join(targets)}")
        build = subprocess.run(
            ["ninja", "-C", str(build_root), *targets],
            cwd=self.topdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode:
            self._dump_command_tail(f"{label} build", build)
            build.check_returncode()
        return build_root

    def _dump_command_tail(self, label: str, result: subprocess.CompletedProcess) -> None:
        streams = [stream for stream in (result.stdout, result.stderr) if stream]
        output = "\n".join(stream.rstrip("\n") for stream in streams)
        tail = "\n".join(output.splitlines()[-200:])
        if tail:
            sys.stderr.write(tail + "\n")
        self.err(f"{label} failed with rc {result.returncode}")

    def _runtime_red_find_build_output(self, build_root: Path, deploy_path: str) -> Path:
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

    def _runtime_red_deploy_targets(self, prefix: Path, deploy_path: str) -> list[Path]:
        try:
            targets = runtime_deploy_targets(prefix, deploy_path)
        except ValueError:
            self.die(f"guest-runtime-deploy deploy path must be relative: {deploy_path}")
        if deploy_path == "bin/darlingserver":
            launcher = self._resolve_darling_launcher(str(prefix))
            if launcher:
                server = Path(launcher).with_name("darlingserver")
                if server not in targets:
                    targets.append(server)
        if deploy_path in {
            "usr/libexec/darling/mldr",
            "usr/libexec/darling/mldr32",
            "usr/lib/dyld",
            "usr/lib/system/libsystem_kernel.dylib",
        }:
            targets.extend(
                target
                for target in self._runtime_red_launcher_install_targets(prefix, deploy_path)
                if target not in targets
            )
        return targets

    def _runtime_red_launcher_install_targets(self, prefix: Path, deploy_path: str) -> list[Path]:
        launcher = self._resolve_darling_launcher(str(prefix))
        if not launcher:
            return []
        install_root = Path(launcher).resolve().parent.parent
        rel = Path(deploy_path)
        return [
            install_root / rel,
            install_root / "libexec/darling" / rel,
        ]

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
    ):
        backups: list[tuple[Path, Path | None]] = []
        with tempfile.TemporaryDirectory(prefix="west-red-proof-deploy-") as temp:
            backup_root = Path(temp)
            if not self._shutdown_runtime_prefix(prefix):
                self.die(f"guest-runtime-deploy could not stop Darling prefix before deploy: {prefix}")
            try:
                for artifact in proof.get("runtime-artifacts", []):
                    for deploy_path in artifact.get("deploy", []):
                        src = self._runtime_red_find_build_output(build_root, deploy_path)
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
            finally:
                if not self._shutdown_runtime_prefix(prefix):
                    self.err(f"guest-runtime-deploy could not stop Darling prefix before restore: {prefix}")
                for dst, backup in reversed(backups):
                    if backup is None:
                        try:
                            dst.unlink()
                        except FileNotFoundError:
                            pass
                    else:
                        self._runtime_replace_file(backup, dst)
                self._shutdown_runtime_prefix(prefix)

    def _shutdown_runtime_prefix(self, prefix: Path) -> bool:
        launcher = self._resolve_darling_launcher(str(prefix))
        if launcher:
            env = os.environ.copy()
            env.update(self._darling_prefix_env(prefix))
            try:
                subprocess.run(
                    [launcher, "shutdown"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=int(os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", "15")),
                )
            except subprocess.TimeoutExpired:
                self.err(f"Darling prefix shutdown timed out for {prefix}; forcing cleanup")
        self._kill_dserver_for_prefix(prefix)
        leftovers = self._prefix_process_snapshot(prefix)
        if leftovers:
            self.err(f"leftover Darling prefix process(es) for {prefix}:")
            for entry in leftovers:
                self.err(f"  {entry}")
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
        except Exception:
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

    def _check_guest_runtime_red_failure(self, proof, invocation, *, since: float) -> bool:
        contains, lacks = self._red_output_expectations(proof)
        if not contains and not lacks:
            return True

        bundle = self._latest_debug_bundle(invocation, since=since)
        if bundle is None:
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
                        bad_rc = self._run_invocation(runtime_invocation, env=resource_env)
                    if bad_rc == 0:
                        self.err("  RED proof failed: deployed bad runtime unexpectedly passed")
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return 1
                    if not self._check_guest_runtime_red_failure(
                        proof,
                        runtime_invocation,
                        since=red_started_at,
                    ):
                        keep_on_failure = True
                        self.err(f"preserving failed RED runtime scratch for inspection: {temp}")
                        return 1
                    self.inf(f"  RED runtime failed as expected (rc={bad_rc})")
        except Exception:
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
                    bad_rc, bad_output = self._run_invocation_captured(proof_invocation, env=bad_env)
                    if bad_rc == 0:
                        self.err("  RED proof failed: source-base run unexpectedly passed")
                        return 1
                    if not self._check_red_output_expectations(
                        proof,
                        proof_invocation,
                        bad_output,
                        where="in source-base RED output",
                    ):
                        return 1
                    self.inf(f"  RED path failed as expected (rc={bad_rc})")
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
                        result_rc = self._run_invocation(invocation, env=exec_env)
            if result_rc:
                rc = result_rc
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

    def _runtime_red_reason_audit(self, tests) -> list[str]:
        missing = []
        for patch, test in tests:
            proof = test.get("red-proof")
            if not isinstance(proof, dict):
                continue
            if proof.get("mode") != "guest-runtime-deploy":
                continue
            if not self._guest_runtime_red_has_positive_reason(proof):
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} "
                    "guest-runtime-deploy RED proof needs expect-output-contains"
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
            subprocess.run(
                [launcher, "shutdown"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
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
        return prefix_process_snapshot(prefix, self._ps_entries())

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
    ) -> Path:
        build = testkit / "build"
        cfg = ["cmake", "-S", str(testkit), "-B", str(build), "-G", "Ninja"]
        if executor:
            cfg.append(f"-DDARLING_TEST_EXECUTOR={executor}")
        if darling_launcher:
            cfg.append(f"-DDARLING_LAUNCHER={darling_launcher}")
        if prefix:
            cfg.append(f"-DDARLING_TEST_PREFIX={prefix}")
        self.inf(f"configuring: {testkit}")
        subprocess.run(cfg, check=True)
        subprocess.run(["ninja", "-C", str(build)], check=True)
        return build

    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

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

    # --- entrypoint ---------------------------------------------------------

    def do_run(self, args, unknown):
        self._prefix = self._resolve_prefix(args)
        self._executor = self._resolve_executor(args.executor)
        self._bundle_root = str(Path(args.bundle_root).expanduser())
        self._materialize_profile = args.materialize_profile
        self._keep_prefix_running = args.keep_prefix_running

        if args.gc:
            self._gc_bundles(
                Path(args.bundle_root), args.keep_last, args.max_bundle_mb,
                dry_run=args.dry_run,
            )
            return

        if args.red_audit:
            profile = args.profile or "homebrew"
            selected, missing = self._metadata_tests(
                profile, args.patch, args.bead, args.env, args.diag, red_only=False
            )
            missing_reasons = self._runtime_red_reason_audit(selected)
            for patch in missing:
                self.inf(f"MISSING {patch['path']} [{patch.get('bead', '-')}]")
            for message in missing_reasons:
                self.inf(f"RED-REASON-MISSING {message}")
            self.inf(f"red-audit: {len(missing)} patch(es) missing tests/exception")
            self.inf(
                "red-audit: "
                f"{len(missing_reasons)} guest-runtime-deploy proof(s) missing RED reason"
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
            self.die(
                "env:darling CTest runs need a Darling launcher; pass --prefix, "
                "set DARLING/DARLING_LAUNCHER, or install ~/work/darling-prefix/bin/darling"
            )
        build = self._configure_and_build(
            testkit,
            self._executor,
            darling_launcher=launcher,
            prefix=self._prefix,
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

        self.inf(f"running: {' '.join(ctest)}")
        raise SystemExit(subprocess.run(ctest, check=False).returncode)
