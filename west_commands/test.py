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
import os
import signal
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from shlex import quote

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
            "--materialize-profile",
            action="store_true",
            help="temporarily switch requires-profile tests to integration/<profile> branches and restore afterward",
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

    def _project_path(self, ref: str) -> Path:
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
        args = self._debug_runner_args(invocation, display_only=True)
        return " ".join(quote(str(arg)) for arg in args)

    def _run_invocation(self, invocation, env=None) -> int:
        result = subprocess.run(
            self._debug_runner_args(invocation),
            cwd=invocation["cwd"],
            env=env if env is not None else invocation.get("env"),
            shell=False,
            check=False,
        )
        return result.returncode

    def _execution_env(self, invocation) -> dict[str, str] | None:
        env = invocation.get("env")
        if "darling-prefix" not in invocation.get("requires_resources", []):
            return env
        prefix = getattr(self, "_prefix", None)
        if not prefix:
            return env
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged["DPREFIX"] = prefix
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

    def _run_source_base_proof(self, patch, proof, invocation) -> int:
        if invocation["shell"]:
            self.die(f"{patch['path']}: source-base proof requires a structured runner")
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
            if mode not in {"self", "source-base"}:
                self.die(
                    f"{patch['path']}: RED proof mode {mode!r} is not implemented; "
                    "use mode: self or mode: source-base"
                )
            script_path = invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                self.die(f"{patch['path']}: test script not found: {script_path}")
            missing_env = self._missing_requirements(invocation)
            if missing_env:
                self.die(
                    f"{patch['path']}: missing required environment for {name}: "
                    f"{', '.join(missing_env)}"
                )
            if invocation["key"] in seen_invocations:
                self.inf("  skipped duplicate invocation already run")
                continue
            seen_invocations.add(invocation["key"])
            with self._required_profile_context(patch, invocation):
                if mode == "source-base":
                    result_rc = self._run_source_base_proof(patch, proof, invocation)
                else:
                    result_rc = self._run_invocation(invocation, env=self._execution_env(invocation))
            if result_rc:
                rc = result_rc
        return rc

    def _shutdown_test_prefix(self) -> bool:
        prefix = getattr(self, "_prefix", None)
        if not prefix or getattr(self, "_keep_prefix_running", False):
            return True
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            env = os.environ.copy()
            env["DPREFIX"] = prefix
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
