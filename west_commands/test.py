"""Darling workspace test orchestrator.

`west test` is a thin layer over CTest, in the same spirit as gVisor's Bazel
test targets and Wine's winetest: the runner sits ON TOP of the build system,
it does not reinvent discovery/parallelism/JUnit/WILL_FAIL. CTest owns those.

This command adds the three things CTest does not give for free in this repo:

  --changed   map changed submodules (from the west manifest + git diff) to the
              `submod:<name>` CTest labels, so a quick local cycle runs only the
              tests a PR could affect.
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

import yaml
from west.commands import WestCommand


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
        return yaml.safe_load(path.read_text()) or {}

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
                            ["git", "am", "--3way", str(patch_file)],
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
        for patch in data.get("patches", []):
            if patch_path and patch["path"] != patch_path:
                continue
            if bead and patch.get("bead") != bead:
                continue
            all_tests = patch.get("tests") or []
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
        if patch_path and not selected and not missing:
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
        if test.get("ctest-label"):
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
                "diag": "bare",
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
        if runner == "script":
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
                    f"{repr(sorted((test.get('generated-headers') or {}).keys()))}"
                ),
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
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
            run_binary = str(test.get("run-binary", f"{source_dir}/{target}"))
            display = (
                f"cd {quote(repo)} && <darling-cmake-target-fixture> "
                f"cmake -S <superproject> -B <temp>/build "
                f"{shell_join(cmake_args)} && "
                f"cmake --build <temp>/build --target {quote(target)} "
                f"{shell_join(build_args)} && "
                f"<temp>/build/{quote(run_binary)}"
            )
            return {
                "key": (
                    f"darling-cmake-target-fixture:{repo}:{target}:"
                    f"{source_dir}:{run_binary}:"
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
                "diag": self._resolved_diag(test),
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
            ok_marker = test.get("ok-marker")
            if not ok_marker:
                self.die(f"{patch['path']}: guest-c-fixture needs ok-marker")
            display = (
                f"cd {quote(repo)} && <upload> {quote(script)} && "
                f"darling shell {quote(guest_cc)} {guest_cflags} "
                f"{shell_join(compile_flags)} -o /tmp/{quote(name)} /tmp/{quote(name)}.c "
                f"{shell_join(link_flags)} && darling shell /tmp/{quote(name)} "
                f"{shell_join(run_args)}"
            )
            return {
                "key": f"guest-c-fixture:{repo}:{script}",
                "display": display,
                "cwd": cwd,
                "script_path": script_path,
                "args": None,
                "shell": False,
                "env": env,
                "guest_c_fixture": True,
                "guest_cc": guest_cc,
                "guest_cflags": guest_cflags,
                "guest_prelude": str(test.get("guest-prelude", "")),
                "compile_flags": compile_flags,
                "link_flags": link_flags,
                "run_args": run_args,
                "ok_marker": str(ok_marker),
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": sorted(resources),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": name,
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
            }

        self.die(f"{patch['path']}: unsupported test runner {runner!r}")

    def _run_metadata_tests(self, tests, list_only: bool, unknown: list[str]) -> int:
        if unknown:
            self.die("metadata command tests do not accept raw ctest passthrough arguments")
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
                result_rc = self._run_invocation(invocation, env=self._execution_env(invocation))
            if result_rc:
                rc = result_rc
        return rc

    def _metadata_needs_prefix(self, tests) -> bool:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            if "darling-prefix" in invocation.get("requires_resources", []):
                return True
        return False

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
        args = ["ctest", "--test-dir", str(build), "--output-on-failure", "-L", label]
        return " ".join(quote(str(arg)) for arg in args)

    def _ensure_ctest_build(self) -> Path:
        build = getattr(self, "_ctest_build", None)
        if build is not None:
            return build
        build = self._configure_and_build(self._testkit_dir(), self._executor)
        self._ctest_build = build
        return build

    def _ctest_label_args(self, invocation) -> list[str]:
        return [
            "ctest",
            "--test-dir",
            str(self._ensure_ctest_build()),
            "--output-on-failure",
            "-L",
            invocation["ctest_label"],
        ]

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
            str(invocation.get("timeout_seconds", 600)),
        ]
        if diag == "forensic":
            args.extend(["--capture-gdb", "--capture-tree"])
        args.append("--")
        args.extend(self._wrapped_args(invocation))
        return args

    def _display_invocation(self, invocation) -> str:
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
                str(invocation.get("timeout_seconds", 600)),
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
            return self._run_darling_cmake_target_fixture(invocation, env=env)
        result = subprocess.run(
            self._debug_runner_args(invocation),
            cwd=invocation["cwd"],
            env=env if env is not None else invocation.get("env"),
            shell=False,
            check=False,
        )
        return result.returncode

    def _run_c_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: c-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
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

    def _archive_source_to(self, source_root: Path, destination: Path) -> int:
        destination.mkdir(parents=True, exist_ok=True)
        archive = subprocess.Popen(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=source_root,
            stdout=subprocess.PIPE,
        )
        try:
            tar = subprocess.run(
                ["tar", "-C", str(destination), "-xf", "-"],
                stdin=archive.stdout,
                check=False,
            )
        finally:
            if archive.stdout is not None:
                archive.stdout.close()
        archive_rc = archive.wait()
        return archive_rc or tar.returncode

    def _write_darling_cmake_superproject(self, project_root: Path, invocation) -> None:
        source_dir = invocation["source_dir"]
        target = invocation["target"]
        fallback_sources = invocation.get("fallback_executable_sources", [])
        if not fallback_sources:
            fallback_sources = [f"{source_dir}/tests/{target}.c"]
        fallback_include_dirs = invocation.get("fallback_include_dirs", [])
        fallback_link_libraries = invocation.get("fallback_link_libraries", [])
        fallback_source_lines = "\n    ".join(fallback_sources)
        fallback_include_lines = " ".join(fallback_include_dirs)
        fallback_link_lines = " ".join(fallback_link_libraries)
        cmake = f"""cmake_minimum_required(VERSION 3.16)
project(west_darling_cmake_target_fixture C)

set(BUILD_TARGET_64BIT ON)
set(BUILD_TARGET_32BIT OFF)

function(_west_darling_collect_sources out_var)
    set(srcs)
    foreach(arg IN LISTS ARGN)
        if(arg STREQUAL FAT OR arg STREQUAL SOURCES OR arg STREQUAL 32BIT_ONLY OR arg STREQUAL 64BIT_ONLY)
            continue()
        endif()
        list(APPEND srcs ${{arg}})
    endforeach()
    set(${{out_var}} ${{srcs}} PARENT_SCOPE)
endfunction()

function(add_darling_static_library name)
    _west_darling_collect_sources(srcs ${{ARGN}})
    add_library(${{name}} STATIC ${{srcs}})
endfunction()

function(add_darling_object_library name)
    _west_darling_collect_sources(srcs ${{ARGN}})
    add_library(${{name}} OBJECT ${{srcs}})
endfunction()

function(add_darling_library name)
    _west_darling_collect_sources(srcs ${{ARGN}})
    add_library(${{name}} STATIC ${{srcs}})
endfunction()

function(add_darling_executable name)
    add_executable(${{name}} ${{ARGN}})
endfunction()

add_library(system STATIC system_shim.c)
add_subdirectory({source_dir})

if(NOT TARGET {target})
    add_executable({target}
    {fallback_source_lines}
    )
    if(NOT "{fallback_include_lines}" STREQUAL "")
        target_include_directories({target} PRIVATE {fallback_include_lines})
    endif()
    if(NOT "{fallback_link_lines}" STREQUAL "")
        target_link_libraries({target} {fallback_link_lines})
    endif()
endif()

set_target_properties({target} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${{CMAKE_BINARY_DIR}}/{source_dir}"
)
target_link_libraries({target} system)
"""
        shim = """#include <stddef.h>

int
timingsafe_bcmp(const void *b1, const void *b2, size_t n)
{
	const unsigned char *p1 = b1;
	const unsigned char *p2 = b2;
	unsigned char result = 0;

	for (size_t i = 0; i < n; i++)
		result |= p1[i] ^ p2[i];
	return result != 0;
}
"""
        (project_root / "CMakeLists.txt").write_text(cmake)
        (project_root / "system_shim.c").write_text(shim)

    def _write_cmake_compiler_launcher(self, launcher: Path, log_path: Path) -> None:
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import subprocess\n"
            "import sys\n"
            f"with open({str(log_path)!r}, 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "sys.exit(subprocess.call(sys.argv[1:]))\n"
        )
        launcher.chmod(0o755)

    def _check_required_compile_options(self, invocation, log_path: Path) -> int:
        checks = invocation.get("required_compile_options", [])
        if not checks:
            return 0
        if not log_path.is_file():
            self.err(f"{invocation['name']}: compiler launcher log not found")
            return 1
        entries = []
        for line in log_path.read_text().splitlines():
            try:
                args = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(args, list):
                entries.append([str(arg) for arg in args])
        for check in checks:
            source = check["source"]
            options = check.get("options", [])
            matches = [
                args
                for args in entries
                if any(arg == source or arg.endswith(f"/{source}") for arg in args)
            ]
            if not matches:
                self.err(
                    f"{invocation['name']}: no compile command recorded for {source}"
                )
                return 1
            if not any(all(option in args for option in options) for args in matches):
                self.err(
                    f"{invocation['name']}: compile command for {source} missing "
                    f"option(s): {', '.join(options)}"
                )
                return 1
        return 0

    def _run_darling_cmake_target_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: darling-cmake-target-fixture currently supports diag:bare only")
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

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        with tempfile.TemporaryDirectory(prefix=f"west-darling-cmake-target-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            project_root = tempdir / "project"
            source_copy = project_root / invocation["source_dir"]
            build_dir = tempdir / "build"
            bin_dir = tempdir / "bin"
            compile_log = tempdir / "compile-commands.jsonl"
            rc = self._archive_source_to(source_root, source_copy)
            if rc:
                return rc
            for fixture in invocation.get("fixture_files", []):
                source_fixture = source_copy / fixture
                if source_fixture.is_file():
                    continue
                current_fixture = invocation["cwd"] / fixture
                if not current_fixture.is_file():
                    self.err(f"{invocation['name']}: fixture file not found: {current_fixture}")
                    return 1
                source_fixture.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current_fixture, source_fixture)

            self._write_darling_cmake_superproject(project_root, invocation)
            if invocation.get("required_compile_options"):
                bin_dir.mkdir()
                launcher = bin_dir / "west-c-compiler-launcher"
                self._write_cmake_compiler_launcher(launcher, compile_log)
            cmake_args = list(invocation.get("cmake_args", []))
            if invocation.get("required_compile_options"):
                cmake_args.append(f"-DCMAKE_C_COMPILER_LAUNCHER={launcher}")
            configure_args = [
                "cmake",
                "-S",
                str(project_root),
                "-B",
                str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                *cmake_args,
            ]
            build_args = [
                "cmake",
                "--build",
                str(build_dir),
                "--target",
                invocation["target"],
                "-j",
                str(os.environ.get("WEST_TEST_BUILD_JOBS", "2")),
                *invocation.get("build_args", []),
            ]
            commands = [
                ("cmake configure", configure_args),
                ("cmake build", build_args),
                (
                    "run target",
                    [str(build_dir / invocation["run_binary"])],
                ),
            ]
            for label, command in commands:
                self.inf(f"  darling-cmake-target-fixture: {label}")
                try:
                    result = subprocess.run(
                        command,
                        cwd=project_root,
                        env=run_env,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    self.err(
                        f"{invocation['name']}: {label} timed out after "
                        f"{timeout_seconds}s"
                    )
                    return 124
                if result.returncode:
                    output = result.stdout + result.stderr
                    tail = "\n".join(output.splitlines()[-160:])
                    if tail:
                        sys.stderr.write(tail + "\n")
                    self.err(
                        f"{invocation['name']}: {label} failed with rc "
                        f"{result.returncode}"
                    )
                    return result.returncode
            rc = self._check_required_compile_options(invocation, compile_log)
            if rc:
                return rc
            return 0

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
            rc = self._archive_source_to(source_root, build_root)
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
            run_id = f"{os.getpid()}.{int(time.time() * 1000)}"
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

guest_shell() {{
\tlocal seconds="$1"
\tshift
\ttimeout --kill-after=5 "$seconds" env DPREFIX="$DPREFIX" "$launch" shell /bin/bash --login -c "$@"
}}

guest_shell 10 "rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1 || true
guest_shell 10 "cat > '$guest_src'" < "$host_src"

set +e
guest_shell "$timeout_seconds" {quote(f'''
{guest_prelude}
guest_cc={quote(invocation["guest_cc"])}
if [ ! -x "$guest_cc" ]; then guest_cc=clang; fi
{' '.join(compile_parts)}
compile_rc=$?
if [ "$compile_rc" -ne 0 ]; then
\tprintf 'ORACLE_RC=%s\\n' "$compile_rc"
\texit "$compile_rc"
fi
{' '.join(run_parts)}
run_rc=$?
printf 'ORACLE_RC=%s\\n' "$run_rc"
exit "$run_rc"
''')} > "$verdict" 2>&1
rc=$?
set -e

cat "$verdict" 2>/dev/null || true
if [ "$rc" -ne 0 ]; then
\texit "$rc"
fi
grep -q "^$ok_marker" "$verdict"
grep -q '^ORACLE_RC=0$' "$verdict"
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

    def _execution_env(self, invocation) -> dict[str, str] | None:
        env = invocation.get("env")
        needs_prefix = "darling-prefix" in invocation.get("requires_resources", [])
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
        merged["DPREFIX"] = prefix
        merged.update(getattr(self, "_prefix_env", {}))
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            merged["DARLING"] = launcher
            merged["DARLING_LAUNCHER"] = launcher
        return merged

    def _missing_requirements(self, invocation) -> list[str]:
        missing = [
            env_name
            for env_name in invocation.get("requires_env", [])
            if not os.environ.get(env_name)
        ]
        if (
            "darling-prefix" in invocation.get("requires_resources", [])
            and not getattr(self, "_prefix", None)
        ):
            missing.append("darling-prefix (--prefix, --prefix-profile, or DPREFIX)")
        if "darling-prefix" in invocation.get("requires_resources", []):
            launcher = self._resolve_darling_launcher(getattr(self, "_prefix", None))
            if not launcher:
                missing.append(
                    "darling-launcher (DARLING, DARLING_LAUNCHER, "
                    "prefix/bin/darling, or ~/work/darling-prefix/bin/darling)"
                )
        return missing

    def _check_requires_profile(self, patch, invocation) -> None:
        required = invocation.get("requires_profile")
        if not required:
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
        with self._profile_worktree_checkout(profile):
            yield

    def _reject_guest_source_base_red_proof(self, patch) -> None:
        self.die(
            f"{patch['path']}: guest-c-fixture cannot use source-base RED proof "
            "because it would run against the already deployed Darling prefix. "
            "Use a GREEN-only guest gate or add an isolated bad/fixed deploy runner."
        )

    def _reject_guest_runtime_deploy_red_proof_not_implemented(self, patch) -> None:
        self.die(
            f"{patch['path']}: guest-runtime-deploy RED proof is declared but "
            "the isolated bad/fixed Darling runtime deploy runner is not implemented yet. "
            "Track this under dar-facb; do not fall back to source-base or a "
            "current-prefix guest smoke."
        )

    def _run_source_base_proof(self, patch, proof, invocation) -> int:
        if invocation["shell"]:
            self.die(f"{patch['path']}: source-base proof requires a structured runner")
        if invocation.get("guest_c_fixture"):
            self._reject_guest_source_base_red_proof(patch)
        source_env = proof.get("source-env")
        if not source_env:
            self.die(f"{patch['path']}: source-base proof needs red-proof.source-env")
        script_path = invocation.get("script_path")
        if script_path is not None and not script_path.is_file():
            self.die(f"{patch['path']}: test script not found: {script_path}")

        module_repo = self._project_path(proof.get("source-module", patch["module"]))
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
                exec_env = self._execution_env(invocation)
                if exec_env:
                    bad_env.update(exec_env)
                bad_env[source_env] = str(worktree)
                self.inf(f"  RED source tree: {bad_revision} via {source_env}={worktree}")
                bad_rc = self._run_invocation(invocation, env=bad_env)
                if bad_rc == 0:
                    self.err("  RED proof failed: source-base run unexpectedly passed")
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

        self.inf("  GREEN current tree")
        return self._run_invocation(invocation, env=self._execution_env(invocation))

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
            if list_only:
                continue
            if mode not in {"self", "source-base", "guest-runtime-deploy"}:
                self.die(
                    f"{patch['path']}: RED proof mode {mode!r} is not implemented; "
                    "use mode: self, source-base, or guest-runtime-deploy"
                )
            if mode == "guest-runtime-deploy":
                self._reject_guest_runtime_deploy_red_proof_not_implemented(patch)
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
                else:
                    result_rc = self._run_invocation(invocation, env=self._execution_env(invocation))
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

    def _reject_unimplemented_red_proof_models(self, tests) -> None:
        for patch, test in tests:
            proof = test.get("red-proof")
            if isinstance(proof, dict) and proof.get("mode") == "guest-runtime-deploy":
                self._reject_guest_runtime_deploy_red_proof_not_implemented(patch)

    def _shutdown_test_prefix(self) -> bool:
        prefix = getattr(self, "_prefix", None)
        if not prefix or getattr(self, "_keep_prefix_running", False):
            return True
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            env = os.environ.copy()
            env["DPREFIX"] = prefix
            env.update(getattr(self, "_prefix_env", {}))
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
        if not leftovers:
            return True
        self.err(f"leftover Darling prefix process(es) after cleanup for {prefix}:")
        for entry in leftovers:
            self.err(f"  {entry}")
        return False

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
        entries = self._ps_entries()
        children: dict[int, list[int]] = {}
        args_by_pid: dict[int, str] = {}
        roots: list[int] = []
        for pid, ppid, args in entries:
            args_by_pid[pid] = args
            children.setdefault(ppid, []).append(pid)
            argv = args.split()
            if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == str(prefix):
                roots.append(pid)
        if not roots:
            return []
        seen: set[int] = set()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            stack.extend(children.get(pid, []))
        return [f"{pid} {args_by_pid[pid]}" for pid in sorted(seen) if pid in args_by_pid]

    def _kill_dserver_for_prefix(self, prefix: Path) -> None:
        pids: list[int] = []
        for pid, _, args in self._ps_entries():
            argv = args.split()
            if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == str(prefix):
                pids.append(pid)
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

    def _configure_and_build(self, testkit: Path, executor: str | None) -> Path:
        build = testkit / "build"
        cfg = ["cmake", "-S", str(testkit), "-B", str(build), "-G", "Ninja"]
        if executor:
            cfg.append(f"-DDARLING_TEST_EXECUTOR={executor}")
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
            _, missing = self._metadata_tests(
                profile, args.patch, args.bead, args.env, args.diag, red_only=False
            )
            for patch in missing:
                self.inf(f"MISSING {patch['path']} [{patch.get('bead', '-')}]")
            self.inf(f"red-audit: {len(missing)} patch(es) missing tests/exception")
            return

        if args.patch and not args.profile:
            self.die("--patch requires --profile")

        if args.profile:
            selected, missing = self._metadata_tests(
                args.profile, args.patch, args.bead, args.env, args.diag, args.red_only
            )
            if args.prove_red:
                self._reject_unsupported_red_proof_models(selected)
                if not args.list:
                    self._reject_unimplemented_red_proof_models(selected)
            materialize_was_requested = self._materialize_profile
            if (
                selected
                and not args.list
                and not self._materialize_profile
                and not self._profile_is_applied(args.profile)
                and (args.prove_red or self._metadata_needs_profile_worktree(selected))
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
                            selected = [
                                (patch, test)
                                for patch, test in selected
                                if test.get("red") or test.get("red-proof")
                            ]
                            if not selected:
                                self.die("no red-proof tests selected from patch metadata")
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

        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f"no testkit at {testkit}")

        build = self._configure_and_build(testkit, self._executor)

        # Translate selectors into a CTest label regex (-L is ANDed per flag).
        label_args: list[str] = []
        if args.bead:
            label_args += ["-L", f"bead:{args.bead}"]
        if args.env:
            label_args += ["-L", f"env:{args.env}"]
        if args.diag:
            label_args += ["-L", f"diag:{args.diag}"]
        if args.label:
            label_args += ["-L", args.label]
        if args.changed:
            changed = self._changed_submodules()
            if not changed:
                self.inf("no changed submodules; nothing selected by --changed")
                return
            alternation = "|".join(f"submod:{name}" for name in changed)
            label_args += ["-L", alternation]
            self.inf(f"changed submodules: {', '.join(changed)}")

        ctest = ["ctest", "--test-dir", str(build), "--output-on-failure"]
        ctest += label_args
        if args.list:
            ctest.append("--show-only")
        ctest += unknown  # pass through e.g. -j, --repeat, --output-junit

        self.inf(f"running: {' '.join(ctest)}")
        raise SystemExit(subprocess.run(ctest, check=False).returncode)
