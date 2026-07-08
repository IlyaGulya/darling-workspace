"""Darling workspace test orchestrator (PoC).

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

It is a PoC: the test tree lives under testkit/ and is configured/built here.
The real version would discover per-submodule test trees from the manifest.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
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
                tests = [test for test in tests if test.get("diag") == diag]
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
            }
        if test.get("ctest-label"):
            return {
                "key": f"ctest-label:{test['ctest-label']}",
                "display": f"ctest-label:{test['ctest-label']}",
                "cwd": None,
                "args": None,
                "shell": False,
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
                "requires_env": list(test.get("requires-env", [])),
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
            diag = test.get("diag", "-")
            kind = test.get("kind", "-")
            red = "red" if test.get("red") else "non-red"
            invocation = self._test_invocation(patch, test)
            self.inf(
                f"{patch['path']}: {name} [{red}, env:{env}, diag:{diag}, kind:{kind}]"
            )
            self.inf(f"  {invocation['display']}")
            if list_only:
                continue
            if test.get("ctest-label"):
                self.die(
                    f"{patch['path']}: ctest-label metadata is list-only until "
                    "profile test-tree discovery is implemented; use runner: script "
                    "or runner: west-build for runnable local metadata"
                )
            script_path = invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                self.die(f"{patch['path']}: test script not found: {script_path}")
            missing_env = [
                env_name
                for env_name in invocation.get("requires_env", [])
                if not os.environ.get(env_name)
            ]
            if missing_env:
                self.die(
                    f"{patch['path']}: missing required environment for {test.get('name', '-')}: "
                    f"{', '.join(missing_env)}"
                )
            if invocation["key"] in seen_invocations:
                self.inf(f"  skipped duplicate invocation already run")
                continue
            seen_invocations.add(invocation["key"])
            result = subprocess.run(
                invocation["args"],
                cwd=invocation["cwd"],
                env=invocation.get("env"),
                shell=invocation["shell"],
                check=False,
            )
            if result.returncode:
                rc = result.returncode
        return rc

    def _bad_revision(self, patch) -> str:
        if patch.get("source-base"):
            return patch["source-base"]
        source_commit = patch.get("source-commit")
        if not source_commit:
            self.die(f"{patch['path']}: source-base proof needs source-base or source-commit")
        return f"{source_commit}^"

    def _run_invocation(self, invocation, env=None) -> int:
        result = subprocess.run(
            invocation["args"],
            cwd=invocation["cwd"],
            env=env if env is not None else invocation.get("env"),
            shell=invocation["shell"],
            check=False,
        )
        return result.returncode

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
                if invocation.get("env"):
                    bad_env.update(invocation["env"])
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
        return self._run_invocation(invocation)

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
            self.inf(f"  {invocation['display']}")
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
            missing_env = [
                env_name
                for env_name in invocation.get("requires_env", [])
                if not os.environ.get(env_name)
            ]
            if missing_env:
                self.die(
                    f"{patch['path']}: missing required environment for {name}: "
                    f"{', '.join(missing_env)}"
                )
            if invocation["key"] in seen_invocations:
                self.inf("  skipped duplicate invocation already run")
                continue
            seen_invocations.add(invocation["key"])
            if mode == "source-base":
                result_rc = self._run_source_base_proof(patch, proof, invocation)
            else:
                result_rc = self._run_invocation(invocation)
            if result_rc:
                rc = result_rc
        return rc

    def _changed_submodules(self) -> list[str]:
        """Submodules whose checkout differs from their manifest revision.

        PoC heuristic: ask west which projects are not at manifest-rev. The real
        version would diff each project against its upstream merge-base and also
        honour an explicit submod:<name> mapping on each test.
        """
        changed: list[str] = []
        for project in self.manifest.projects:
            if not self.manifest.is_active(project):
                continue
            path = Path(self.topdir) / project.path
            if not (path / ".git").exists():
                continue
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if project.revision and head and not head.startswith(project.revision):
                changed.append(project.name)
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
                if args.prove_red:
                    selected = [
                        (patch, test)
                        for patch, test in selected
                        if test.get("red") or test.get("red-proof")
                    ]
                    if not selected:
                        self.die("no red-proof tests selected from patch metadata")
                    raise SystemExit(self._run_red_proofs(selected, args.list, unknown))
                raise SystemExit(self._run_metadata_tests(selected, args.list, unknown))
            if args.list:
                return
            self.die("no tests selected from patch metadata")

        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f"no testkit at {testkit}")

        executor = args.executor or shutil.which("darling-debug-runner")
        build = self._configure_and_build(testkit, executor)

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
